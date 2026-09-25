"""Vector index background daemon management subcommands."""

from __future__ import annotations

import json as _json
import time
from pathlib import Path

import typer

from reconciliator.commands.common import _resolve_cli_config, console
from reconciliator.config import load_config
from reconciliator.indexer.ipc.client import DaemonLifecycleManager
from reconciliator.indexer.ipc.protocol import get_default_socket_path
from reconciliator.progress import render_vector_daemon_panel

daemon_app = typer.Typer(name="daemon", help="Manage singleton vector index service.")


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
