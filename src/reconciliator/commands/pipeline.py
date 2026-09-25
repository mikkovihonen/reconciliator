"""Pipeline execution command."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import typer

from reconciliator.commands.common import _resolve_cli_config, console
from reconciliator.commands.ingest import (
    ingest_oai_cmd,
    ingest_vocab_cmd,
    ingest_wikidata_cmd,
)
from reconciliator.commands.reconcile import reconcile_cmd
from reconciliator.commands.wemi import build_index_cmd
from reconciliator.config import configure_logging, load_config


def run_cmd(
    config: Path = typer.Option(
        Path("config.yaml"), "--config", "-c", help="Path to YAML configuration file"
    ),
) -> None:
    """Execute end-to-end pipeline configured in YAML."""
    start_time = time.time()
    resolved_config = _resolve_cli_config(config)
    cfg = load_config(resolved_config)
    configure_logging(cfg.logging)

    steps: list[tuple[str, Any, dict[str, Any]]] = []
    if cfg.ingestion.vocab and cfg.ingestion.vocab.enabled:
        steps.append(("ingest-vocab", ingest_vocab_cmd, {"config": config}))
    if cfg.ingestion.oai:
        steps.append(("ingest-oai", ingest_oai_cmd, {"config": config, "all_sources": True}))
    if cfg.ingestion.wikidata and cfg.ingestion.wikidata.enabled:
        steps.append(("ingest-wikidata", ingest_wikidata_cmd, {"config": config}))
    if cfg.reconcile:
        # Between ingest and reconcile: the matcher's dense tier searches the work
        # index, and ingest does not populate it. Without this step the pipeline
        # would reconcile against an empty index and silently reach only FTS.
        steps.append(
            (
                "build-index",
                build_index_cmd,
                {"config_path": config, "tier": "all", "batch_size": 128},
            )
        )
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
