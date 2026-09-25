"""Ingestion commands for vocabularies, OAI-PMH, and Wikidata."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Optional

import typer
from rich.table import Table

from reconciliator.commands.common import _resolve_cli_config, _wemi_repo, console
from reconciliator.config import (
    AppConfig,
    OAIIngestConfig,
    SkosmosDownloadConfig,
    configure_logging,
    load_config,
)
from reconciliator.ingestion.oai import ingest_oai_pipeline
from reconciliator.ingestion.skosmos_downloader import SkosmosDownloader
from reconciliator.ingestion.vocab import ingest_vocab_file
from reconciliator.ingestion.wikidata import ingest_wikidata_pipeline
from reconciliator.progress import (
    ProgressTracker,
    find_latest_task_for_source,
    mark_stale_tasks,
)
from reconciliator.vocab import VocabStore


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
    resolved_config = _resolve_cli_config(config)
    cfg = load_config(resolved_config) if resolved_config.exists() else AppConfig()
    configure_logging(cfg.logging)

    if list_vocabs:
        effective_api_url = api_url
        if not effective_api_url:
            if source and source in cfg.ingestion.skosmos:
                effective_api_url = cfg.ingestion.skosmos[source].api_url
            elif cfg.ingestion.skosmos:
                effective_api_url = next(iter(cfg.ingestion.skosmos.values())).api_url
            else:
                console.print(
                    "[red]Error: No Skosmos API URL provided and no source configured under 'ingestion.skosmos'.[/red]"
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
        if not cfg.ingestion.skosmos:
            console.print(
                "[yellow]No Skosmos sources configured under 'ingestion.skosmos'.[/yellow]"
            )
            return
        enabled_sources = {k: v for k, v in cfg.ingestion.skosmos.items() if v.enabled}
        if not enabled_sources:
            console.print(
                "[yellow]No Skosmos sources are currently enabled under 'ingestion.skosmos'.[/yellow]"
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
        if source not in cfg.ingestion.skosmos:
            console.print(
                f"[red]Error: Source '{source}' not found in configuration 'ingestion.skosmos'.[/red]"
            )
            console.print(f"Available sources: {', '.join(cfg.ingestion.skosmos.keys()) or 'None'}")
            raise typer.Exit(code=1)
        src_name = source
        dl_cfg = cfg.ingestion.skosmos[source]
    else:
        enabled_sources = {k: v for k, v in cfg.ingestion.skosmos.items() if v.enabled}
        if enabled_sources:
            src_name = next(iter(enabled_sources.keys()))
            dl_cfg = enabled_sources[src_name]
        elif cfg.ingestion.skosmos:
            src_name = next(iter(cfg.ingestion.skosmos.keys()))
            dl_cfg = cfg.ingestion.skosmos[src_name]
        else:
            console.print(
                "[red]Error: No Skosmos source specified and none configured in 'ingestion.skosmos'.[/red]"
            )
            console.print(
                "Please specify --source or configure at least one source under 'ingestion.skosmos'."
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


def ingest_vocab_cmd(
    config: Path = typer.Option(
        Path("config.yaml"), "--config", "-c", help="Path to YAML configuration file"
    ),
) -> None:
    """Ingest ontologies and legacy migration tables into dedicated vocab.db."""
    resolved_config = _resolve_cli_config(config)
    cfg = load_config(resolved_config)
    configure_logging(cfg.logging)
    if not cfg.ingestion.vocab or not cfg.ingestion.vocab.enabled:
        console.print("[red]No ingestion.vocab section found or enabled in configuration.[/red]")
        raise typer.Exit(code=1)

    vocab_store = VocabStore(cfg.storage.database_url)
    tracker = ProgressTracker(
        db_path=cfg.storage.database_url,
        task_id=f"vocab-ingest-{int(time.time())}",
        task_type="vocab_ingest",
        heartbeat_interval=cfg.telemetry.heartbeat_interval_seconds,
    )

    console.print(f"[cyan]Ingesting vocabularies from {cfg.ingestion.vocab.input_path}...[/cyan]")
    c_count, m_count = ingest_vocab_file(
        cfg.ingestion.vocab.input_path, vocab_store, tracker=tracker
    )
    console.print(
        f"[green]Successfully ingested {c_count} concepts and {m_count} mappings.[/green]"
    )


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
    task_id: Optional[str] = typer.Option(
        None,
        "--task-id",
        help="Explicit task ID to use for progress telemetry",
    ),
    adopt_previous_task_id: Optional[bool] = typer.Option(
        None,
        "--adopt-previous-task-id/--no-adopt-previous-task-id",
        help="Override automatic adoption of previous uncompleted task ID when resuming",
    ),
) -> None:
    """Harvest or read authority records from configured OAI-PMH/XML sources.

    An interrupted OAI-PMH harvest resumes from its saved checkpoint by default,
    and a source that previously completed is harvested incrementally. Use
    --restart to discard that checkpoint. --skip-existing filters individual
    records instead, and is independent of harvest checkpointing.
    """
    # Typer's OptionInfo defaults are truthy, so guard for direct invocation.
    source = source if isinstance(source, str) else None
    all_sources = all_sources if isinstance(all_sources, bool) else False
    from_date = from_date if isinstance(from_date, str) else None
    until_date = until_date if isinstance(until_date, str) else None
    workers = workers if isinstance(workers, int) else None
    slice_days = slice_days if isinstance(slice_days, int) else None
    max_concurrent_requests = (
        max_concurrent_requests if isinstance(max_concurrent_requests, int) else None
    )
    prefetch = prefetch if isinstance(prefetch, bool) else None
    skip_existing = skip_existing if isinstance(skip_existing, bool) else False
    restart = restart if isinstance(restart, bool) else False
    task_id = task_id if isinstance(task_id, str) else None
    adopt_previous_task_id = (
        adopt_previous_task_id if isinstance(adopt_previous_task_id, bool) else None
    )

    resolved_config = _resolve_cli_config(config)
    cfg = load_config(resolved_config)
    configure_logging(cfg.logging)
    if not cfg.ingestion.oai:
        console.print("[red]No ingestion.oai section found in configuration.[/red]")
        raise typer.Exit(code=1)

    mark_stale_tasks(
        cfg.storage.database_url,
        timeout_seconds=cfg.telemetry.stale_heartbeat_timeout_seconds,
    )

    targets: dict[str, OAIIngestConfig] = {}
    if all_sources:
        targets = {name: src for name, src in cfg.ingestion.oai.items() if src.enabled}
        if not targets:
            console.print("[yellow]No enabled sources found under ingestion.oai.[/yellow]")
            return
    elif source:
        if source not in cfg.ingestion.oai:
            available = ", ".join(cfg.ingestion.oai.keys())
            console.print(
                f"[red]Source '{source}' not found in ingestion.oai config. Available sources: {available}[/red]"
            )
            raise typer.Exit(code=1)
        targets = {source: cfg.ingestion.oai[source]}
    elif len(cfg.ingestion.oai) == 1:
        targets = {k: v for k, v in cfg.ingestion.oai.items() if v.enabled}
    else:
        available = ", ".join(cfg.ingestion.oai.keys())
        console.print(
            f"[red]Multiple OAI sources configured ({available}). Please specify --source <name> or --all.[/red]"
        )
        raise typer.Exit(code=1)

    # Decomposition folds legacy subject headings and normalizes relator codes, so
    # ingest needs the same vocabulary store reconcile uses.
    vocab_store = VocabStore(cfg.storage.database_url)
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

        effective_adopt = (
            adopt_previous_task_id
            if adopt_previous_task_id is not None
            else src_cfg.adopt_previous_task_id
        )

        effective_task_id = task_id
        if not effective_task_id and effective_adopt and not restart:
            prior_task = find_latest_task_for_source(
                cfg.storage.database_url,
                source_name=src_name,
                task_type="oai_pmh_harvest",
            )
            if prior_task:
                effective_task_id = prior_task.task_id
                console.print(
                    f"[dim cyan]Adopting previous task ID '{effective_task_id}' for resumed harvest.[/dim cyan]"
                )

        if not effective_task_id:
            effective_task_id = f"oai-ingest-{src_name}-{int(time.time())}"

        tracker = ProgressTracker(
            db_path=cfg.storage.database_url,
            task_id=effective_task_id,
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


def ingest_wikidata_cmd(
    config: Path = typer.Option(
        Path("config.yaml"), "--config", "-c", help="Path to YAML configuration file"
    ),
) -> None:
    """Stream gzipped Wikidata dump, extract entities, embed, and index."""
    resolved_config = _resolve_cli_config(config)
    cfg = load_config(resolved_config)
    configure_logging(cfg.logging)
    if not cfg.ingestion.wikidata or not cfg.ingestion.wikidata.enabled:
        console.print("[red]No ingestion.wikidata section found or enabled in configuration.[/red]")
        raise typer.Exit(code=1)

    tracker = ProgressTracker(
        db_path=cfg.storage.database_url,
        task_id=f"wikidata-ingest-{int(time.time())}",
        task_type="wikidata_ingest",
        heartbeat_interval=cfg.telemetry.heartbeat_interval_seconds,
    )

    wemi_repo = _wemi_repo(cfg)
    console.print("[cyan]Streaming Wikidata entities into WEMI authority repository...[/cyan]")
    total = ingest_wikidata_pipeline(cfg.ingestion.wikidata, wemi_repo, tracker=tracker)
    console.print(f"[green]Successfully ingested {total} records from Wikidata.[/green]")
