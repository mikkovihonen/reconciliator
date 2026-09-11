"""Typer CLI interface exposing ingest, reconcile, status, and reset subcommands via unified --config."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import IO, Any, Iterator, Optional

import typer
from lxml import etree
from rdflib import Graph
from rich.console import Console
from rich.table import Table

from reconciliator.config import AppConfig, OAIIngestConfig, SkosmosDownloadConfig, load_config
from reconciliator.db import reset_all_storage
from reconciliator.embedder import SentenceEmbedder
from reconciliator.ingestion.oai import ingest_oai_pipeline
from reconciliator.ingestion.skosmos_downloader import SkosmosDownloader
from reconciliator.ingestion.vocab import ingest_vocab_file
from reconciliator.ingestion.wikidata import ingest_wikidata_pipeline
from reconciliator.loaders.bibframe import (
    BIBFRAMESchemaProfile,
    enrich_bibframe_graph,
    enrich_bibframe_jsonld,
    get_profile,
    iter_bibframe_records,
    iter_jsonld_bibframe_records,
)
from reconciliator.loaders.bibra import (
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
from reconciliator.vocab_folding import VocabFolder
from reconciliator.vocab_store import VocabStore
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
        help="Reset all storage (authority.db, vocab.db, progress.db, vector index)",
    ),
    authority: bool = typer.Option(
        False, "--authority", help="Reset authority.db and vector index only"
    ),
    vocab: bool = typer.Option(False, "--vocab", help="Reset vocab.db only"),
    progress: bool = typer.Option(False, "--progress", help="Reset progress.db only"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Bypass interactive confirmation prompt"),
) -> None:
    """Reset configured databases and vector index."""
    cfg = load_config(config)

    # Determine which components to reset
    if all_components or not (authority or vocab or progress):
        reset_auth = True
        reset_voc = True
        reset_prog = True
    else:
        reset_auth = authority
        reset_voc = vocab
        reset_prog = progress

    if not yes:
        confirm = typer.confirm("Are you sure you want to reset the specified storage?")
        if not confirm:
            console.print("[yellow]Reset aborted.[/yellow]")
            raise typer.Abort()

    reset_all_storage(
        storage_config=cfg.storage,
        reset_authority=reset_auth,
        reset_vocab=reset_voc,
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


def _download_single_skosmos_source(
    cfg: AppConfig,
    src_name: str,
    dl_cfg: SkosmosDownloadConfig,
    vocab: Optional[list[str]] = None,
    output_dir: Optional[Path] = None,
    api_url: Optional[str] = None,
    all_vocabs: bool = False,
    force: bool = False,
    ingest: bool = False,
) -> None:
    target_api_url = api_url or dl_cfg.api_url
    target_dir = output_dir or dl_cfg.output_dir
    overwrite = force or dl_cfg.overwrite

    downloader = SkosmosDownloader(
        api_url=target_api_url,
        timeout_seconds=dl_cfg.timeout_seconds,
        dump_urls=dl_cfg.dump_urls,
    )

    if all_vocabs:
        console.print(f"[cyan]Discovering all vocabularies from {target_api_url}...[/cyan]")
        vocabs = downloader.list_vocabularies()
        vocab_ids = [str(v["id"]) for v in vocabs if "id" in v]
    elif vocab:
        vocab_ids = vocab
    else:
        vocab_ids = dl_cfg.vocabularies

    if not vocab_ids:
        console.print(
            f"[yellow]No vocabularies specified or discovered for Skosmos source '{src_name}'.[/yellow]"
        )
        return

    tracker = ProgressTracker(
        db_path=cfg.storage.progress_db,
        task_id=f"skosmos-download-{src_name}-{int(time.time())}",
        task_type="vocab_ingest",
        heartbeat_interval=cfg.telemetry.heartbeat_interval_seconds,
    )

    console.print(
        f"[cyan][{src_name}] Downloading {len(vocab_ids)} vocabularies from {target_api_url} to {target_dir}...[/cyan]"
    )

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
        f"[green][{src_name}] Successfully downloaded {len(downloaded)} vocabularies to {target_dir}.[/green]"
    )

    if ingest:
        console.print(
            f"[cyan][{src_name}] Ingesting downloaded vocabularies into vocab.db ({cfg.storage.vocab_db})...[/cyan]"
        )
        vocab_store = VocabStore(cfg.storage.vocab_db)
        ingest_tracker = ProgressTracker(
            db_path=cfg.storage.progress_db,
            task_id=f"vocab-ingest-{src_name}-{int(time.time())}",
            task_type="vocab_ingest",
            heartbeat_interval=cfg.telemetry.heartbeat_interval_seconds,
        )
        c_count, m_count = ingest_vocab_file(target_dir, vocab_store, tracker=ingest_tracker)
        console.print(
            f"[green][{src_name}] Successfully ingested {c_count} concepts and {m_count} mappings into {cfg.storage.vocab_db}.[/green]"
        )


@app.command("download-skosmos")
def download_skosmos_cmd(
    config: Path = typer.Option(
        Path("config.yaml"), "--config", "-c", help="Path to YAML configuration file"
    ),
    source: Optional[str] = typer.Option(
        None,
        "--source",
        "-s",
        help="Name of configured Skosmos source to download",
    ),
    all_sources: bool = typer.Option(
        False, "--all", "-a", help="Download vocabularies for all enabled Skosmos sources"
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
    api_url: Optional[str] = typer.Option(None, "--api-url", help="Override Skosmos REST API URL"),
    all_vocabs: bool = typer.Option(
        False,
        "--all-vocabs",
        help="Discover and download all vocabularies available on the Skosmos instance",
    ),
    list_vocabs: bool = typer.Option(
        False, "--list", help="List all vocabularies available on the Skosmos instance and exit"
    ),
    force: bool = typer.Option(
        False, "--force", help="Force re-download even if file already exists"
    ),
    ingest: bool = typer.Option(
        False, "--ingest", help="Automatically ingest downloaded vocabularies into vocab.db"
    ),
) -> None:
    """Download value vocabularies in SKOS Turtle format from Skosmos instances for offline use."""
    cfg = load_config(config) if config.exists() else AppConfig()

    if list_vocabs:
        effective_api_url = api_url
        if not effective_api_url:
            if source and source in cfg.download_skosmos:
                effective_api_url = cfg.download_skosmos[source].api_url
            elif cfg.download_skosmos:
                effective_api_url = next(iter(cfg.download_skosmos.values())).api_url
            else:
                console.print(
                    "[red]Error: No Skosmos API URL provided and no source configured under 'download_skosmos'.[/red]"
                )
                raise typer.Exit(code=1)

        downloader = SkosmosDownloader(api_url=effective_api_url)
        console.print(f"[cyan]Fetching available vocabularies from {effective_api_url}...[/cyan]")
        vocabs = downloader.list_vocabularies()
        table = Table(title=f"Skosmos Vocabularies ({effective_api_url})")
        table.add_column("ID", style="bold green")
        table.add_column("Title")
        table.add_column("URI")
        for v in vocabs:
            table.add_row(str(v.get("id", "")), str(v.get("title", "")), str(v.get("uri", "")))
        console.print(table)
        return

    if all_sources:
        if not cfg.download_skosmos:
            console.print(
                "[yellow]No Skosmos sources configured under 'download_skosmos'.[/yellow]"
            )
            return
        enabled_sources = {k: v for k, v in cfg.download_skosmos.items() if v.enabled}
        if not enabled_sources:
            console.print(
                "[yellow]No Skosmos sources are currently enabled under 'download_skosmos'.[/yellow]"
            )
            return
        for name, dl_cfg in enabled_sources.items():
            _download_single_skosmos_source(
                cfg=cfg,
                src_name=name,
                dl_cfg=dl_cfg,
                vocab=vocab,
                output_dir=output_dir,
                api_url=api_url,
                all_vocabs=all_vocabs,
                force=force,
                ingest=ingest,
            )
        return

    # Single source selection
    if source:
        if source not in cfg.download_skosmos:
            console.print(
                f"[red]Error: Source '{source}' not found in configuration 'download_skosmos'.[/red]"
            )
            console.print(f"Available sources: {', '.join(cfg.download_skosmos.keys()) or 'None'}")
            raise typer.Exit(code=1)
        src_name = source
        dl_cfg = cfg.download_skosmos[source]
    else:
        enabled_sources = {k: v for k, v in cfg.download_skosmos.items() if v.enabled}
        if enabled_sources:
            src_name = next(iter(enabled_sources.keys()))
            dl_cfg = enabled_sources[src_name]
        elif cfg.download_skosmos:
            src_name = next(iter(cfg.download_skosmos.keys()))
            dl_cfg = cfg.download_skosmos[src_name]
        else:
            console.print(
                "[red]Error: No Skosmos source specified and none configured in 'download_skosmos'.[/red]"
            )
            console.print(
                "Please specify --source or configure at least one source under 'download_skosmos'."
            )
            raise typer.Exit(code=1)

    _download_single_skosmos_source(
        cfg=cfg,
        src_name=src_name,
        dl_cfg=dl_cfg,
        vocab=vocab,
        output_dir=output_dir,
        api_url=api_url,
        all_vocabs=all_vocabs,
        force=force,
        ingest=ingest,
    )


@app.command("ingest-vocab")
def ingest_vocab_cmd(
    config: Path = typer.Option(
        Path("config.yaml"), "--config", "-c", help="Path to YAML configuration file"
    ),
) -> None:
    """Ingest ontologies and legacy migration tables into dedicated vocab.db."""
    cfg = load_config(config)
    if not cfg.ingest_vocab:
        console.print("[red]No ingest_vocab section found in configuration.[/red]")
        raise typer.Exit(code=1)

    vocab_store = VocabStore(cfg.storage.vocab_db)
    tracker = ProgressTracker(
        db_path=cfg.storage.progress_db,
        task_id=f"vocab-ingest-{int(time.time())}",
        task_type="vocab_ingest",
        heartbeat_interval=cfg.telemetry.heartbeat_interval_seconds,
    )

    console.print(f"[cyan]Ingesting vocabularies from {cfg.ingest_vocab.input_path}...[/cyan]")
    c_count, m_count = ingest_vocab_file(cfg.ingest_vocab.input_path, vocab_store, tracker=tracker)
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

    vocab_store = VocabStore(cfg.storage.vocab_db)
    vocab_cfg = (
        cfg.ingest_vocab.normalization if cfg.ingest_vocab else None
    ) or cfg.normalization.vocab
    vocab_folder = VocabFolder(vocab_store, config=vocab_cfg)
    embedder = SentenceEmbedder(cfg.embedding)
    store = AuthorityStore(
        cfg.storage.authority_db,
        cfg.storage.index_path,
        dimension=embedder.dimension,
        vector_engine_config=cfg.storage.vector_engine,
    )

    for src_name, src_cfg in targets.items():
        if from_date:
            src_cfg.from_date = from_date
        if until_date:
            src_cfg.until_date = until_date

        src_norm_cfg = src_cfg.normalization or cfg.normalization.record
        normalizer = RecordNormalizer(
            vocab_folder=vocab_folder,
            template_engine=embedder.template_engine,
            config=src_norm_cfg,
        )

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

    vocab_store = VocabStore(cfg.storage.vocab_db)
    vocab_cfg = (
        cfg.ingest_vocab.normalization if cfg.ingest_vocab else None
    ) or cfg.normalization.vocab
    vocab_folder = VocabFolder(vocab_store, config=vocab_cfg)
    embedder = SentenceEmbedder(cfg.embedding)
    wiki_norm_cfg = cfg.ingest_wikidata.normalization or cfg.normalization.record
    normalizer = RecordNormalizer(
        vocab_folder=vocab_folder,
        template_engine=embedder.template_engine,
        config=wiki_norm_cfg,
    )
    store = AuthorityStore(
        cfg.storage.authority_db,
        cfg.storage.index_path,
        dimension=embedder.dimension,
        vector_engine_config=cfg.storage.vector_engine,
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

    vocab_store = VocabStore(cfg.storage.vocab_db)
    vocab_cfg = (
        cfg.ingest_vocab.normalization if cfg.ingest_vocab else None
    ) or cfg.normalization.vocab
    vocab_folder = VocabFolder(vocab_store, config=vocab_cfg)
    embedder = SentenceEmbedder(cfg.embedding)
    rec_norm_cfg = r_cfg.normalization or cfg.normalization.record
    normalizer = RecordNormalizer(
        vocab_folder=vocab_folder,
        template_engine=embedder.template_engine,
        config=rec_norm_cfg,
    )
    store = AuthorityStore(
        cfg.storage.authority_db,
        cfg.storage.index_path,
        dimension=embedder.dimension,
        vector_engine_config=cfg.storage.vector_engine,
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
    enricher = MARCXMLEnricher(vocab_folder=vocab_folder)
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
    target_format = r_cfg.format
    if target_format == "auto":
        suffix = r_cfg.target_file.suffix.lower()
        if suffix in (".ttl", ".turtle", ".nt", ".n3", ".jsonld"):
            target_format = "bibframe"
        elif suffix in (".json", ".jsonl", ".ndjson"):
            try:
                with open(r_cfg.target_file, "rb") as probe_f:
                    sample = probe_f.read(2048).decode("utf-8", errors="replace")
                if "@context" in sample or "@graph" in sample or "http://id.loc.gov" in sample or "bffi" in sample:
                    target_format = "bibframe"
                else:
                    target_format = "bibra"
            except Exception:
                target_format = "bibra"
        elif suffix in (".rdf",):
            target_format = "bibframe"
        elif suffix in (".xml",):
            try:
                with open(r_cfg.target_file, "rb") as probe_f:
                    sample = probe_f.read(2048).decode("utf-8", errors="replace")
                if "RDF" in sample or "bibframe" in sample:
                    target_format = "bibframe"
                else:
                    target_format = "marcxml"
            except Exception:
                target_format = "marcxml"
        else:
            target_format = "marcxml"

    bf_profile: Optional[BIBFRAMESchemaProfile] = (
        get_profile(r_cfg.schema_profile) if r_cfg.schema_profile else None
    )

    is_stream_json = target_format == "bibra" or (
        target_format == "bibframe"
        and r_cfg.target_file.suffix.lower() in (".json", ".jsonl", ".ndjson", ".jsonld")
    )
    is_marcxml = target_format == "marcxml"

    out_f: Optional[IO[bytes]] = None
    if r_cfg.output_file:
        r_cfg.output_file.parent.mkdir(parents=True, exist_ok=True)
        if is_stream_json or is_marcxml:
            out_f = open(r_cfg.output_file, "wb")
            if is_marcxml:
                out_f.write(
                    b'<?xml version="1.0" encoding="UTF-8"?>\n'
                    b'<collection xmlns="http://www.loc.gov/MARC21/slim">\n'
                )

    active_graph: Optional[Graph] = None

    def _iter_records() -> Iterator[
        tuple[str, Any, dict[str, Any], int]
    ]:
        nonlocal active_graph
        if target_format == "bibra":
            for raw_rec_item, b_offset, bib_item in iter_bibra_records(r_cfg.target_file):
                yield "bibra", bib_item, raw_rec_item, b_offset
        elif target_format == "bibframe":
            suffix = r_cfg.target_file.suffix.lower()
            if suffix in (".json", ".jsonl", ".ndjson", ".jsonld"):
                for raw_rec_item, b_offset, doc_item in iter_jsonld_bibframe_records(
                    r_cfg.target_file, profile=bf_profile, source="target"
                ):
                    yield "bibframe_jsonld", doc_item, raw_rec_item, b_offset
            else:
                for raw_rec_item, b_offset, (g, nodes) in iter_bibframe_records(
                    r_cfg.target_file, profile=bf_profile, source="target"
                ):
                    active_graph = g
                    yield "bibframe_rdf", (g, nodes), raw_rec_item, b_offset
        else:
            for r_elem, b_offset in iter_marc_record_elements(r_cfg.target_file):
                raw_rec_item = parse_marc_record_elem(r_elem, source="target")
                yield "marcxml", r_elem, raw_rec_item, b_offset

    total_records = 0
    try:
        for rec_fmt, orig_payload, raw_rec, byte_offset in _iter_records():
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
                if rec_fmt == "bibra":
                    enriched_json = enrich_bibra_json(orig_payload, res, target_record=target_record)
                    out_f.write(json.dumps(enriched_json, ensure_ascii=False).encode("utf-8"))
                    out_f.write(b"\n")
                elif rec_fmt == "bibframe_jsonld":
                    enriched_doc = enrich_bibframe_jsonld(
                        orig_payload,
                        res,
                        profile=bf_profile,
                        target_record=target_record,
                        enrich=r_cfg.enrich,
                        fold_legacy=r_cfg.fold_legacy,
                    )
                    out_f.write(json.dumps(enriched_doc, ensure_ascii=False).encode("utf-8"))
                    out_f.write(b"\n")
                elif rec_fmt == "marcxml":
                    enricher.enrich_record_tree(
                        orig_payload,
                        res,
                        enrich=r_cfg.enrich,
                        fold_legacy=r_cfg.fold_legacy,
                    )
                    out_f.write(etree.tostring(orig_payload, encoding="utf-8", pretty_print=True))
                    out_f.write(b"\n")
            elif rec_fmt == "bibframe_rdf":
                g, nodes = orig_payload
                enrich_bibframe_graph(
                    g,
                    nodes,
                    res,
                    profile=bf_profile,
                    target_record=target_record,
                    enrich=r_cfg.enrich,
                    fold_legacy=r_cfg.fold_legacy,
                )

            tracker.update_progress(
                items_increment=1,
                bytes_processed=byte_offset,
            )
    finally:
        if out_f is not None:
            if is_marcxml:
                out_f.write(b"</collection>\n")
            out_f.close()
            console.print(f"[green]Wrote output to {r_cfg.output_file}[/green]")
        elif r_cfg.output_file and active_graph is not None:
            fmt = "turtle"
            suffix = r_cfg.output_file.suffix.lower()
            if suffix in (".ttl", ".turtle"):
                fmt = "turtle"
            elif suffix in (".rdf", ".xml"):
                fmt = "xml"
            elif suffix in (".nt", ".ntriples"):
                fmt = "nt"
            elif suffix in (".jsonld", ".json"):
                fmt = "json-ld"
            active_graph.serialize(destination=str(r_cfg.output_file), format=fmt)
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
    if cfg.ingest_vocab:
        ingest_vocab_cmd(config=config)
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
