"""Terminal UI, Rich tables, and real-time dashboard rendering for progress telemetry."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from rich.console import Console, Group, RenderableType
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from reconciliator.models import TaskProgress


def format_task_table(tasks: list[TaskProgress]) -> Table:
    """Render task progress list as a Rich Table."""
    table = Table(title="Reconciliator Task Telemetry", show_lines=True)
    table.add_column("Task ID", style="cyan", no_wrap=True)
    table.add_column("Type", style="magenta")
    table.add_column("Status", style="bold")
    table.add_column("Stage")
    table.add_column("Progress", justify="right")
    table.add_column("Processed / Total", justify="right")
    table.add_column("Rate", justify="right")
    table.add_column("Errors", justify="right")
    table.add_column("Last Heartbeat", style="dim")

    pipeline_tasks = [t for t in tasks if t.task_type != "system"]
    for t in pipeline_tasks:
        if t.status == "completed":
            status_style = "green"
        elif t.status == "running":
            status_style = "yellow"
        elif t.status == "interrupted":
            status_style = "dim yellow"
        else:
            status_style = "red"
        status_text = f"[{status_style}]{t.status.upper()}[/{status_style}]"

        meta: dict[str, Any] = {}
        if t.metadata_json:
            try:
                meta = json.loads(t.metadata_json)
            except Exception:
                pass

        if meta.get("harvested") is not None and meta.get("indexed") is not None:
            if "skipped" in meta:
                count_str = f"{meta['indexed']:,} indexed, {meta['skipped']:,} skipped ({meta['harvested']:,} fetched)"
            else:
                count_str = f"{meta['indexed']:,} indexed ({meta['harvested']:,} fetched)"
            if meta.get("slices_total"):
                count_str += f" [{meta.get('slices_done', 0)}/{meta['slices_total']} slices]"
        else:
            total_str = f"{t.items_total:,}" if t.items_total is not None else "?"
            count_str = f"{t.items_processed:,} / {total_str}"

        pct_val = t.percentage
        if pct_val <= 0 and meta.get("slices_total"):
            pct_val = min(100.0, (meta.get("slices_done", 0) / meta["slices_total"]) * 100.0)
        pct_str = f"{pct_val:.1f}%"
        rate_str = f"{t.rate_per_sec:.1f}/s"
        err_str = f"[red]{t.error_count:,}[/red]" if t.error_count > 0 else "0"

        table.add_row(
            t.task_id,
            t.task_type,
            status_text,
            t.current_stage,
            pct_str,
            count_str,
            rate_str,
            err_str,
            t.last_heartbeat[:19].replace("T", " "),
        )
    return table


def render_vector_daemon_panel(status: Optional[dict[str, Any]] = None) -> Panel:
    """Compose a Rich Panel displaying real-time vector daemon telemetry."""
    if not status or status.get("status") != "running":
        content = Text()
        content.append("○ ", style="dim yellow")
        content.append("Daemon Status: ", style="bold")
        content.append("STANDBY", style="dim yellow")
        content.append(
            " (Inactive — will auto-spawn on demand when vector queries execute)\n", style="dim"
        )
        content.append("Mode: ", style="bold dim")
        content.append("IPC Singleton Service", style="cyan")
        return Panel(
            content, title="[bold cyan]Vector Daemon Service[/]", style="dim", padding=(0, 1)
        )

    content = Text()
    pid = status.get("pid", "?")
    dev = str(status.get("device", "auto")).upper()
    ram = status.get("host_ram_mb", 0.0)
    uptime = int(status.get("uptime_seconds", 0))
    idle = int(status.get("idle_seconds", 0))
    idle_limit = status.get("idle_timeout_seconds")
    clients = status.get("active_clients", 0)

    # Line 1: Status badge, PID, Device, RAM, Clients
    content.append("● ", style="bold green")
    content.append("RUNNING ", style="bold green")
    content.append(f"(PID {pid})   ", style="dim")
    content.append("Device: ", style="bold white")
    content.append(f"{dev}   ", style="bold cyan")
    content.append("Host RAM: ", style="bold white")
    content.append(f"{ram:.1f} MB   ", style="yellow")
    content.append("Clients: ", style="bold white")
    content.append(f"{clients}\n", style="cyan")

    # Line 2: Uptime, Idle time / countdown
    mins, secs = divmod(uptime, 60)
    hrs, mins = divmod(mins, 60)
    uptime_str = f"{hrs:02d}:{mins:02d}:{secs:02d}"
    content.append("Uptime: ", style="dim")
    content.append(f"{uptime_str}   ", style="white")

    if idle_limit is not None and idle_limit > 0:
        remain = max(0, idle_limit - idle)
        rem_m, rem_s = divmod(remain, 60)
        content.append("Idle Countdown: ", style="dim")
        rem_style = "green" if remain > 120 else "bold red"
        content.append(f"{rem_m}m {rem_s}s   ", style=rem_style)
    else:
        content.append("Idle Shutdown: ", style="dim")
        content.append("Disabled   ", style="dim")

    indices = status.get("indices", [])
    content.append(f"Managed Indices: {len(indices)}\n", style="dim")

    # Line 3+: Indices summary
    if indices:
        for idx in indices:
            key_name = idx.get("key", "index")
            tot = idx.get("total_vectors", 0)
            dim = idx.get("dimension", "?")
            dt = idx.get("dtype", "float32")
            backend = idx.get("backend", "unknown")
            searches = idx.get("searches_total", 0)
            adds = idx.get("adds_total", 0)
            content.append(f"  • {key_name}: ", style="bold white")
            content.append(f"{tot:,} vectors ", style="bold green")
            content.append(f"({dim}-d {dt}, {backend})", style="dim")
            content.append(f" | Searches: {searches:,} | Added: {adds:,}", style="cyan")
            if idx.get("is_dirty"):
                content.append(" *UNSAVED", style="bold red")
            content.append("\n")
    else:
        content.append("  No indices currently opened\n", style="dim italic")

    return Panel(
        content,
        title="[bold cyan]Vector Daemon Service[/]",
        style="cyan",
        padding=(0, 1),
    )


def render_status_json(
    tasks: list[TaskProgress],
    daemon_status: Optional[dict[str, Any]] = None,
) -> str:
    """Render task progress list as structured JSON."""
    task_dicts = [t.model_dump() for t in tasks]
    if daemon_status and not any(d.get("task_id") == "system_vector_daemon" for d in task_dicts):
        now_iso = datetime.now(timezone.utc).isoformat()
        task_dicts.append(
            {
                "task_id": "system_vector_daemon",
                "task_type": "system",
                "status": daemon_status.get("status", "running"),
                "current_stage": f"{len(daemon_status.get('indices', []))} active index(es)",
                "items_processed": sum(
                    idx.get("total_vectors", 0) for idx in daemon_status.get("indices", [])
                ),
                "items_total": None,
                "bytes_processed": 0,
                "bytes_total": None,
                "rate_per_sec": 0.0,
                "percentage": 0.0,
                "start_time": now_iso,
                "last_heartbeat": now_iso,
                "error_message": None,
                "metadata_json": json.dumps(daemon_status, ensure_ascii=False),
            }
        )
    return json.dumps(task_dicts, indent=2, ensure_ascii=False)


def render_telemetry_tui(
    tasks: list[TaskProgress],
    db_path: Path | str = "progress.db",
    interval: float = 1.0,
    daemon_status: Optional[dict[str, Any]] = None,
    show_vector_daemon: bool = True,
) -> Group:
    """Compose a full rich TUI view for live task telemetry."""
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    path_name = Path(db_path).name

    if daemon_status is None:
        for t in tasks:
            if t.task_id == "system_vector_daemon" and t.metadata_json:
                try:
                    daemon_status = json.loads(t.metadata_json)
                except Exception:
                    pass

    pipeline_tasks = [t for t in tasks if t.task_id != "system_vector_daemon"]

    running_count = sum(1 for t in pipeline_tasks if t.status == "running")
    completed_count = sum(1 for t in pipeline_tasks if t.status == "completed")
    failed_count = sum(1 for t in pipeline_tasks if t.status == "failed")
    total_rate = sum(t.rate_per_sec for t in pipeline_tasks if t.status == "running")

    header_text = Text()
    header_text.append("RECONCILIATOR ", style="bold cyan")
    header_text.append("TASK TELEMETRY MONITOR", style="bold white")

    header = Panel(
        header_text,
        subtitle=f"[dim]DB:[/] [yellow]{path_name}[/]  [dim]Interval:[/] [green]{interval:.1f}s[/]  [dim]Time:[/] [blue]{now_utc}[/]",
        style="bright_blue",
    )

    if pipeline_tasks:
        table = Table(title="Task Queue & Pipeline Execution", expand=True, show_lines=True)
        table.add_column("Task ID", style="cyan", no_wrap=True)
        table.add_column("Type", style="magenta")
        table.add_column("Status", justify="center")
        table.add_column("Stage", style="blue")
        table.add_column("Progress", justify="right")
        table.add_column("Items / Bytes", justify="right")
        table.add_column("Throughput", justify="right")
        table.add_column("Errors", justify="right")
        table.add_column("Last Heartbeat", style="dim", justify="right")

        for t in pipeline_tasks:
            if t.status == "completed":
                status_badge = "[bold green]✔ COMPLETED[/]"
            elif t.status == "running":
                status_badge = "[bold yellow]● RUNNING[/]"
            elif t.status == "failed":
                status_badge = "[bold red]✖ FAILED[/]"
            else:
                status_badge = f"[dim]{t.status.upper()}[/]"

            meta: dict[str, Any] = {}
            if t.metadata_json:
                try:
                    meta = json.loads(t.metadata_json)
                except Exception:
                    pass

            pct_val = t.percentage
            if pct_val <= 0 and meta.get("slices_total"):
                pct_val = min(100.0, (meta.get("slices_done", 0) / meta["slices_total"]) * 100.0)
            pct_str = f"[bold green]{pct_val:.1f}%[/]" if pct_val > 0 else "0.0%"

            if meta.get("harvested") is not None and meta.get("indexed") is not None:
                if "skipped" in meta:
                    counts_str = f"{meta['indexed']:,} indexed, {meta['skipped']:,} skipped ({meta['harvested']:,} fetched)"
                else:
                    counts_str = f"{meta['indexed']:,} indexed ({meta['harvested']:,} fetched)"
                if meta.get("slices_total"):
                    counts_str += f" [{meta.get('slices_done', 0)}/{meta['slices_total']} slices]"
            elif t.items_total is not None or t.items_processed > 0:
                tot_str = f"{t.items_total:,}" if t.items_total is not None else "?"
                counts_str = f"{t.items_processed:,} / {tot_str}"
            elif t.bytes_total is not None or t.bytes_processed > 0:
                mb_processed = t.bytes_processed / (1024 * 1024)
                if t.bytes_total:
                    mb_total = t.bytes_total / (1024 * 1024)
                    counts_str = f"{mb_processed:.1f} MB / {mb_total:.1f} MB"
                else:
                    counts_str = f"{mb_processed:.1f} MB"
            else:
                counts_str = "-"

            rate_str = f"{t.rate_per_sec:,.1f} items/s" if t.rate_per_sec > 0 else "-"
            hb_str = t.last_heartbeat[:19].replace("T", " ") if t.last_heartbeat else "-"
            err_str = f"[bold red]{t.error_count:,}[/]" if t.error_count > 0 else "[dim]0[/]"

            table.add_row(
                t.task_id,
                t.task_type,
                status_badge,
                t.current_stage,
                pct_str,
                counts_str,
                rate_str,
                err_str,
                hb_str,
            )
        task_view: RenderableType = table
    else:
        task_view = Panel(
            Text(
                "No active or past tasks found in progress.db.\nWaiting for pipeline tasks to initiate...",
                justify="center",
                style="italic dim",
            ),
            title="Task Queue",
            style="dim",
            padding=(1, 2),
        )

    total_skipped = 0
    total_errors = 0
    for t in pipeline_tasks:
        total_errors += t.error_count
        if t.metadata_json:
            try:
                m = json.loads(t.metadata_json)
                total_skipped += m.get("skipped", 0)
            except Exception:
                pass

    summary_text = Text()
    summary_text.append(
        f"Running: {running_count}  ", style="bold yellow" if running_count > 0 else "dim"
    )
    summary_text.append(
        f"Completed: {completed_count}  ", style="bold green" if completed_count > 0 else "dim"
    )
    summary_text.append(
        f"Failed: {failed_count}  ", style="bold red" if failed_count > 0 else "dim"
    )
    summary_text.append(f"| Total Tasks: {len(pipeline_tasks)}  ", style="dim")
    if total_errors > 0:
        summary_text.append(f"| Errors: {total_errors:,}  ", style="bold red")
    if total_skipped > 0:
        summary_text.append(f"| Skipped: {total_skipped:,}  ", style="bold cyan")
    summary_text.append(f"| Aggregate Throughput: {total_rate:,.1f} items/s", style="bold cyan")

    summary = Panel(summary_text, title="Telemetry Summary", style="dim")

    elements: list[RenderableType] = [header]
    if show_vector_daemon:
        elements.append(render_vector_daemon_panel(daemon_status))
    elements.extend([task_view, summary])

    failed_tasks = [t for t in pipeline_tasks if t.status == "failed" and t.error_message]
    if failed_tasks:
        error_lines = Text()
        for ft in failed_tasks:
            error_lines.append(f"Task {ft.task_id} ({ft.task_type}) failed: ", style="bold red")
            error_lines.append(f"{ft.error_message}\n", style="white")
        elements.append(Panel(error_lines, title="[bold red]Task Error Details[/]", style="red"))

    footer = Text(
        "Press Ctrl+C to exit live telemetry monitor", style="dim italic", justify="center"
    )
    elements.append(footer)

    return Group(*elements)


def run_telemetry_tui(
    fetch_tasks: Callable[[], list[TaskProgress]],
    db_path: Path | str,
    interval: float = 1.0,
    console: Optional[Console] = None,
    show_vector_daemon: bool = True,
    get_daemon_status: Optional[Callable[[], Optional[dict[str, Any]]]] = None,
    exit_when_done: bool = False,
) -> None:
    """Run interactive full-screen Live TUI monitor."""
    c = console or Console()
    initial_tasks = fetch_tasks()
    initial_d_status = get_daemon_status() if get_daemon_status else None
    with Live(
        render_telemetry_tui(
            initial_tasks,
            db_path=db_path,
            interval=interval,
            daemon_status=initial_d_status,
            show_vector_daemon=show_vector_daemon,
        ),
        console=c,
        screen=c.is_terminal,
        refresh_per_second=max(1.0 / max(interval, 0.1), 1.0),
    ) as live:
        try:
            while True:
                tasks = fetch_tasks()
                d_status = get_daemon_status() if get_daemon_status else None
                live.update(
                    render_telemetry_tui(
                        tasks,
                        db_path=db_path,
                        interval=interval,
                        daemon_status=d_status,
                        show_vector_daemon=show_vector_daemon,
                    )
                )
                if (
                    exit_when_done
                    and tasks
                    and all(
                        t.status in ("completed", "failed")
                        for t in tasks
                        if t.task_id != "system_vector_daemon"
                    )
                ):
                    break
                time.sleep(interval)
        except KeyboardInterrupt:
            pass
