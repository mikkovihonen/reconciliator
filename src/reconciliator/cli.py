"""Typer CLI interface exposing ingest, reconcile, status, and reset subcommands via unified --config."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Optional

import typer
from lxml import etree
from rich.console import Console

from reconciliator.config import load_config
from reconciliator.db import reset_all_storage
from reconciliator.embedder import SentenceEmbedder
from reconciliator.finto_folding import FintoFolder
from reconciliator.finto_store import FintoStore
from reconciliator.ingestion.finto import ingest_finto_file
from reconciliator.ingestion.melinda import ingest_melinda_pipeline
from reconciliator.ingestion.wikidata import ingest_wikidata_pipeline
from reconciliator.loaders.marcxml import MARC_NS, parse_marc_record_elem
from reconciliator.matcher import ReconciliationEngine
from reconciliator.models import TaskProgress
from reconciliator.normalizers.record import RecordNormalizer
from reconciliator.progress import (
    ProgressTracker,
    format_task_table,
    get_task_progress,
    list_tasks,
    render_status_json,
)
from reconciliator.reports import AgentReportRow, ReportGenerator, SubjectReportRow
from reconciliator.repository import AuthorityStore
from reconciliator.vocabularies import detect_vocabulary
from reconciliator.xml_enricher import MARCXMLEnricher

app = typer.Typer(
    help="Reconciliator: High-Performance Finnish Bibliographic Authority Reconciliation Engine"
)
console = Console()


@app.command("reset")
def reset_cmd(
    config: Path = typer.Option(
        Path("config.yaml"), "--config", "-c", help="Path to YAML configuration file"
    ),
    authority: bool = typer.Option(
        False, "--authority", help="Reset authority.db and FAISS index only"
    ),
    finto: bool = typer.Option(False, "--finto", help="Reset finto.db only"),
    progress: bool = typer.Option(False, "--progress", help="Reset progress.db only"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Bypass interactive confirmation prompt"),
) -> None:
    """Reset configured databases and FAISS vector index."""
    cfg = load_config(config)

    # Determine which components to reset
    if not (authority or finto or progress):
        reset_auth = True
        reset_fin = True
        reset_prog = True
    else:
        reset_auth = authority
        reset_fin = finto
        reset_prog = progress

    if not yes:
        confirm = typer.confirm("Are you sure you want to reset the specified storage?")
        if not confirm:
            console.print("[yellow]Reset aborted.[/yellow]")
            raise typer.Abort()

    reset_all_storage(
        storage_config=cfg.storage,
        reset_authority=reset_auth,
        reset_finto=reset_fin,
        reset_progress=reset_prog,
    )
    console.print("[green]Successfully reset specified storage.[/green]")


@app.command("status")
def status_cmd(
    config: Path = typer.Option(
        Path("config.yaml"), "--config", "-c", help="Path to YAML configuration file"
    ),
    task_id: Optional[str] = typer.Option(
        None, "--task-id", help="Query status of a specific task ID"
    ),
    watch: bool = typer.Option(False, "--watch", "-w", help="Continuous live watch monitor"),
    interval: float = typer.Option(
        1.0, "--interval", "-i", help="Watch polling interval in seconds"
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit output as JSON"),
) -> None:
    """Query real-time task progress and telemetry from progress.db."""
    cfg = load_config(config)
    progress_db = cfg.storage.progress_db

    def fetch_tasks() -> list[TaskProgress]:
        if task_id:
            t = get_task_progress(progress_db, task_id)
            return [t] if t else []
        return list_tasks(progress_db)

    if watch:
        try:
            while True:
                tasks = fetch_tasks()
                if json_output:
                    console.print(render_status_json(tasks))
                else:
                    console.clear()
                    table = format_task_table(tasks)
                    console.print(table)

                # If all tasks are completed or failed, stop watch mode
                if tasks and all(t.status in ("completed", "failed") for t in tasks):
                    break
                time.sleep(interval)
        except KeyboardInterrupt:
            pass
    else:
        tasks = fetch_tasks()
        if json_output:
            console.print(render_status_json(tasks))
        else:
            table = format_task_table(tasks)
            console.print(table)


@app.command("ingest-finto")
def ingest_finto_cmd(
    config: Path = typer.Option(
        Path("config.yaml"), "--config", "-c", help="Path to YAML configuration file"
    ),
) -> None:
    """Ingest FINTO ontologies and legacy migration tables into dedicated finto.db."""
    cfg = load_config(config)
    if not cfg.ingest_finto:
        console.print("[red]No ingest_finto section found in configuration.[/red]")
        raise typer.Exit(code=1)

    finto_store = FintoStore(cfg.storage.finto_db)
    tracker = ProgressTracker(
        db_path=cfg.storage.progress_db,
        task_id=f"finto-ingest-{int(time.time())}",
        task_type="finto_ingest",
        heartbeat_interval=cfg.telemetry.heartbeat_interval_seconds,
    )

    console.print(f"[cyan]Ingesting FINTO from {cfg.ingest_finto.input_path}...[/cyan]")
    c_count, m_count = ingest_finto_file(cfg.ingest_finto.input_path, finto_store, tracker=tracker)
    console.print(
        f"[green]Successfully ingested {c_count} concepts and {m_count} mappings.[/green]"
    )


@app.command("ingest-melinda")
def ingest_melinda_cmd(
    config: Path = typer.Option(
        Path("config.yaml"), "--config", "-c", help="Path to YAML configuration file"
    ),
) -> None:
    """Harvest or read Melinda authority records, embed, and index into authority.db and FAISS."""
    cfg = load_config(config)
    if not cfg.ingest_melinda:
        console.print("[red]No ingest_melinda section found in configuration.[/red]")
        raise typer.Exit(code=1)

    finto_store = FintoStore(cfg.storage.finto_db)
    finto_folder = FintoFolder(finto_store)
    embedder = SentenceEmbedder(cfg.embedding)
    normalizer = RecordNormalizer(
        finto_folder=finto_folder, template_engine=embedder.template_engine
    )
    store = AuthorityStore(
        cfg.storage.authority_db, cfg.storage.index_path, dimension=embedder.dimension
    )

    tracker = ProgressTracker(
        db_path=cfg.storage.progress_db,
        task_id=f"melinda-ingest-{int(time.time())}",
        task_type="oai_pmh_harvest",
        heartbeat_interval=cfg.telemetry.heartbeat_interval_seconds,
    )

    console.print("[cyan]Ingesting Melinda authority records...[/cyan]")
    total = ingest_melinda_pipeline(
        cfg.ingest_melinda, store, normalizer, embedder, tracker=tracker
    )
    console.print(f"[green]Successfully ingested {total} records from Melinda.[/green]")


@app.command("ingest-wikidata")
def ingest_wikidata_cmd(
    config: Path = typer.Option(
        Path("config.yaml"), "--config", "-c", help="Path to YAML configuration file"
    ),
) -> None:
    """Stream gzipped Wikidata dump, extract entities, embed, and index."""
    cfg = load_config(config)
    if not cfg.ingest_wikidata:
        console.print("[red]No ingest_wikidata section found in configuration.[/red]")
        raise typer.Exit(code=1)

    finto_store = FintoStore(cfg.storage.finto_db)
    finto_folder = FintoFolder(finto_store)
    embedder = SentenceEmbedder(cfg.embedding)
    normalizer = RecordNormalizer(
        finto_folder=finto_folder, template_engine=embedder.template_engine
    )
    store = AuthorityStore(
        cfg.storage.authority_db, cfg.storage.index_path, dimension=embedder.dimension
    )

    tracker = ProgressTracker(
        db_path=cfg.storage.progress_db,
        task_id=f"wikidata-ingest-{int(time.time())}",
        task_type="wikidata_ingest",
        heartbeat_interval=cfg.telemetry.heartbeat_interval_seconds,
    )

    console.print("[cyan]Streaming Wikidata entities from compressed dump...[/cyan]")
    total = ingest_wikidata_pipeline(
        cfg.ingest_wikidata, store, normalizer, embedder, tracker=tracker
    )
    console.print(f"[green]Successfully ingested {total} records from Wikidata.[/green]")


@app.command("reconcile")
def reconcile_cmd(
    config: Path = typer.Option(
        Path("config.yaml"), "--config", "-c", help="Path to YAML configuration file"
    ),
) -> None:
    """Reconcile target MARCXML records offline, enrich URIs, and write dual TSV reports."""
    cfg = load_config(config)
    if not cfg.reconcile:
        console.print("[red]No reconcile section found in configuration.[/red]")
        raise typer.Exit(code=1)

    r_cfg = cfg.reconcile
    if not r_cfg.target_file.exists():
        console.print(f"[red]Target MARCXML file not found: {r_cfg.target_file}[/red]")
        raise typer.Exit(code=1)

    finto_store = FintoStore(cfg.storage.finto_db)
    finto_folder = FintoFolder(finto_store)
    embedder = SentenceEmbedder(cfg.embedding)
    normalizer = RecordNormalizer(
        finto_folder=finto_folder, template_engine=embedder.template_engine
    )
    store = AuthorityStore(
        cfg.storage.authority_db, cfg.storage.index_path, dimension=embedder.dimension
    )

    # Verify index metadata compatibility
    if store.indexer.total_vectors > 0:
        store.indexer.verify_metadata(
            expected_model=cfg.embedding.model_name,
            expected_dimension=embedder.dimension,
            expected_template_hash=embedder.template_hash,
            force_template=cfg.embedding.force_template,
        )

    engine = ReconciliationEngine(authority_store=store, embedder=embedder, config=r_cfg)
    enricher = MARCXMLEnricher(finto_folder=finto_folder)
    reporter = ReportGenerator(config=cfg)

    tracker = ProgressTracker(
        db_path=cfg.storage.progress_db,
        task_id=f"reconciliation-{int(time.time())}",
        task_type="reconciliation",
        heartbeat_interval=cfg.telemetry.heartbeat_interval_seconds,
    )
    tracker.start_task(stage="reconciling")

    start_time_sec = time.time()
    console.print(f"[cyan]Reconciling {r_cfg.target_file}...[/cyan]")

    # Parse XML document
    tree = etree.parse(str(r_cfg.target_file))
    root = tree.getroot()
    records = root.findall(".//marc:record", MARC_NS)
    if not records:
        records = root.findall(".//record")
    if not records and (root.tag.endswith("record") or root.tag == "record"):
        records = [root]

    tracker.update_progress(items_total=len(records))
    reporter.total_records = len(records)

    for rec_elem in records:
        raw_rec = parse_marc_record_elem(rec_elem, source="target")
        target_record = normalizer.normalize(raw_rec)

        res = engine.reconcile_record(target_record)
        if res.accepted_candidates:
            reporter.matched_records += 1

        # Record agent report rows
        best_match = res.matches[0] if res.matches else None
        match_tier = best_match.match_tier if best_match else "unmatched"
        match_score = best_match.match_score if best_match else 0.0
        dense_score = best_match.dense_score if best_match else None
        sparse_score = best_match.sparse_score if best_match else None

        for idx, agent in enumerate(target_record.agents):
            selected = res.selected_agent_uris.get(idx, [])
            suppressed = res.suppressed_agent_uris.get(idx, [])
            existing = set(agent.uri_subfield_0 + agent.uri_subfield_1)
            discovered_map = {u.uri: u for u in res.discovered_agent_uris.get(idx, [])}

            # Field tag determination: use recorded field_tag, or fallback by is_personal / position
            field_tag = agent.field_tag
            if not field_tag:
                if agent.is_personal:
                    field_tag = "100" if idx == 0 else "700"
                else:
                    field_tag = "110" if idx == 0 else "710"

            for uri in selected:
                is_novel = uri not in existing
                action = (
                    "injected"
                    if (r_cfg.enrich and is_novel)
                    else ("existing" if not is_novel else "proposed")
                )
                subfield_code = "1" if "wikidata.org" in uri.lower() else "0"
                disc_uri = discovered_map.get(uri)
                cand_source_id = (
                    disc_uri.source_record_id
                    if disc_uri
                    else (res.accepted_candidates[0].source_id if res.accepted_candidates else "")
                )
                cand_source = (
                    disc_uri.source
                    if disc_uri
                    else (res.accepted_candidates[0].source if res.accepted_candidates else "")
                )
                agent_name_score = disc_uri.alignment_score if disc_uri else 100.0

                reporter.add_agent_row(
                    AgentReportRow(
                        target_record_id=target_record.source_id,
                        target_title=target_record.title,
                        target_year=target_record.publication_year,
                        target_isbn=target_record.identifiers.isbn[0]
                        if target_record.identifiers.isbn
                        else None,
                        field_tag=field_tag,
                        field_occurrence=idx + 1,
                        subfield_code=subfield_code,
                        target_heading=agent.name,
                        target_role=agent.role,
                        uri=uri,
                        vocabulary=detect_vocabulary(uri) or "OTHER",
                        authority_label=agent.name,
                        authority_record_id=cand_source_id,
                        authority_source=cand_source,
                        match_tier=match_tier,
                        match_score=match_score,
                        dense_score=dense_score,
                        sparse_score=sparse_score,
                        agent_name_score=agent_name_score,
                        action=action,
                        is_novel=is_novel,
                    )
                )

            for uri in suppressed:
                subfield_code = "1" if "wikidata.org" in uri.lower() else "0"
                disc_uri = discovered_map.get(uri)
                cand_source_id = (
                    disc_uri.source_record_id
                    if disc_uri
                    else (res.accepted_candidates[0].source_id if res.accepted_candidates else "")
                )
                cand_source = (
                    disc_uri.source
                    if disc_uri
                    else (res.accepted_candidates[0].source if res.accepted_candidates else "")
                )
                agent_name_score = disc_uri.alignment_score if disc_uri else 100.0

                reporter.add_agent_row(
                    AgentReportRow(
                        target_record_id=target_record.source_id,
                        target_title=target_record.title,
                        target_year=target_record.publication_year,
                        target_isbn=target_record.identifiers.isbn[0]
                        if target_record.identifiers.isbn
                        else None,
                        field_tag=field_tag,
                        field_occurrence=idx + 1,
                        subfield_code=subfield_code,
                        target_heading=agent.name,
                        target_role=agent.role,
                        uri=uri,
                        vocabulary=detect_vocabulary(uri) or "OTHER",
                        authority_label=agent.name,
                        authority_record_id=cand_source_id,
                        authority_source=cand_source,
                        match_tier=match_tier,
                        match_score=match_score,
                        dense_score=dense_score,
                        sparse_score=sparse_score,
                        agent_name_score=agent_name_score,
                        action="suppressed_by_priority",
                        is_novel=True,
                    )
                )

        # Record subject report rows
        all_subjects = (
            [("650", s) for s in target_record.subjects.topical]
            + [("655", s) for s in target_record.subjects.genre_form]
            + [("651", s) for s in target_record.subjects.geographic]
            + [("382", s) for s in target_record.subjects.medium_of_performance]
        )
        for s_idx, (default_tag, sub_item) in enumerate(all_subjects):
            s_tag = sub_item.source_field or default_tag
            t_tag = sub_item.target_field or default_tag
            s_vocab = sub_item.source_vocab or ""
            s_uri = sub_item.source_uri or ""
            t_vocab = sub_item.vocabulary or ""
            t_uri = sub_item.uri or ""
            s_heading = sub_item.source_heading or sub_item.pref_label
            m_type = sub_item.mapping_type or "pref_label_lookup"
            tag_changed = s_tag != t_tag

            if not t_uri:
                action = "unmapped"
                is_novel = False
            elif s_uri and s_uri == t_uri:
                action = "existing"
                is_novel = False
            elif r_cfg.enrich:
                action = "injected"
                is_novel = True
            else:
                action = "proposed"
                is_novel = True

            reporter.add_subject_row(
                SubjectReportRow(
                    target_record_id=target_record.source_id,
                    target_title=target_record.title,
                    source_field_tag=s_tag,
                    field_occurrence=s_idx + 1,
                    source_heading=s_heading,
                    source_vocab=s_vocab,
                    source_uri=s_uri,
                    language=target_record.language or "fi",
                    target_field_tag=t_tag,
                    target_vocab=t_vocab,
                    target_uri=t_uri,
                    target_pref_label=sub_item.pref_label,
                    mapping_type=m_type,
                    action=action,
                    is_novel=is_novel,
                    tag_changed=tag_changed,
                )
            )

        # Mutate XML if output_file requested
        if r_cfg.output_file:
            enricher.enrich_record_tree(
                rec_elem,
                res,
                enrich=r_cfg.enrich,
                fold_legacy=r_cfg.fold_legacy,
            )

        tracker.update_progress(items_increment=1)

    # Write output XML
    if r_cfg.output_file:
        r_cfg.output_file.parent.mkdir(parents=True, exist_ok=True)
        tree.write(
            str(r_cfg.output_file), encoding="utf-8", xml_declaration=True, pretty_print=True
        )
        console.print(f"[green]Wrote output MARCXML to {r_cfg.output_file}[/green]")

    # Write TSV Reports
    reporter.elapsed_seconds = time.time() - start_time_sec
    reporter.write_agent_detailed_report(r_cfg.agent_reports.detailed_file)
    reporter.write_agent_summary_report(r_cfg.agent_reports.summary_file)
    reporter.write_subject_detailed_report(r_cfg.subject_reports.detailed_file)
    reporter.write_subject_summary_report(r_cfg.subject_reports.summary_file)

    tracker.complete_task(
        metadata={
            "matched_records": reporter.matched_records,
            "total_records": reporter.total_records,
        }
    )
    console.print("[green]Reconciliation complete! Emitted dual TSV report sets.[/green]")


@app.command("run")
def run_cmd(
    config: Path = typer.Option(
        Path("config.yaml"), "--config", "-c", help="Path to YAML configuration file"
    ),
) -> None:
    """Execute end-to-end pipeline configured in YAML."""
    cfg = load_config(config)
    if cfg.ingest_finto:
        ingest_finto_cmd(config=config)
    if cfg.ingest_melinda:
        ingest_melinda_cmd(config=config)
    if cfg.ingest_wikidata:
        ingest_wikidata_cmd(config=config)
    if cfg.reconcile:
        reconcile_cmd(config=config)


def main() -> None:
    """Main CLI entrypoint."""
    app()


if __name__ == "__main__":
    main()
