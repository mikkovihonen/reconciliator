"""Maintenance and administrative CLI commands (reset, status, prune)."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Optional

import typer

from reconciliator.commands.common import _resolve_cli_config, console
from reconciliator.config import configure_logging, load_config
from reconciliator.database import reset_all_storage
from reconciliator.indexer.ipc.client import DaemonLifecycleManager
from reconciliator.indexer.ipc.protocol import get_default_socket_path
from reconciliator.models import TaskProgress
from reconciliator.progress import (
    format_task_table,
    get_task_progress,
    list_tasks,
    mark_stale_tasks,
    prune_tasks,
    render_status_json,
    render_vector_daemon_panel,
    run_telemetry_tui,
)
from reconciliator.utils import parse_duration_seconds


def reset_cmd(
    config: Path = typer.Option(
        Path("config.yaml"), "--config", "-c", help="Path to YAML configuration file"
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Bypass interactive confirmation prompt"),
) -> None:
    """Reset all storage: PostgreSQL database tables, vector indices, and the vector daemon."""
    # Typer's OptionInfo defaults are truthy, so guard for direct invocation.
    yes = yes if isinstance(yes, bool) else False

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
        mark_stale_tasks(db_target, timeout_seconds=cfg.telemetry.stale_heartbeat_timeout_seconds)
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
                        and all(t.status in ("completed", "failed", "interrupted") for t in tasks)
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
    """Prune finished or stale task records from PostgreSQL telemetry tables."""
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

