"""Reconciliation and corpus alignment CLI commands."""

from __future__ import annotations

from pathlib import Path

import typer

from reconciliator.commands.common import _resolve_cli_config, console
from reconciliator.config import configure_logging, load_config
from reconciliator.reconciliation.runner import ReconciliationPipelineRunner


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

    runner = ReconciliationPipelineRunner.from_config(cfg, console=console)
    runner.run()


__all__ = [
    "ReconciliationPipelineRunner",
    "reconcile_cmd",
]
