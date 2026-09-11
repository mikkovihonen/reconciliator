"""Typer CLI interface exposing ingest, reconcile, status, and reset subcommands via unified --config."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import IO, Any, Iterator, Optional

import typer
from lxml import etree
from rich.console import Console
from rich.table import Table

from reconciliator.config import AppConfig, FintoDownloadConfig, OAIIngestConfig, load_config
from reconciliator.db import reset_all_storage
from reconciliator.embedder import SentenceEmbedder
from reconciliator.finto_folding import FintoFolder
from reconciliator.finto_store import FintoStore
from reconciliator.ingestion.finto import ingest_finto_file
from reconciliator.ingestion.finto_downloader import FintoDownloader
from reconciliator.ingestion.oai import ingest_oai_pipeline
from reconciliator.ingestion.wikidata import ingest_wikidata_pipeline
from reconciliator.loaders.bibra import (
    bibra_to_marcxml_elem,
    enrich_bibra_json,
    iter_bibra_records,
)
from reconciliator.loaders.marcxml import (
    iter_marc_record_elements,
    parse_marc_record_elem,
)
from reconciliator.matcher import ReconciliationEngine
from reconciliator.models import TaskProgress
from reconciliator.normalizers.record import RecordNormalizer
from reconciliator.progress import (
    ProgressTracker,
    format_task_table,
    get_task_progress,
    list_tasks,
    parse_duration_seconds,
    prune_tasks,
    render_status_json,
    run_telemetry_tui,
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
    all_components: bool = typer.Option(
        False,
        "--all",
        "-a",
        help="Reset all storage (authority.db, finto.db, progress.db, FAISS index)",
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
    if all_components or not (authority or finto or progress):
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
    interval: Optional[float] = typer.Option(
        None, "--interval", "-i", help="Watch polling interval in seconds"
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit output as JSON"),
    prune_completed: bool = typer.Option(
        False, "--prune-completed", "--prune", help="Prune completed tasks after displaying status"
    ),
) -> None:
    """Query real-time task progress and telemetry from progress.db."""
    cfg = load_config(config)
    progress_db = cfg.storage.progress_db
    poll_interval = interval if interval is not None else cfg.telemetry.watch_interval_seconds

    def fetch_tasks() -> list[TaskProgress]:
        if task_id:
            t = get_task_progress(progress_db, task_id)
            return [t] if t else []
        return list_tasks(progress_db)

    if watch:
        if json_output:
            try:
                while True:
                    tasks = fetch_tasks()
                    console.print(render_status_json(tasks))
                    if tasks and all(t.status in ("completed", "failed") for t in tasks):
                        break
                    time.sleep(poll_interval)
            except KeyboardInterrupt:
                pass
        else:
            run_telemetry_tui(
                fetch_tasks=fetch_tasks,
                db_path=progress_db,
                interval=poll_interval,
                console=console,
            )
    else:
        tasks = fetch_tasks()
        if json_output:
            console.print(render_status_json(tasks))
        else:
            table = format_task_table(tasks)
            console.print(table)

        if prune_completed:
            deleted = prune_tasks(progress_db, status="completed")
            console.print(f"[green]Pruned {deleted} completed task(s) from progress.db.[/green]")


@app.command("prune")
def prune_cmd(
    config: Path = typer.Option(
        Path("config.yaml"), "--config", "-c", help="Path to YAML configuration file"
    ),
    task_id: Optional[str] = typer.Option(None, "--task-id", help="Prune specific task ID"),
    completed: bool = typer.Option(False, "--completed", help="Prune completed tasks"),
    failed: bool = typer.Option(False, "--failed", help="Prune failed tasks"),
    older_than: Optional[str] = typer.Option(
        None, "--older-than", help="Prune tasks older than duration (e.g. 7d, 24h, 30m, 60s)"
    ),
    keep_last: Optional[int] = typer.Option(None, "--keep-last", help="Keep the latest N tasks"),
    all_tasks: bool = typer.Option(False, "--all", "-a", help="Prune all finished tasks"),
    include_running: bool = typer.Option(
        False, "--include-running", help="Include active running tasks in pruning"
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Bypass interactive confirmation prompt"),
) -> None:
    """Prune telemetry tasks and monitoring records from progress.db."""
    cfg = load_config(config)
    progress_db = cfg.storage.progress_db

    if completed and failed:
        console.print(
            "[red]Cannot specify both --completed and --failed. Use --all to prune all finished tasks.[/red]"
        )
        raise typer.BadParameter("Cannot specify both --completed and --failed")

    status: Optional[str] = None
    if completed:
        status = "completed"
    elif failed:
        status = "failed"
    elif all_tasks and not include_running:
        status = "terminated"

    if (
        not task_id
        and not completed
        and not failed
        and not older_than
        and keep_last is None
        and not all_tasks
    ):
        console.print(
            "[yellow]No filter specified. Please specify --completed, --failed, --older-than, --keep-last, --task-id, or --all.[/yellow]"
        )
        raise typer.BadParameter("No prune filter specified")

    older_than_seconds: Optional[float] = None
    if older_than:
        try:
            older_than_seconds = parse_duration_seconds(older_than)
        except ValueError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.BadParameter(str(exc))

    if not yes:
        confirm = typer.confirm(
            "Are you sure you want to prune records from progress.db matching the criteria?"
        )
        if not confirm:
            console.print("[yellow]Prune aborted.[/yellow]")
            raise typer.Abort()

    deleted = prune_tasks(
        db_path=progress_db,
        task_id=task_id,
        status=status,
        older_than_seconds=older_than_seconds,
        keep_last=keep_last,
        include_running=include_running,
    )
    console.print(f"[green]Successfully pruned {deleted} task(s) from progress.db.[/green]")


@app.command("prune-telemetry", hidden=True)
def prune_telemetry_cmd(
    config: Path = typer.Option(
        Path("config.yaml"), "--config", "-c", help="Path to YAML configuration file"
    ),
    task_id: Optional[str] = typer.Option(None, "--task-id", help="Prune specific task ID"),
    completed: bool = typer.Option(False, "--completed", help="Prune completed tasks"),
    failed: bool = typer.Option(False, "--failed", help="Prune failed tasks"),
    older_than: Optional[str] = typer.Option(
        None, "--older-than", help="Prune tasks older than duration (e.g. 7d, 24h, 30m, 60s)"
    ),
    keep_last: Optional[int] = typer.Option(None, "--keep-last", help="Keep the latest N tasks"),
    all_tasks: bool = typer.Option(False, "--all", "-a", help="Prune all finished tasks"),
    include_running: bool = typer.Option(
        False, "--include-running", help="Include active running tasks in pruning"
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Bypass interactive confirmation prompt"),
) -> None:
    """Alias for prune command."""
    prune_cmd(
        config=config,
        task_id=task_id,
        completed=completed,
        failed=failed,
        older_than=older_than,
        keep_last=keep_last,
        all_tasks=all_tasks,
        include_running=include_running,
        yes=yes,
    )


@app.command("download-finto")
def download_finto_cmd(
    config: Path = typer.Option(
        Path("config.yaml"), "--config", "-c", help="Path to YAML configuration file"
    ),
    vocab: Optional[list[str]] = typer.Option(
        None,
        "--vocab",
        "-v",
        help="Specific vocabulary ID to download (repeatable, e.g. -v yso -v slm)",
    ),
    output_dir: Optional[Path] = typer.Option(
        None, "--output-dir", "-o", help="Target directory for downloaded .ttl files"
    ),
    all_vocabs: bool = typer.Option(
        False, "--all", help="Discover and download all vocabularies available on Finto"
    ),
    list_vocabs: bool = typer.Option(
        False, "--list", help="List all vocabularies available on Finto and exit"
    ),
    force: bool = typer.Option(
        False, "--force", help="Force re-download even if file already exists"
    ),
    ingest: bool = typer.Option(
        False, "--ingest", help="Automatically ingest downloaded vocabularies into finto.db"
    ),
) -> None:
    """Download FINTO value vocabularies in SKOS Turtle format for offline use."""
    cfg = load_config(config) if config.exists() else AppConfig()
    dl_cfg = cfg.download_finto or FintoDownloadConfig()

    api_url = dl_cfg.api_url
    target_dir = output_dir or dl_cfg.output_dir
    overwrite = force or dl_cfg.overwrite

    downloader = FintoDownloader(api_url=api_url, timeout_seconds=dl_cfg.timeout_seconds)

    if list_vocabs:
        console.print(f"[cyan]Fetching available vocabularies from {api_url}...[/cyan]")
        vocabs = downloader.list_vocabularies()
        table = Table(title="FINTO Vocabularies")
        table.add_column("ID", style="bold green")
        table.add_column("Title")
        table.add_column("URI")
        for v in vocabs:
            table.add_row(str(v.get("id", "")), str(v.get("title", "")), str(v.get("uri", "")))
        console.print(table)
        return

    if all_vocabs:
        console.print(f"[cyan]Discovering all vocabularies from {api_url}...[/cyan]")
        vocabs = downloader.list_vocabularies()
        vocab_ids = [str(v["id"]) for v in vocabs if "id" in v]
    elif vocab:
        vocab_ids = vocab
    else:
        vocab_ids = dl_cfg.vocabularies

    if not vocab_ids:
        console.print("[yellow]No vocabularies specified or discovered to download.[/yellow]")
        return

    tracker = ProgressTracker(
        db_path=cfg.storage.progress_db,
        task_id=f"finto-download-{int(time.time())}",
        task_type="finto_ingest",
        heartbeat_interval=cfg.telemetry.heartbeat_interval_seconds,
    )

    console.print(f"[cyan]Downloading {len(vocab_ids)} vocabularies to {target_dir}...[/cyan]")

    def _progress_cb(v_id: str, idx: int, total: int) -> None:
        console.print(f"  [{idx}/{total}] Downloading [bold green]{v_id}[/bold green]...")

    downloaded = downloader.download_vocabularies(
        vocab_ids=vocab_ids,
        output_dir=target_dir,
        overwrite=overwrite,
        format=dl_cfg.format,
        tracker=tracker,
        on_progress=_progress_cb,
    )
    console.print(
        f"[green]Successfully downloaded {len(downloaded)} vocabularies to {target_dir}.[/green]"
    )

    if ingest:
        console.print(
            f"[cyan]Ingesting downloaded vocabularies into finto.db ({cfg.storage.finto_db})...[/cyan]"
        )
        finto_store = FintoStore(cfg.storage.finto_db)
        ingest_tracker = ProgressTracker(
            db_path=cfg.storage.progress_db,
            task_id=f"finto-ingest-{int(time.time())}",
            task_type="finto_ingest",
            heartbeat_interval=cfg.telemetry.heartbeat_interval_seconds,
        )
        c_count, m_count = ingest_finto_file(target_dir, finto_store, tracker=ingest_tracker)
        console.print(
            f"[green]Successfully ingested {c_count} concepts and {m_count} mappings into {cfg.storage.finto_db}.[/green]"
        )


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


@app.command("ingest-oai")
def ingest_oai_cmd(
    config: Path = typer.Option(
        Path("config.yaml"), "--config", "-c", help="Path to YAML configuration file"
    ),
    source: Optional[str] = typer.Option(
        None,
        "--source",
        "-s",
        help="Name of specific configured OAI source to ingest (e.g. melinda)",
    ),
    all_sources: bool = typer.Option(
        False, "--all", "-a", help="Ingest all enabled configured OAI sources"
    ),
    from_date: Optional[str] = typer.Option(
        None, "--from", help="Override harvest start date (YYYY-MM-DD)"
    ),
    until_date: Optional[str] = typer.Option(
        None, "--until", help="Override harvest end date (YYYY-MM-DD)"
    ),
) -> None:
    """Harvest or read authority records from configured OAI-PMH/XML sources."""
    cfg = load_config(config)
    if not cfg.ingest_oai:
        console.print("[red]No ingest_oai section found in configuration.[/red]")
        raise typer.Exit(code=1)

    targets: dict[str, OAIIngestConfig] = {}
    if all_sources:
        targets = {name: src for name, src in cfg.ingest_oai.items() if src.enabled}
        if not targets:
            console.print("[yellow]No enabled sources found under ingest_oai.[/yellow]")
            return
    elif source:
        if source not in cfg.ingest_oai:
            available = ", ".join(cfg.ingest_oai.keys())
            console.print(
                f"[red]Source '{source}' not found in ingest_oai config. Available sources: {available}[/red]"
            )
            raise typer.Exit(code=1)
        targets = {source: cfg.ingest_oai[source]}
    elif len(cfg.ingest_oai) == 1:
        targets = {k: v for k, v in cfg.ingest_oai.items() if v.enabled}
    else:
        available = ", ".join(cfg.ingest_oai.keys())
        console.print(
            f"[red]Multiple OAI sources configured ({available}). Please specify --source <name> or --all.[/red]"
        )
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

    for src_name, src_cfg in targets.items():
        if from_date:
            src_cfg.from_date = from_date
        if until_date:
            src_cfg.until_date = until_date

        tracker = ProgressTracker(
            db_path=cfg.storage.progress_db,
            task_id=f"oai-ingest-{src_name}-{int(time.time())}",
            task_type="oai_pmh_harvest",
            heartbeat_interval=cfg.telemetry.heartbeat_interval_seconds,
        )

        console.print(f"[cyan]Ingesting authority records from source '{src_name}'...[/cyan]")
        total = ingest_oai_pipeline(
            source_name=src_name,
            config=src_cfg,
            store=store,
            normalizer=normalizer,
            embedder=embedder,
            tracker=tracker,
        )
        console.print(
            f"[green]Successfully ingested {total} records from source '{src_name}'.[/green]"
        )


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
    bytes_total = r_cfg.target_file.stat().st_size if r_cfg.target_file.exists() else None
    tracker.start_task(stage="reconciling", bytes_total=bytes_total)

    start_time_sec = time.time()
    console.print(f"[cyan]Reconciling {r_cfg.target_file}...[/cyan]")

    # Format detection
    is_bibra = False
    if r_cfg.target_format == "bibra":
        is_bibra = True
    elif r_cfg.target_format == "auto":
        suffix = r_cfg.target_file.suffix.lower()
        if suffix in (".json", ".jsonl", ".ndjson"):
            is_bibra = True

    is_json_output = False
    if r_cfg.output_file:
        if r_cfg.output_format == "bibra":
            is_json_output = True
        elif r_cfg.output_format == "marcxml":
            is_json_output = False
        else:
            if r_cfg.output_file.suffix.lower() in (".json", ".jsonl", ".ndjson"):
                is_json_output = True
            else:
                is_json_output = False

    out_f: Optional[IO[bytes]] = None
    if r_cfg.output_file:
        r_cfg.output_file.parent.mkdir(parents=True, exist_ok=True)
        out_f = open(r_cfg.output_file, "wb")
        if not is_json_output:
            out_f.write(
                b'<?xml version="1.0" encoding="UTF-8"?>\n'
                b'<collection xmlns="http://www.loc.gov/MARC21/slim">\n'
            )

    def _iter_records() -> Iterator[
        tuple[Optional[etree._Element], dict[str, Any], int, Optional[dict[str, Any]]]
    ]:
        if is_bibra:
            for raw_rec_item, b_offset, bib_item in iter_bibra_records(r_cfg.target_file):
                yield None, raw_rec_item, b_offset, bib_item
        else:
            for r_elem, b_offset in iter_marc_record_elements(r_cfg.target_file):
                raw_rec_item = parse_marc_record_elem(r_elem, source="target")
                yield r_elem, raw_rec_item, b_offset, None

    total_records = 0
    try:
        for rec_elem, raw_rec, byte_offset, bibra_item in _iter_records():
            total_records += 1
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
                        else (
                            res.accepted_candidates[0].source_id if res.accepted_candidates else ""
                        )
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
                        else (
                            res.accepted_candidates[0].source_id if res.accepted_candidates else ""
                        )
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

            # Mutate / serialize and stream write if output_file requested
            if out_f is not None:
                if is_json_output:
                    orig_dict = (
                        bibra_item
                        if bibra_item is not None
                        else target_record.model_dump(by_alias=True)
                    )
                    enriched_json = enrich_bibra_json(orig_dict, res, target_record=target_record)
                    out_f.write(json.dumps(enriched_json, ensure_ascii=False).encode("utf-8"))
                    out_f.write(b"\n")
                else:
                    if rec_elem is not None:
                        enricher.enrich_record_tree(
                            rec_elem,
                            res,
                            enrich=r_cfg.enrich,
                            fold_legacy=r_cfg.fold_legacy,
                        )
                        out_f.write(etree.tostring(rec_elem, encoding="utf-8", pretty_print=True))
                        out_f.write(b"\n")
                    elif bibra_item is not None:
                        synth_elem = bibra_to_marcxml_elem(
                            bibra_item, res, target_record=target_record
                        )
                        out_f.write(etree.tostring(synth_elem, encoding="utf-8", pretty_print=True))
                        out_f.write(b"\n")

            tracker.update_progress(
                items_increment=1,
                bytes_processed=byte_offset,
            )
    finally:
        if out_f is not None:
            if not is_json_output:
                out_f.write(b"</collection>\n")
            out_f.close()
            console.print(f"[green]Wrote output to {r_cfg.output_file}[/green]")

    reporter.total_records = total_records
    tracker.update_progress(
        items_total=total_records,
        bytes_processed=bytes_total,
        force=True,
    )

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
    if cfg.ingest_oai:
        ingest_oai_cmd(config=config, all_sources=True)
    if cfg.ingest_wikidata:
        ingest_wikidata_cmd(config=config)
    if cfg.reconcile:
        reconcile_cmd(config=config)


def main() -> None:
    """Main CLI entrypoint."""
    app()


if __name__ == "__main__":
    main()
