"""Typer CLI interface exposing ingest, reconcile, status, and reset subcommands via unified --config."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import IO, Any, Iterator, Optional

import typer
from lxml import etree
from rdflib import Graph
from rich.console import Console
from rich.table import Table

from reconciliator.clusterer import CorpusClusterer
from reconciliator.config import (
    AppConfig,
    OAIIngestConfig,
    SkosmosDownloadConfig,
    configure_logging,
    load_config,
)
from reconciliator.curie import extract_wikidata_qid
from reconciliator.database import (
    is_container_present,
    is_container_running,
    reset_all_storage,
    start_postgres_container,
    stop_postgres_container,
)
from reconciliator.embedder import SentenceEmbedder
from reconciliator.indexer.ipc.client import DaemonLifecycleManager
from reconciliator.indexer.ipc.protocol import get_default_socket_path
from reconciliator.ingestion.oai import ingest_oai_pipeline
from reconciliator.ingestion.skosmos_downloader import SkosmosDownloader
from reconciliator.ingestion.vocab import ingest_vocab_file
from reconciliator.ingestion.wikidata import ingest_wikidata_pipeline
from reconciliator.loaders.bibframe import (
    BIBFRAMESchemaProfile,
    build_target_record_from_bibframe,
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
    build_target_record_from_marc,
    iter_marc_record_elements,
    parse_marc_record_elem,
)
from reconciliator.matcher import WorkMatcher
from reconciliator.models import (
    CorpusLinkage,
    ExpressionCluster,
    ManifestationCluster,
    MatchRecord,
    TaskProgress,
    WorkCluster,
)
from reconciliator.normalizers.blocking import (
    compute_author_key,
    compute_title_stem,
    compute_work_blocking_key,
)
from reconciliator.normalizers.record import RecordNormalizer
from reconciliator.progress import (
    ProgressTracker,
    format_task_table,
    get_task_progress,
    list_tasks,
    prune_tasks,
    render_status_json,
    render_vector_daemon_panel,
    run_telemetry_tui,
)
from reconciliator.reports import (
    AgentReportRow,
    ExpressionReportRow,
    ReportGenerator,
    SubjectReportRow,
    WorkReportRow,
)
from reconciliator.database import WEMIRepository
from reconciliator.utils import parse_duration_seconds
from reconciliator.vocab_folding import VocabFolder
from reconciliator.vocab_store import VocabStore
from reconciliator.vocabularies import detect_vocabulary
from reconciliator.xml_enricher import MARCXMLEnricher

app = typer.Typer(
    help="Reconciliator: High-Performance Finnish Bibliographic Authority Reconciliation Engine"
)
db_app = typer.Typer(help="Manage local PostgreSQL container via Podman")
app.add_typer(db_app, name="db")

console = Console()
logger = logging.getLogger(__name__)


@db_app.command("start")
def db_start_cmd(
    config: Path = typer.Option(
        Path("config.yaml"), "--config", "-c", help="Path to YAML configuration file"
    ),
    timeout: float = typer.Option(
        30.0, "--timeout", "-t", help="Timeout in seconds to wait for PostgreSQL readiness"
    ),
) -> None:
    """Start local containerized PostgreSQL instance using Podman."""
    cfg_path = _resolve_cli_config(config)
    cfg = load_config(cfg_path)
    pg = cfg.storage.postgres
    console.print(
        f"[bold blue]Starting PostgreSQL container '{pg.container_name}' on port {pg.port}...[/bold blue]"
    )
    try:
        start_postgres_container(
            host=pg.host,
            port=pg.port,
            database=pg.database,
            user=pg.user,
            password=pg.password,
            name=pg.container_name,
            image=pg.container_image,
            wait_ready=True,
            timeout_seconds=timeout,
        )
        console.print(
            f"[bold green]PostgreSQL is running and ready on {pg.host}:{pg.port}/{pg.database}[/bold green]"
        )
    except Exception as e:
        console.print(f"[bold red]Failed to start PostgreSQL container: {e}[/bold red]")
        raise typer.Exit(code=1)


@db_app.command("stop")
def db_stop_cmd(
    config: Path = typer.Option(
        Path("config.yaml"), "--config", "-c", help="Path to YAML configuration file"
    ),
) -> None:
    """Stop the local PostgreSQL container."""
    cfg_path = _resolve_cli_config(config)
    cfg = load_config(cfg_path)
    cname = cfg.storage.postgres.container_name
    console.print(f"[bold yellow]Stopping PostgreSQL container '{cname}'...[/bold yellow]")
    if stop_postgres_container(name=cname):
        console.print(f"[bold green]Container '{cname}' stopped.[/bold green]")
    else:
        console.print(f"[bold yellow]Container '{cname}' was not running.[/bold yellow]")


@db_app.command("status")
def db_status_cmd(
    config: Path = typer.Option(
        Path("config.yaml"), "--config", "-c", help="Path to YAML configuration file"
    ),
) -> None:
    """Check status of the local PostgreSQL container."""
    from rich.table import Table

    cfg_path = _resolve_cli_config(config)
    cfg = load_config(cfg_path)
    pg = cfg.storage.postgres
    cname = pg.container_name
    running = is_container_running(name=cname)
    present = is_container_present(name=cname)

    table = Table(title="PostgreSQL Container & Connection Status")
    table.add_column("Property", style="cyan")
    table.add_column("Value", style="green" if running else "yellow")

    table.add_row("Container Name", cname)
    table.add_row("Container Present", "Yes" if present else "No")
    table.add_row(
        "Container Running",
        "[bold green]Running[/bold green]" if running else "[bold red]Stopped[/bold red]",
    )
    table.add_row("Host / Port", f"{pg.host}:{pg.port}")
    table.add_row("Database", pg.database)
    table.add_row("User", pg.user)
    table.add_row("Database URL", pg.get_database_url())

    console.print(table)


def _resolve_cli_config(config: Path | str) -> Path:
    """Resolve config path, falling back to config.example.yaml if config.yaml is absent."""
    p = Path(config)
    if not p.exists() and p == Path("config.yaml") and Path("config.example.yaml").exists():
        return Path("config.example.yaml")
    return p


@app.command("reset")
def reset_cmd(
    config: Path = typer.Option(
        Path("config.yaml"), "--config", "-c", help="Path to YAML configuration file"
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Bypass interactive confirmation prompt"),
) -> None:
    """Reset all storage: PostgreSQL database tables, vector indices, and the vector daemon."""
    resolved_config = _resolve_cli_config(config)
    cfg = load_config(resolved_config)
    configure_logging(cfg.logging)

    if not yes:
        confirm = typer.confirm("Reset all databases and vector indices?")
        if not confirm:
            console.print("[yellow]Reset aborted.[/yellow]")
            raise typer.Abort()

    reset_all_storage(storage_config=cfg.storage)
    console.print("[green]Successfully reset all storage.[/green]")


@app.command("status")
def status_cmd(
    action: Optional[str] = typer.Argument(None, help="Optional subcommand or alias like 'watch'"),
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
    exit_when_done: bool = typer.Option(
        False,
        "--exit-when-done",
        "--until-done",
        help="Automatically exit watch monitor once all active tasks complete",
    ),
) -> None:
    """Query real-time task progress and telemetry from PostgreSQL."""
    resolved_config = _resolve_cli_config(config)
    cfg = load_config(resolved_config)
    configure_logging(cfg.logging)
    db_target = cfg.storage.database_url
    poll_interval = interval if interval is not None else cfg.telemetry.watch_interval_seconds
    is_watch = watch or (action is not None and action.lower() == "watch")

    daemon_status: Optional[dict[str, Any]] = None
    lifecycle: Optional[DaemonLifecycleManager] = None
    if cfg.storage.vector_engine.mode == "daemon":
        sock_p = cfg.storage.vector_engine.daemon.socket_path or get_default_socket_path(
            cfg.storage.data_dir
        )
        lifecycle = DaemonLifecycleManager(sock_p)
        daemon_status = lifecycle.query_status()

    def fetch_tasks() -> list[TaskProgress]:
        if task_id:
            t = get_task_progress(db_target, task_id)
            return [t] if t else []
        return list_tasks(db_target)

    if is_watch:
        if json_output:
            try:
                while True:
                    tasks = fetch_tasks()
                    d_status = lifecycle.query_status() if lifecycle else None
                    console.print(render_status_json(tasks, daemon_status=d_status))
                    if (
                        exit_when_done
                        and tasks
                        and all(t.status in ("completed", "failed") for t in tasks)
                    ):
                        break
                    time.sleep(poll_interval)
            except KeyboardInterrupt:
                pass
        else:
            run_telemetry_tui(
                fetch_tasks=fetch_tasks,
                db_path=db_target,
                interval=poll_interval,
                console=console,
                show_vector_daemon=cfg.telemetry.show_vector_daemon,
                get_daemon_status=lifecycle.query_status if lifecycle else None,
                exit_when_done=exit_when_done,
            )
    else:
        tasks = fetch_tasks()
        if json_output:
            console.print(render_status_json(tasks, daemon_status=daemon_status))
        else:
            if cfg.telemetry.show_vector_daemon and cfg.storage.vector_engine.mode == "daemon":
                console.print(render_vector_daemon_panel(daemon_status))
            table = format_task_table(tasks)
            console.print(table)

        if prune_completed:
            deleted = prune_tasks(db_target, status="completed")
            console.print(f"[green]Pruned {deleted} completed task(s).[/green]")


daemon_app = typer.Typer(name="daemon", help="Manage singleton vector index service.")
app.add_typer(daemon_app, name="daemon")


@daemon_app.command("status")
def daemon_status_cmd(
    config: Path = typer.Option(
        Path("config.yaml"), "--config", "-c", help="Path to YAML configuration file"
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit status as JSON"),
) -> None:
    """Query real-time health and active indices of the vector daemon."""
    resolved_config = _resolve_cli_config(config)
    cfg = load_config(resolved_config)
    sock_p = cfg.storage.vector_engine.daemon.socket_path or get_default_socket_path(
        cfg.storage.data_dir
    )
    lifecycle = DaemonLifecycleManager(sock_p, config_path=resolved_config)
    status = lifecycle.query_status()
    if json_output:
        import json as _json

        console.print(
            _json.dumps(status or {"status": "stopped", "socket_path": str(sock_p)}, indent=2)
        )
    else:
        console.print(render_vector_daemon_panel(status))


@daemon_app.command("start")
def daemon_start_cmd(
    config: Path = typer.Option(
        Path("config.yaml"), "--config", "-c", help="Path to YAML configuration file"
    ),
) -> None:
    """Start the vector daemon in the background if not already running."""
    resolved_config = _resolve_cli_config(config)
    cfg = load_config(resolved_config)
    sock_p = cfg.storage.vector_engine.daemon.socket_path or get_default_socket_path(
        cfg.storage.data_dir
    )
    lifecycle = DaemonLifecycleManager(sock_p, config_path=resolved_config, auto_start=True)
    if lifecycle.is_running():
        console.print(f"[yellow]Vector daemon is already running on {sock_p}.[/yellow]")
        return

    console.print(f"[cyan]Starting vector daemon on {sock_p}...[/cyan]")
    lifecycle.ensure_running()
    status = lifecycle.query_status()
    pid = status.get("pid", "?") if status else "?"
    console.print(f"[green]Vector daemon successfully started (PID {pid}).[/green]")


@daemon_app.command("stop")
def daemon_stop_cmd(
    config: Path = typer.Option(
        Path("config.yaml"), "--config", "-c", help="Path to YAML configuration file"
    ),
) -> None:
    """Gracefully flush indices and terminate the vector daemon."""
    resolved_config = _resolve_cli_config(config)
    cfg = load_config(resolved_config)
    sock_p = cfg.storage.vector_engine.daemon.socket_path or get_default_socket_path(
        cfg.storage.data_dir
    )
    lifecycle = DaemonLifecycleManager(sock_p, config_path=resolved_config)
    if not lifecycle.is_running():
        console.print(f"[yellow]Vector daemon is not running on {sock_p}.[/yellow]")
        return

    console.print(f"[cyan]Stopping vector daemon on {sock_p}...[/cyan]")
    success = lifecycle.shutdown()
    if success:
        console.print("[green]Vector daemon stopped successfully.[/green]")
    else:
        console.print("[red]Failed to stop vector daemon cleanly.[/red]")


@daemon_app.command("restart")
def daemon_restart_cmd(
    config: Path = typer.Option(
        Path("config.yaml"), "--config", "-c", help="Path to YAML configuration file"
    ),
) -> None:
    """Restart the vector daemon."""
    resolved_config = _resolve_cli_config(config)
    daemon_stop_cmd(config=resolved_config)
    time.sleep(0.5)
    daemon_start_cmd(config=resolved_config)


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
    resolved_config = _resolve_cli_config(config)
    cfg = load_config(resolved_config)
    configure_logging(cfg.logging)
    db_target = cfg.storage.database_url

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
            "Are you sure you want to prune task telemetry records matching the criteria?"
        )
        if not confirm:
            console.print("[yellow]Prune aborted.[/yellow]")
            raise typer.Abort()

    deleted = prune_tasks(
        db_path=db_target,
        task_id=task_id,
        status=status,
        older_than_seconds=older_than_seconds,
        keep_last=keep_last,
        include_running=include_running,
    )
    console.print(f"[green]Successfully pruned {deleted} task(s).[/green]")


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
        db_path=cfg.storage.database_url,
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
            f"[cyan][{src_name}] Ingesting downloaded vocabularies into PostgreSQL database...[/cyan]"
        )
        vocab_store = VocabStore(cfg.storage.database_url)
        ingest_tracker = ProgressTracker(
            db_path=cfg.storage.database_url,
            task_id=f"vocab-ingest-{src_name}-{int(time.time())}",
            task_type="vocab_ingest",
            heartbeat_interval=cfg.telemetry.heartbeat_interval_seconds,
        )
        c_count, m_count = ingest_vocab_file(target_dir, vocab_store, tracker=ingest_tracker)
        console.print(
            f"[green][{src_name}] Successfully ingested {c_count} concepts and {m_count} mappings into database.[/green]"
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
    configure_logging(cfg.logging)

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
    resolved_config = _resolve_cli_config(config)
    cfg = load_config(resolved_config)
    configure_logging(cfg.logging)
    if not cfg.ingest_vocab:
        console.print("[red]No ingest_vocab section found in configuration.[/red]")
        raise typer.Exit(code=1)

    vocab_store = VocabStore(cfg.storage.database_url)
    tracker = ProgressTracker(
        db_path=cfg.storage.database_url,
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
    workers: Optional[int] = typer.Option(
        None, "--workers", "-w", help="Override number of harvest workers"
    ),
    slice_days: Optional[int] = typer.Option(
        None, "--slice-days", help="Override the width in days of each concurrent date slice"
    ),
    max_concurrent_requests: Optional[int] = typer.Option(
        None, "--max-concurrent-requests", help="Override maximum concurrent OAI requests"
    ),
    prefetch: Optional[bool] = typer.Option(
        None, "--prefetch/--no-prefetch", help="Override background page prefetching"
    ),
    skip_existing: bool = typer.Option(
        False,
        "--skip-existing",
        help="Skip individual records already stored for this source",
    ),
    restart: bool = typer.Option(
        False,
        "--restart",
        help="Ignore the saved harvest checkpoint and re-harvest the source from the start",
    ),
) -> None:
    """Harvest or read authority records from configured OAI-PMH/XML sources.

    An interrupted OAI-PMH harvest resumes from its saved checkpoint by default,
    and a source that previously completed is harvested incrementally. Use
    --restart to discard that checkpoint. --skip-existing filters individual
    records instead, and is independent of harvest checkpointing.
    """
    resolved_config = _resolve_cli_config(config)
    cfg = load_config(resolved_config)
    configure_logging(cfg.logging)
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

    # Decomposition folds legacy subject headings and normalizes relator codes, so
    # ingest needs the same vocabulary store and relator mappings reconcile uses.
    vocab_store = VocabStore(cfg.storage.database_url)
    relator_mappings = getattr(cfg.normalization.record, "relator_mappings", None)
    for src_name, src_cfg in targets.items():
        if isinstance(from_date, str):
            src_cfg.from_date = from_date
        if isinstance(until_date, str):
            src_cfg.until_date = until_date
        if isinstance(workers, int):
            src_cfg.workers = workers
        if isinstance(slice_days, int):
            src_cfg.slice_days = slice_days
        if isinstance(max_concurrent_requests, int):
            src_cfg.max_concurrent_requests = max_concurrent_requests
        if prefetch is not None:
            src_cfg.prefetch = prefetch

        tracker = ProgressTracker(
            db_path=cfg.storage.database_url,
            task_id=f"oai-ingest-{src_name}-{int(time.time())}",
            task_type="oai_pmh_harvest",
            heartbeat_interval=cfg.telemetry.heartbeat_interval_seconds,
        )

        console.print(f"[cyan]Ingesting authority records from source '{src_name}'...[/cyan]")
        wemi_repo = _wemi_repo(cfg)
        total = asyncio.run(
            ingest_oai_pipeline(
                source_name=src_name,
                config=src_cfg,
                store=wemi_repo,
                tracker=tracker,
                skip_existing=bool(skip_existing) if isinstance(skip_existing, bool) else False,
                relator_mappings=relator_mappings,
                vocab_store=vocab_store,
                strip_isbd_punctuation=cfg.record_normalization.strip_isbd_punctuation,
                isbd_punctuation=cfg.record_normalization.isbd_punctuation,
                resume=not (bool(restart) if isinstance(restart, bool) else False),
                constituents_default_profile=cfg.constituents.profile,
                constituents_default_mode=cfg.constituents.mode,
                config_dir=cfg.config_dir,
            )
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
    resolved_config = _resolve_cli_config(config)
    cfg = load_config(resolved_config)
    configure_logging(cfg.logging)
    if not cfg.ingest_wikidata:
        console.print("[red]No ingest_wikidata section found in configuration.[/red]")
        raise typer.Exit(code=1)

    tracker = ProgressTracker(
        db_path=cfg.storage.database_url,
        task_id=f"wikidata-ingest-{int(time.time())}",
        task_type="wikidata_ingest",
        heartbeat_interval=cfg.telemetry.heartbeat_interval_seconds,
    )

    wemi_repo = _wemi_repo(cfg)
    console.print("[cyan]Streaming Wikidata entities into WEMI authority repository...[/cyan]")
    total = ingest_wikidata_pipeline(cfg.ingest_wikidata, wemi_repo, tracker=tracker)
    console.print(f"[green]Successfully ingested {total} records from Wikidata.[/green]")


def _wemi_repo(cfg: Any, embedder: Optional[Any] = None) -> WEMIRepository:
    """Build the repository the way every command should.

    Passing an `embedder` wires the work and manifestation vector indexes. Every
    CLI command used to construct the repository with `db_path` and `fts_config`
    only, which left `work_indexer` as None and so made the matcher's tier-2
    dense branch unreachable in production - every match fell through to FTS
    while the embedding configuration, engine selection and IPC daemon all sat
    there serving nothing.

    The embedder is what supplies the dimension; there is no configured value, so
    a command with no embedder gets no indexes rather than a guessed dimension
    that would not match the vectors already stored. Index files that do not
    exist yet are created empty, and both the matcher and the clusterer guard on
    `total_vectors == 0`, so wiring them before `build-index` has run is safe.
    """
    if embedder is None:
        return WEMIRepository(cfg.storage.database_url, fts_config=cfg.storage.fts)
    return WEMIRepository(
        cfg.storage.database_url,
        fts_config=cfg.storage.fts,
        work_index_path=cfg.storage.work_index,
        manifestation_index_path=cfg.storage.manifestation_index,
        dimension=embedder.dimension,
        vector_engine_config=cfg.storage.vector_engine,
    )


def _ingest_corpus_as_wemi(
    repo: Any,
    records: Any,
    source_name: str,
    profile: Optional[BIBFRAMESchemaProfile] = None,
    strip_isbd_punctuation: bool = True,
    isbd_punctuation: str = " ./,;:",
    batch_size: int = 500,
) -> int:
    """Persist the corpus being reconciled as ordinary WEMI rows.

    Replaces the `target_records` staging path. Every ingested record, corpus or
    authority, becomes works/expressions/manifestations plus identifiers and
    entity_keys; `authoritative: false` on the source is what keeps a corpus from
    being matched against as if it were authority data.
    """
    from reconciliator.loaders.bibframe import bibframe_entity_keys, decompose_bibframe_work
    from reconciliator.loaders.marcxml import decompose_marc_wemi

    total = 0
    batch: dict[str, list[Any]] = {}

    def _flush() -> None:
        if not batch.get("works"):
            return
        repo.insert_wemi_batch(
            works=batch.get("works") or [],
            expressions=batch.get("expressions") or [],
            manifestations=batch.get("manifestations") or [],
            agents=batch.get("agents") or [],
            relationships=batch.get("relationships") or [],
            identifiers=batch.get("identifiers") or [],
            manifestation_expressions=batch.get("associations") or [],
            contributions=batch.get("contributions") or [],
            entity_keys=batch.get("entity_keys") or [],
            entity_subjects=batch.get("entity_subjects") or [],
            record_origin=source_name,
        )
        batch.clear()

    def _stage(key: str, values: Any) -> None:
        batch.setdefault(key, []).extend(values)

    for rec_fmt, orig_payload, _raw_rec, _offset in records:
        keys: list[Any] = []
        subjs: list[Any] = []
        try:
            if rec_fmt == "marcxml":
                w, e, m, itms, agts, rels, idents, assoc, contribs = decompose_marc_wemi(
                    orig_payload,
                    source=source_name,
                    record_origin=source_name,
                    strip_isbd_punctuation=strip_isbd_punctuation,
                    isbd_punctuation=isbd_punctuation,
                    key_sink=keys,
                    subject_sink=subjs,
                )
            elif rec_fmt in ("bibframe_rdf", "bibframe_jsonld"):
                if rec_fmt == "bibframe_rdf":
                    g, nodes = orig_payload
                    node = nodes[0] if nodes else None
                else:
                    g = Graph()
                    g.parse(data=json.dumps(orig_payload), format="json-ld")
                    node = None
                w, e, m, itms, agts, rels, idents, assoc, contribs = decompose_bibframe_work(
                    g=g, instance_node=node, profile=profile, source=source_name, subject_sink=subjs
                )
                keys = bibframe_entity_keys(m, w, agts, idents)
            else:
                continue
        except Exception:
            continue

        _stage("works", [w])
        _stage("expressions", [e])
        _stage("manifestations", [m])
        _stage("items", itms)
        _stage("agents", agts)
        _stage("relationships", rels)
        _stage("identifiers", idents)
        _stage("associations", assoc)
        _stage("contributions", contribs)
        _stage("entity_keys", keys)
        _stage("entity_subjects", subjs)
        total += 1
        if total % batch_size == 0:
            _flush()

    _flush()
    return total


def _to_target_record(
    rec_fmt: str,
    orig_payload: Any,
    raw_rec: dict[str, Any],
    profile: Optional[BIBFRAMESchemaProfile] = None,
) -> MatchRecord:
    """Decompose or map raw bibliographic items into normalized MatchRecord instances."""
    try:
        if rec_fmt == "marcxml":
            return build_target_record_from_marc(orig_payload, source="target")
        elif rec_fmt == "bibframe_rdf":
            g, nodes = orig_payload
            instance_node = nodes[0] if nodes else None
            return build_target_record_from_bibframe(
                g, instance_node=instance_node, profile=profile, source="target"
            )
        elif rec_fmt == "bibframe_jsonld":
            g = Graph()
            g.parse(data=json.dumps(orig_payload), format="json-ld")
            return build_target_record_from_bibframe(g, profile=profile, source="target")
    except Exception:
        pass

    source_id = raw_rec.get("source_id") or "unknown_target"
    title = raw_rec.get("title") or ""
    agents = raw_rec.get("agents") or []
    author_name = agents[0].get("name") if agents else None
    author_surname = agents[0].get("surname") if agents else None
    author_key = compute_author_key(author_name, author_surname)
    title_stem = compute_title_stem(title)
    work_blocking_key = compute_work_blocking_key(author_key, title_stem)

    if isinstance(orig_payload, (dict, list)):
        raw_bytes = json.dumps(orig_payload, ensure_ascii=False).encode("utf-8")
    elif hasattr(orig_payload, "tag"):
        raw_bytes = etree.tostring(orig_payload, encoding="utf-8")
    else:
        raw_bytes = str(orig_payload).encode("utf-8")

    return MatchRecord(
        record_id=source_id,
        format=rec_fmt,
        work_type="text",
        title=title,
        title_stem=title_stem,
        author_key=author_key,
        work_blocking_key=work_blocking_key,
        raw_payload=raw_bytes,
    )


def _make_agent_report_row(
    target_record: Any,
    agent: Any,
    field_tag: str,
    field_occurrence: int,
    uri: str = "",
    action: str = "unmatched",
    is_novel: bool = False,
    discovered_map: Optional[dict[str, Any]] = None,
    accepted_candidates: Optional[list[Any]] = None,
    match_tier: str = "TIER_0",
    match_score: float = 0.0,
    dense_score: Optional[float] = None,
    rerank_score: Optional[float] = None,
) -> AgentReportRow:
    target_isbn = (
        target_record.identifiers.isbn[0]
        if getattr(target_record, "identifiers", None) and target_record.identifiers.isbn
        else None
    )
    target_role = (
        ", ".join(agent.role_counts.keys()) if getattr(agent, "role_counts", None) else None
    )

    if not uri:
        return AgentReportRow(
            target_record_id=target_record.source_id,
            target_title=target_record.title,
            target_year=target_record.publication_year,
            target_isbn=target_isbn,
            field_tag=field_tag,
            field_occurrence=field_occurrence,
            subfield_code="0",
            target_heading=agent.name,
            target_role=target_role,
            uri="",
            vocabulary="",
            authority_label=None,
            authority_record_id="",
            authority_source="",
            match_tier=match_tier,
            match_score=match_score,
            dense_score=dense_score,
            rerank_score=rerank_score,
            agent_name_score=None,
            action=action,
            is_novel=is_novel,
            dates=agent.dates,
            fuller_name=agent.fuller_name,
            birth_year=agent.birth_year,
            death_year=agent.death_year,
        )

    subfield_code = "1" if "wikidata.org" in uri.lower() else "0"
    disc_uri = discovered_map.get(uri) if discovered_map else None
    cand_source_id = (
        disc_uri.source_record_id
        if disc_uri
        else (accepted_candidates[0].source_id if accepted_candidates else "")
    )
    cand_source = (
        disc_uri.source
        if disc_uri
        else (accepted_candidates[0].source if accepted_candidates else "")
    )
    agent_name_score = disc_uri.alignment_score if disc_uri else 100.0

    return AgentReportRow(
        target_record_id=target_record.source_id,
        target_title=target_record.title,
        target_year=target_record.publication_year,
        target_isbn=target_isbn,
        field_tag=field_tag,
        field_occurrence=field_occurrence,
        subfield_code=subfield_code,
        target_heading=agent.name,
        target_role=target_role,
        uri=uri,
        vocabulary=detect_vocabulary(uri) or "OTHER",
        authority_label=agent.name,
        authority_record_id=cand_source_id,
        authority_source=cand_source,
        match_tier=match_tier,
        match_score=match_score,
        dense_score=dense_score,
        rerank_score=rerank_score,
        agent_name_score=agent_name_score,
        action=action,
        is_novel=is_novel,
    )


@app.command("reconcile")
def reconcile_cmd(
    config: Path = typer.Option(
        Path("config.yaml"), "--config", "-c", help="Path to YAML configuration file"
    ),
) -> None:
    """Reconcile target MARCXML records offline, enrich URIs, and write dual TSV reports."""
    resolved_config = _resolve_cli_config(config)
    cfg = load_config(resolved_config)
    configure_logging(cfg.logging)
    if not cfg.reconcile:
        console.print("[red]No reconcile section found in configuration.[/red]")
        raise typer.Exit(code=1)

    r_cfg = cfg.reconcile
    if not r_cfg.target_file.exists():
        console.print(f"[red]Target MARCXML file not found: {r_cfg.target_file}[/red]")
        raise typer.Exit(code=1)

    vocab_store = VocabStore(cfg.storage.database_url)
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
    # The embedder is passed so the work and manifestation indexes are live:
    # without them the matcher's dense tier cannot fire at all.
    wemi_repo = _wemi_repo(cfg, embedder=embedder)

    # Matching is about to run, and it depends on `record_origin` agreeing with
    # the `sources` registry. When they disagree the failure is silent and total:
    # the allow-list filters out every candidate, so nothing matches and nothing
    # errors. Report it here rather than leaving it to be debugged from empty
    # output.
    for finding in wemi_repo.check_source_registry():
        console.print(f"[yellow]Warning: {finding}[/yellow]")

    # Tier 3 standalone agent fallback needs the dedicated agent index; it is
    # optional, so only load it when build-agent-index has produced one.
    agent_indexer = None
    if r_cfg.fallback_agent_reconciliation and cfg.storage.agent_index:
        from reconciliator.indexer import create_vector_engine

        try:
            agent_indexer = create_vector_engine(
                cfg.storage.agent_index,
                dimension=embedder.dimension,
                config=cfg.storage.vector_engine,
            )
            if getattr(agent_indexer, "total_vectors", 0) == 0:
                agent_indexer = None
        except Exception as exc:
            logger.warning("Agent index unavailable, skipping agent fallback: %s", exc)
            agent_indexer = None

    work_matcher = WorkMatcher(
        wemi_repo=wemi_repo,
        embedder=embedder,
        config=r_cfg,
        agent_indexer=agent_indexer,
    )
    enricher = MARCXMLEnricher(vocab_folder=vocab_folder)
    reporter = ReportGenerator(config=cfg)

    tracker = ProgressTracker(
        db_path=cfg.storage.database_url,
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
                if (
                    "@context" in sample
                    or "@graph" in sample
                    or "http://id.loc.gov" in sample
                    or "bffi" in sample
                ):
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

    def _iter_records() -> Iterator[tuple[str, Any, dict[str, Any], int]]:
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
                raw_rec_item = parse_marc_record_elem(
                    r_elem,
                    source="target",
                    supported_subject_vocabularies=getattr(
                        vocab_cfg, "supported_subject_vocabularies", None
                    ),
                    legacy_to_modern=getattr(vocab_cfg, "legacy_to_modern", None),
                    relator_mappings=getattr(rec_norm_cfg, "relator_mappings", None),
                    vocab_store=vocab_store,
                )
                yield "marcxml", r_elem, raw_rec_item, b_offset

    linkage_map: dict[str, CorpusLinkage] = {}
    work_cluster_map: dict[str, WorkCluster] = {}
    expression_cluster_map: dict[str, ExpressionCluster] = {}
    manifestation_cluster_map: dict[str, ManifestationCluster] = {}

    if r_cfg.cluster and r_cfg.cluster.enabled:
        console.print(
            "[cyan]Staging target records and running intra-corpus graph clusterer...[/cyan]"
        )
        corpus_repo = wemi_repo

        # The corpus is ingested exactly like any other source: WEMI rows carrying
        # its own record_origin, registered as non-authoritative. There is no
        # staging table, so clustering and reconciliation read the same store.
        corpus_source = r_cfg.corpus_source_name or r_cfg.target_file.stem
        corpus_repo.register_source(corpus_source, is_authoritative=False)
        staged = _ingest_corpus_as_wemi(
            corpus_repo,
            _iter_records(),
            corpus_source,
            bf_profile,
            strip_isbd_punctuation=cfg.record_normalization.strip_isbd_punctuation,
            isbd_punctuation=cfg.record_normalization.isbd_punctuation,
        )

        if staged:
            clusterer = CorpusClusterer(
                corpus_repo=corpus_repo,
                wemi_repo=wemi_repo,
                matcher=work_matcher,
                config=r_cfg.cluster,
            )
            clusterer.cluster_corpus()
            for lnk in corpus_repo.get_all_corpus_linkages():
                linkage_map[lnk.record_id] = lnk
            for wc in corpus_repo.get_all_work_clusters():
                work_cluster_map[wc.cluster_id] = wc
            for ec in corpus_repo.get_all_expression_clusters():
                expression_cluster_map[ec.cluster_id] = ec
            for mc in corpus_repo.get_all_manifestation_clusters():
                manifestation_cluster_map[mc.cluster_id] = mc

    total_records = 0
    try:
        for rec_fmt, orig_payload, raw_rec, byte_offset in _iter_records():
            total_records += 1
            target_record = normalizer.normalize(raw_rec)

            # One construction, one match. Both halves of the output - the agent
            # authority URIs and the WEMI report columns - derive from the same
            # resolution. They used to come from two separately built records, of
            # which the one driving agent reconciliation knew neither the work
            # type nor any identifier.
            target_wemi = _to_target_record(rec_fmt, orig_payload, raw_rec, bf_profile)
            wemi_res = work_matcher.match_target(target_wemi)

            res = work_matcher.resolve_agents(wemi_res, target_record)
            if res.accepted_candidates:
                reporter.matched_records += 1

            # Record agent report rows
            best_match = res.matches[0] if res.matches else None
            match_tier = best_match.match_tier if best_match else "unmatched"
            match_score = best_match.match_score if best_match else 0.0
            dense_score = best_match.dense_score if best_match else None
            rerank_score = best_match.rerank_score if best_match else None

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
                    reporter.add_agent_row(
                        _make_agent_report_row(
                            target_record,
                            agent,
                            field_tag,
                            idx + 1,
                            uri=uri,
                            action=action,
                            is_novel=is_novel,
                            discovered_map=discovered_map,
                            accepted_candidates=res.accepted_candidates,
                            match_tier=match_tier,
                            match_score=match_score,
                            dense_score=dense_score,
                            rerank_score=rerank_score,
                        )
                    )

                for uri in suppressed:
                    reporter.add_agent_row(
                        _make_agent_report_row(
                            target_record,
                            agent,
                            field_tag,
                            idx + 1,
                            uri=uri,
                            action="suppressed_by_priority",
                            is_novel=True,
                            discovered_map=discovered_map,
                            accepted_candidates=res.accepted_candidates,
                            match_tier=match_tier,
                            match_score=match_score,
                            dense_score=dense_score,
                            rerank_score=rerank_score,
                        )
                    )

                # An agent with no candidate URI is left untouched in the record,
                # but must still appear in the report: without a row there is no
                # way to tell an agent that failed to link from one never seen,
                # and no way to measure link coverage over a corpus.
                if not selected and not suppressed:
                    reporter.add_agent_row(
                        _make_agent_report_row(
                            target_record,
                            agent,
                            field_tag,
                            idx + 1,
                            action="unmatched",
                            is_novel=False,
                            match_tier=match_tier,
                            match_score=match_score,
                            dense_score=dense_score,
                            rerank_score=rerank_score,
                        )
                    )

            # Record subject report rows
            all_subjects = target_record.subjects.all_items()
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
                        language=target_record.language or cfg.linguistic.default_language,
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

            # Modern WEMI resolution and reporting, from the single match above.
            linkage = linkage_map.get(target_wemi.record_id)
            w_cluster = work_cluster_map.get(linkage.work_cluster_id) if linkage else None
            e_cluster = (
                expression_cluster_map.get(linkage.expression_cluster_id) if linkage else None
            )
            m_cluster = (
                manifestation_cluster_map.get(linkage.manifestation_cluster_id) if linkage else None
            )

            # Add WorkReportRow
            is_clustered = linkage is not None and linkage.work_cluster_id is not None

            # Precedence must match the enricher's, which lets the work cluster's
            # canonical URI win: every member of a work cluster has to carry the
            # same work URI, and the report has to state the URI actually written.
            # Sibling records can match different authority sources, so the
            # record's own resolution is kept alongside as provenance.
            record_level_w_uri = wemi_res.work_uri if wemi_res else None
            matched_w_uri = (
                (w_cluster.canonical_work_uri if w_cluster else None)
                or (linkage.final_work_uri if linkage else None)
                or record_level_w_uri
            )
            qid = None
            if wemi_res and wemi_res.matched_work and wemi_res.matched_work.source == "wikidata":
                qid = wemi_res.matched_work.source_id
            if not qid and matched_w_uri:
                qid = extract_wikidata_qid(matched_w_uri)
            w_action = (
                "injected"
                if (r_cfg.enrich and matched_w_uri)
                else ("proposed" if matched_w_uri else "unmatched")
            )
            if is_clustered and not (wemi_res and wemi_res.matched_work):
                w_action = (
                    "hoisted"
                    if (
                        w_cluster
                        and w_cluster.canonical_work_uri
                        and not w_cluster.canonical_work_uri.startswith("urn:reconciliator:")
                    )
                    else "minted"
                )

            reporter.add_work_row(
                WorkReportRow(
                    target_record_id=target_wemi.record_id,
                    work_type=target_wemi.work_type,
                    target_title=target_wemi.title,
                    target_author=target_wemi.author_key,
                    target_year=target_wemi.year,
                    work_blocking_key=target_wemi.work_blocking_key,
                    matched_work_uri=matched_w_uri,
                    wikidata_qid=qid,
                    match_tier=wemi_res.match_tier if wemi_res else "unmatched",
                    funnel_used=wemi_res.funnel_used if wemi_res else "none",
                    confidence_score=wemi_res.confidence_score if wemi_res else 0.0,
                    is_clustered=is_clustered,
                    cluster_id=linkage.work_cluster_id if linkage else None,
                    action=w_action,
                    record_level_work_uri=record_level_w_uri,
                )
            )

            # Add ExpressionReportRow
            matched_e_uri = (
                (wemi_res.expression_uri if wemi_res else None)
                or (linkage.final_expression_uri if linkage else None)
                or (e_cluster.canonical_expression_uri if e_cluster else None)
            )
            e_action = (
                "injected"
                if (r_cfg.enrich and matched_e_uri)
                else ("proposed" if matched_e_uri else "unmatched")
            )
            e_content_type = (
                (
                    wemi_res.matched_expression.content_type
                    if (wemi_res and wemi_res.matched_expression)
                    else None
                )
                or (e_cluster.content_type if e_cluster else None)
                or "text"
            )
            e_lang = (
                (
                    wemi_res.matched_expression.language
                    if (
                        wemi_res
                        and wemi_res.matched_expression
                        and wemi_res.matched_expression.language
                    )
                    else None
                )
                or (e_cluster.language if e_cluster else None)
                or target_wemi.language
                or "und"
            )
            e_version = (
                (
                    wemi_res.matched_expression.version_designation
                    if (wemi_res and wemi_res.matched_expression)
                    else None
                )
                or (e_cluster.version_designation if e_cluster else None)
                or target_wemi.volume_designation
            )

            reporter.add_expression_row(
                ExpressionReportRow(
                    target_record_id=target_wemi.record_id,
                    work_uri=matched_w_uri,
                    expression_uri=matched_e_uri,
                    content_type=e_content_type,
                    language=e_lang,
                    version_designation=e_version,
                    translators=None,
                    performers=None,
                    match_tier=wemi_res.match_tier if wemi_res else "unmatched",
                    confidence_score=wemi_res.confidence_score if wemi_res else 0.0,
                    cluster_id=linkage.expression_cluster_id if linkage else None,
                    action=e_action,
                )
            )

            # Mutate / serialize and stream write if output_file requested
            if out_f is not None:
                if rec_fmt == "bibra":
                    enriched_json = enrich_bibra_json(
                        orig_payload, res, target_record=target_record
                    )
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
                        wemi_result=wemi_res,
                        linkage=linkage,
                        work_cluster=w_cluster,
                        expression_cluster=e_cluster,
                        manifestation_cluster=m_cluster,
                        enrichment_config=r_cfg.enrichment_toggles,
                    )
                    out_f.write(json.dumps(enriched_doc, ensure_ascii=False).encode("utf-8"))
                    out_f.write(b"\n")
                elif rec_fmt == "marcxml":
                    sibs = (
                        corpus_repo.get_sibling_manifestation_record_ids(target_wemi.record_id)
                        if is_clustered and corpus_repo
                        else []
                    )
                    enricher.enrich_record_tree(
                        orig_payload,
                        res,
                        enrich=r_cfg.enrich,
                        fold_legacy=r_cfg.fold_legacy,
                        wemi_result=wemi_res,
                        linkage=linkage,
                        work_cluster=w_cluster,
                        expression_cluster=e_cluster,
                        manifestation_cluster=m_cluster,
                        target_record=target_wemi,
                        enrichment_config=r_cfg.enrichment_toggles,
                        sibling_ids=sibs,
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
                    wemi_result=wemi_res,
                    linkage=linkage,
                    work_cluster=w_cluster,
                    expression_cluster=e_cluster,
                    manifestation_cluster=m_cluster,
                    enrichment_config=r_cfg.enrichment_toggles,
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

    if r_cfg.work_reports:
        reporter.write_work_report(r_cfg.work_reports.detailed_file)
        if r_cfg.work_reports.summary_file:
            reporter.write_wemi_summary_report(r_cfg.work_reports.summary_file)
    if r_cfg.expression_reports:
        reporter.write_expression_report(r_cfg.expression_reports.detailed_file)
        if r_cfg.expression_reports.summary_file and (
            not r_cfg.work_reports
            or r_cfg.expression_reports.summary_file != r_cfg.work_reports.summary_file
        ):
            reporter.write_wemi_summary_report(r_cfg.expression_reports.summary_file)

    tracker.complete_task(
        metadata={
            "matched_records": reporter.matched_records,
            "total_records": reporter.total_records,
        }
    )
    console.print("[green]Reconciliation complete! Emitted dual TSV report sets.[/green]")


@app.command("consolidate-works")
def consolidate_works_cmd(
    config: Path = typer.Option(
        Path("config.yaml"), "--config", "-c", help="Path to YAML configuration file"
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Report the groups that would be formed without writing"
    ),
    reset: bool = typer.Option(
        False, "--reset", help="Discard existing consolidation before running"
    ),
) -> None:
    """Consolidate work rows that different sources hold for the same work.

    Each source mints its own work URI, so one work held by three catalogues is
    three rows. This elects one canonical row per equivalence group and records
    same_work_as edges explaining why. Nothing is deleted, and the run is
    repeatable after ingesting further sources.
    """
    from reconciliator.consolidation import WorkConsolidator

    resolved_config = _resolve_cli_config(config)
    cfg = load_config(resolved_config)
    configure_logging(cfg.logging)

    # Typer's OptionInfo defaults are truthy, so guard for direct invocation.
    dry_run = dry_run if isinstance(dry_run, bool) else False
    reset = reset if isinstance(reset, bool) else False

    wemi_repo = _wemi_repo(cfg)
    if reset:
        wemi_repo.clear_canonical_works()
        console.print("[yellow]Cleared existing work consolidation.[/yellow]")

    consolidator = WorkConsolidator(repo=wemi_repo, config=cfg.consolidation)
    if not cfg.consolidation.enabled:
        console.print("[yellow]Work consolidation is disabled in configuration.[/yellow]")
        raise typer.Exit(code=0)

    signatures = consolidator.load_signatures()
    console.print(f"[cyan]Comparing {len(signatures)} authority works...[/cyan]")
    result = consolidator.consolidate(dry_run=dry_run)

    if not result.groups:
        console.print("[green]No cross-source duplicates found.[/green]")
        return

    table = Table(title="Work consolidation groups")
    table.add_column("Canonical work", overflow="fold")
    table.add_column("Folded in", overflow="fold")
    table.add_column("Evidence")
    for members in result.groups:
        canonical = result.canonical_by_work[members[0]]
        others = [m for m in members if m != canonical]
        evidence = sorted(
            {ev for (a, b), ev in result.evidence.items() if a in members and b in members}
        )
        table.add_row(
            signatures[canonical].work_uri,
            "\n".join(signatures[m].work_uri for m in others),
            ", ".join(evidence),
        )
    console.print(table)

    verb = "Would consolidate" if dry_run else "Consolidated"
    console.print(
        f"[green]{verb} {result.consolidated_works} work rows into "
        f"{len(result.groups)} canonical works"
        + ("" if dry_run else f" ({result.edges_written} same_work_as edges).")
        + "[/green]"
    )
    if dry_run:
        console.print("[yellow]Dry run: nothing was written.[/yellow]")


@app.command("cluster-agents")
def cluster_agents_cmd(
    config: Path = typer.Option(
        Path("config.yaml"), "--config", "-c", help="Path to YAML configuration file"
    ),
    threshold: Optional[float] = typer.Option(
        None, "--threshold", help="Clustering similarity threshold override"
    ),
) -> None:
    """Cluster agent entities across sources into canonical agent clusters."""
    from reconciliator.agent_clusterer import AgentClusterer

    resolved_config = _resolve_cli_config(config)
    cfg = load_config(resolved_config)
    configure_logging(cfg.logging)

    cluster_config = cfg.agent_clustering
    if threshold is not None:
        cluster_config.similarity_threshold = threshold

    wemi_repo = _wemi_repo(cfg)
    clusterer = AgentClusterer(repo=wemi_repo, config=cluster_config)

    result = clusterer.cluster_agents()
    console.print(
        f"[green]Clustered {result.total_agents} agents into {result.total_clusters} canonical clusters "
        f"({result.multi_member_clusters} multi-member, max size {result.max_cluster_size}).[/green]"
    )


@app.command("build-agent-index")

def build_agent_index_cmd(
    config_path: Path = typer.Option(
        Path("config.yaml"), "--config", "-c", help="Path to config.yaml"
    ),
    batch_size: int = typer.Option(128, "--batch-size", "-b", help="Encoding batch size"),
) -> None:
    """Build dedicated agent vector index (agent.index) using concise essential serialization."""
    from reconciliator.indexer import create_vector_engine

    cfg = load_config(config_path)
    wemi_repo = _wemi_repo(cfg)
    embedder = SentenceEmbedder(cfg.embedding)
    agent_indexer = create_vector_engine(
        cfg.storage.agent_index,
        dimension=embedder.dimension,
        config=cfg.storage.vector_engine,
    )
    console.print(f"[cyan]Building agent index at {cfg.storage.agent_index}...[/cyan]")
    tpl_path = cfg.reconcile.templates.agent if cfg.reconcile else None
    count = wemi_repo.build_agent_index(
        agent_indexer=agent_indexer,
        embedder=embedder,
        batch_size=batch_size,
        template_path=tpl_path,
    )
    console.print(
        f"[green]Successfully indexed {count} agents into {cfg.storage.agent_index}[/green]"
    )


@app.command("build-index")
def build_index_cmd(
    config_path: Path = typer.Option(
        Path("config.yaml"), "--config", "-c", help="Path to config.yaml"
    ),
    batch_size: int = typer.Option(128, "--batch-size", "-b", help="Encoding batch size"),
    tier: str = typer.Option(
        "all", "--tier", "-t", help="Which index to build: work, manifestation, or all"
    ),
) -> None:
    """Build the work and manifestation vector indexes from stored serializations.

    Two tiers, each with one consumer: the work index serves the matcher's
    semantic fallback, the manifestation index serves the clusterer's
    near-duplicate detection. There is no expression tier.
    """
    from reconciliator.indexer import create_vector_engine

    cfg = load_config(config_path)
    embedder = SentenceEmbedder(cfg.embedding)
    wemi_repo = _wemi_repo(cfg, embedder=embedder)

    chosen = str(tier).lower()
    if chosen not in ("all", "work", "manifestation"):
        console.print(f"[red]Unknown tier '{tier}'. Use work, manifestation, or all.[/red]")
        raise typer.Exit(code=2)

    work_indexer = manifestation_indexer = None
    if chosen in ("all", "work"):
        work_indexer = create_vector_engine(
            cfg.storage.work_index, dimension=embedder.dimension, config=cfg.storage.vector_engine
        )
    if chosen in ("all", "manifestation"):
        manifestation_indexer = create_vector_engine(
            cfg.storage.manifestation_index,
            dimension=embedder.dimension,
            config=cfg.storage.vector_engine,
        )

    console.print("[cyan]Embedding stored works and manifestations...[/cyan]")
    counts = wemi_repo.build_wemi_indexes(
        embedder=embedder,
        work_indexer=work_indexer,
        manifestation_indexer=manifestation_indexer,
        batch_size=batch_size,
        include_constituent_works=cfg.constituents.index_works,
    )
    console.print(
        f"[green]Indexed {counts['works']} works into {cfg.storage.work_index} and "
        f"{counts['manifestations']} manifestations into {cfg.storage.manifestation_index}[/green]"
    )


@app.command("run")
def run_cmd(
    config: Path = typer.Option(
        Path("config.yaml"), "--config", "-c", help="Path to YAML configuration file"
    ),
) -> None:
    """Execute end-to-end pipeline configured in YAML."""
    import time

    start_time = time.time()
    resolved_config = _resolve_cli_config(config)
    cfg = load_config(resolved_config)
    configure_logging(cfg.logging)

    steps: list[tuple[str, Any, dict[str, Any]]] = []
    if cfg.ingest_vocab:
        steps.append(("ingest-vocab", ingest_vocab_cmd, {"config": config}))
    if cfg.ingest_oai:
        steps.append(("ingest-oai", ingest_oai_cmd, {"config": config, "all_sources": True}))
    if cfg.ingest_wikidata:
        steps.append(("ingest-wikidata", ingest_wikidata_cmd, {"config": config}))
    if cfg.reconcile:
        # Between ingest and reconcile: the matcher's dense tier searches the work
        # index, and ingest does not populate it. Without this step the pipeline
        # would reconcile against an empty index and silently reach only FTS.
        steps.append(("build-index", build_index_cmd, {"config_path": config}))
        steps.append(("reconcile", reconcile_cmd, {"config": config}))

    if not steps:
        console.print("[yellow]No pipeline steps enabled in configuration.[/yellow]")
        return

    console.print(
        f"[bold blue]Starting end-to-end pipeline with {len(steps)} enabled step(s):[/bold blue] "
        + ", ".join(f"[cyan]{s[0]}[/cyan]" for s in steps)
    )

    completed_steps: list[tuple[str, float]] = []
    for idx, (step_name, step_fn, step_kwargs) in enumerate(steps, start=1):
        step_start = time.time()
        console.print(f"\n[bold green]=== Step {idx}/{len(steps)}: {step_name} ===[/bold green]")
        try:
            step_fn(**step_kwargs)
            step_dur = time.time() - step_start
            completed_steps.append((step_name, step_dur))
            console.print(
                f"[bold green]=== Step {idx}/{len(steps)}: {step_name} completed in {step_dur:.2f}s ===[/bold green]"
            )
        except Exception as e:
            step_dur = time.time() - step_start
            console.print(
                f"\n[bold red]Pipeline failed at step {idx}/{len(steps)} ({step_name}) after {step_dur:.2f}s: {e}[/bold red]"
            )
            raise typer.Exit(code=1) from e

    total_dur = time.time() - start_time
    console.print(
        f"\n[bold blue]Pipeline completed successfully![/bold blue] "
        f"Ran {len(completed_steps)} step(s) in [bold green]{total_dur:.2f}s[/bold green]:"
    )
    for s_name, s_dur in completed_steps:
        console.print(f"  - [cyan]{s_name}[/cyan]: {s_dur:.2f}s")


def main() -> None:
    """Main CLI entrypoint."""
    app()


if __name__ == "__main__":
    main()
