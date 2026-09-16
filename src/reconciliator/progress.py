"""SQLAlchemy 2.0 Core progress telemetry tracking and CLI status query engine operating on progress.db."""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from rich.console import Console, Group, RenderableType
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from sqlalchemy import (
    Column,
    Engine,
    Float,
    Index,
    Integer,
    MetaData,
    String,
    and_,
    delete,
    select,
    text,
    update,
)
from sqlalchemy import (
    Table as SQLTable,
)
from sqlalchemy import (
    Text as SQLText,
)
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from reconciliator.db import get_engine
from reconciliator.models import TaskProgress, TaskStatus, TaskType

logger = logging.getLogger(__name__)

# ===================================================================
# Progress Schema & Table Definition
# ===================================================================

progress_metadata = MetaData()

task_progress_table = SQLTable(
    "task_progress",
    progress_metadata,
    Column("task_id", String, primary_key=True),
    Column("task_type", String, nullable=False),
    Column("status", String, nullable=False),
    Column("current_stage", String, nullable=False),
    Column("items_processed", Integer, default=0, server_default="0"),
    Column("items_total", Integer, nullable=True),
    Column("bytes_processed", Integer, default=0, server_default="0"),
    Column("bytes_total", Integer, nullable=True),
    Column("rate_per_sec", Float, default=0.0, server_default="0.0"),
    Column("percentage", Float, default=0.0, server_default="0.0"),
    Column("start_time", String, nullable=False),
    Column("last_heartbeat", String, nullable=False),
    Column("error_message", String, nullable=True),
    Column("metadata_json", SQLText, nullable=True),
    Index("idx_progress_type", "task_type"),
    Index("idx_progress_status", "status"),
)


def init_progress_db(engine: Engine) -> None:
    """Initialize task_progress table in progress.db."""
    progress_metadata.create_all(engine)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class ProgressTracker:
    """Manages real-time task lifecycle and metrics heartbeats in progress.db via SQLAlchemy 2.0 Core."""

    def __init__(
        self,
        db_path: Path | str,
        task_id: str,
        task_type: TaskType,
        heartbeat_interval: float = 1.0,
    ) -> None:
        self.db_path = Path(db_path)
        self.task_id = task_id
        self.task_type = task_type
        self.heartbeat_interval = heartbeat_interval

        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.engine = get_engine(self.db_path)
        init_progress_db(self.engine)

        self._start_time_sec = time.time()
        self._last_heartbeat_sec = 0.0
        self._items_processed = 0
        self._items_total: Optional[int] = None
        self._bytes_processed = 0
        self._bytes_total: Optional[int] = None
        self._current_stage = "pending"
        self._status: TaskStatus = "running"
        self._metadata: dict[str, Any] = {}
        self._error_message: Optional[str] = None
        self._lock = threading.Lock()

    def close(self) -> None:
        """Dispose SQLAlchemy engine and release connection pool."""
        self.engine.dispose()

    def start_task(
        self,
        stage: str = "running",
        items_total: Optional[int] = None,
        bytes_total: Optional[int] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> None:
        """Start task and record initial row in progress.db."""
        with self._lock:
            self._start_time_sec = time.time()
            self._current_stage = stage
            self._status = "running"
            self._items_total = items_total
            self._bytes_total = bytes_total
            if metadata:
                self._metadata.update(metadata)

            now = _now_iso()
            meta_json = json.dumps(self._metadata, ensure_ascii=False) if self._metadata else None
            with self.engine.begin() as conn:
                stmt = sqlite_insert(task_progress_table).values(
                    task_id=self.task_id,
                    task_type=self.task_type,
                    status=self._status,
                    current_stage=self._current_stage,
                    items_processed=self._items_processed,
                    items_total=self._items_total,
                    bytes_processed=self._bytes_processed,
                    bytes_total=self._bytes_total,
                    rate_per_sec=0.0,
                    percentage=0.0,
                    start_time=now,
                    last_heartbeat=now,
                    error_message=None,
                    metadata_json=meta_json,
                )
                stmt = stmt.on_conflict_do_update(
                    index_elements=["task_id"],
                    set_={
                        "status": stmt.excluded.status,
                        "current_stage": stmt.excluded.current_stage,
                        "items_total": stmt.excluded.items_total,
                        "bytes_total": stmt.excluded.bytes_total,
                        "last_heartbeat": stmt.excluded.last_heartbeat,
                        "metadata_json": stmt.excluded.metadata_json,
                    },
                )
                conn.execute(stmt)
            self._last_heartbeat_sec = time.time()

    def update_progress(
        self,
        stage: Optional[str] = None,
        items_processed: Optional[int] = None,
        items_increment: int = 0,
        items_total: Optional[int] = None,
        bytes_processed: Optional[int] = None,
        bytes_total: Optional[int] = None,
        metadata: Optional[dict[str, Any]] = None,
        force: bool = False,
    ) -> None:
        """Update metrics and emit heartbeat if interval elapsed or forced."""
        with self._lock:
            if stage:
                self._current_stage = stage
            if items_total is not None:
                self._items_total = items_total
            if bytes_total is not None:
                self._bytes_total = bytes_total
            if items_processed is not None:
                self._items_processed = items_processed
            else:
                self._items_processed += items_increment

            if bytes_processed is not None:
                self._bytes_processed = bytes_processed

            if metadata:
                self._metadata.update(metadata)

            now_sec = time.time()
            if force or (now_sec - self._last_heartbeat_sec >= self.heartbeat_interval):
                self._commit_heartbeat(now_sec)

    def _commit_heartbeat(self, now_sec: float) -> None:
        elapsed = max(now_sec - self._start_time_sec, 0.001)
        rate = self._items_processed / elapsed

        percentage = 0.0
        if self._items_total and self._items_total > 0:
            percentage = min(100.0, (self._items_processed / self._items_total) * 100.0)
        elif self._bytes_total and self._bytes_total > 0:
            percentage = min(100.0, (self._bytes_processed / self._bytes_total) * 100.0)

        now_iso = _now_iso()
        meta_json = json.dumps(self._metadata, ensure_ascii=False) if self._metadata else None
        with self.engine.begin() as conn:
            stmt = (
                update(task_progress_table)
                .where(task_progress_table.c.task_id == self.task_id)
                .values(
                    status=self._status,
                    current_stage=self._current_stage,
                    items_processed=self._items_processed,
                    bytes_processed=self._bytes_processed,
                    rate_per_sec=round(rate, 2),
                    percentage=round(percentage, 1),
                    last_heartbeat=now_iso,
                    error_message=self._error_message,
                    metadata_json=meta_json,
                )
            )
            conn.execute(stmt)
        self._last_heartbeat_sec = now_sec

    def complete_task(self, metadata: Optional[dict[str, Any]] = None) -> None:
        """Mark task completed with 100% progress."""
        with self._lock:
            self._status = "completed"
            self._current_stage = "completed"
            if self._items_total and self._items_processed < self._items_total:
                self._items_processed = self._items_total
            if metadata:
                self._metadata.update(metadata)
            self._commit_heartbeat(time.time())

    def fail_task(self, error_message: str, metadata: Optional[dict[str, Any]] = None) -> None:
        """Mark task failed with error details."""
        with self._lock:
            self._status = "failed"
            self._current_stage = "failed"
            self._error_message = error_message
            if metadata:
                self._metadata.update(metadata)
            self._commit_heartbeat(time.time())


def get_task_progress(db_path: Path | str, task_id: str) -> Optional[TaskProgress]:
    """Retrieve progress model for a specific task."""
    path = Path(db_path)
    if not path.exists():
        return None
    engine = get_engine(path)
    try:
        with engine.connect() as conn:
            stmt = select(task_progress_table).where(task_progress_table.c.task_id == task_id)
            row = conn.execute(stmt).mappings().first()
            if not row:
                return None
            return TaskProgress(
                task_id=row["task_id"],
                task_type=row["task_type"],
                status=row["status"],
                current_stage=row["current_stage"],
                items_processed=row["items_processed"],
                items_total=row["items_total"],
                bytes_processed=row["bytes_processed"],
                bytes_total=row["bytes_total"],
                rate_per_sec=row["rate_per_sec"],
                percentage=row["percentage"],
                start_time=row["start_time"],
                last_heartbeat=row["last_heartbeat"],
                error_message=row["error_message"],
                metadata_json=row["metadata_json"],
            )
    finally:
        engine.dispose()


def list_tasks(db_path: Path | str, limit: int = 50) -> list[TaskProgress]:
    """Retrieve all recent task progress records."""
    path = Path(db_path)
    if not path.exists():
        return []
    engine = get_engine(path)
    try:
        with engine.connect() as conn:
            stmt = select(task_progress_table).order_by(task_progress_table.c.last_heartbeat.desc()).limit(limit)
            rows = conn.execute(stmt).mappings().all()
            return [
                TaskProgress(
                    task_id=r["task_id"],
                    task_type=r["task_type"],
                    status=r["status"],
                    current_stage=r["current_stage"],
                    items_processed=r["items_processed"],
                    items_total=r["items_total"],
                    bytes_processed=r["bytes_processed"],
                    bytes_total=r["bytes_total"],
                    rate_per_sec=r["rate_per_sec"],
                    percentage=r["percentage"],
                    start_time=r["start_time"],
                    last_heartbeat=r["last_heartbeat"],
                    error_message=r["error_message"],
                    metadata_json=r["metadata_json"],
                )
                for r in rows
            ]
    finally:
        engine.dispose()


def parse_duration_seconds(duration_str: str) -> float:
    """Parse human-readable duration strings like '7d', '24h', '30m', '60s' into seconds."""
    s = duration_str.strip().lower()
    if not s:
        raise ValueError("Duration string cannot be empty")
    if s.endswith(("days", "day", "d")):
        val = s.rstrip("days").rstrip("d")
        return float(val) * 86400.0
    elif s.endswith(("hours", "hour", "hrs", "hr", "h")):
        val = s.rstrip("hours").rstrip("hrs").rstrip("h")
        return float(val) * 3600.0
    elif s.endswith(("minutes", "minute", "mins", "min", "m")):
        val = s.rstrip("minutes").rstrip("mins").rstrip("m")
        return float(val) * 60.0
    elif s.endswith(("seconds", "second", "secs", "sec", "s")):
        val = s.rstrip("seconds").rstrip("secs").rstrip("s")
        return float(val)
    else:
        # Default numeric strings without unit to days
        return float(s) * 86400.0


def prune_tasks(
    db_path: Path | str,
    task_id: Optional[str] = None,
    status: Optional[str] = None,
    older_than_seconds: Optional[float] = None,
    keep_last: Optional[int] = None,
    include_running: bool = False,
) -> int:
    """Prune task telemetry records from progress.db."""
    path = Path(db_path)
    if not path.exists():
        return 0

    engine = get_engine(path)
    try:
        with engine.begin() as conn:
            conditions = []

            if not include_running:
                conditions.append(task_progress_table.c.status != "running")

            if task_id:
                conditions.append(task_progress_table.c.task_id == task_id)

            if status:
                if status == "terminated":
                    conditions.append(task_progress_table.c.status.in_(["completed", "failed"]))
                else:
                    conditions.append(task_progress_table.c.status == status)

            if older_than_seconds is not None:
                cutoff = datetime.now(timezone.utc) - timedelta(seconds=older_than_seconds)
                cutoff_iso = cutoff.isoformat()
                conditions.append(task_progress_table.c.last_heartbeat < cutoff_iso)

            if keep_last is not None and keep_last > 0:
                keep_stmt = (
                    select(task_progress_table.c.task_id)
                    .order_by(task_progress_table.c.last_heartbeat.desc())
                    .limit(keep_last)
                )
                keep_ids = [r[0] for r in conn.execute(keep_stmt).fetchall()]
                if keep_ids:
                    conditions.append(~task_progress_table.c.task_id.in_(keep_ids))

            stmt = delete(task_progress_table)
            if conditions:
                stmt = stmt.where(and_(*conditions))
            result = conn.execute(stmt)
            deleted_count = result.rowcount

            if deleted_count > 0:
                try:
                    conn.execute(text("VACUUM;"))
                except Exception:
                    pass

            return max(deleted_count, 0)
    finally:
        engine.dispose()


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
    table.add_column("Last Heartbeat", style="dim")

    pipeline_tasks = [t for t in tasks if t.task_type != "system"]
    for t in pipeline_tasks:
        status_style = (
            "green" if t.status == "completed" else "yellow" if t.status == "running" else "red"
        )
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
        else:
            total_str = f"{t.items_total:,}" if t.items_total is not None else "?"
            count_str = f"{t.items_processed:,} / {total_str}"
        pct_str = f"{t.percentage:.1f}%"
        rate_str = f"{t.rate_per_sec:.1f}/s"

        table.add_row(
            t.task_id,
            t.task_type,
            status_text,
            t.current_stage,
            pct_str,
            count_str,
            rate_str,
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
        content.append(" (Inactive — will auto-spawn on demand when vector queries execute)\n", style="dim")
        content.append("Mode: ", style="bold dim")
        content.append("IPC Singleton Service", style="cyan")
        return Panel(content, title="[bold cyan]Vector Daemon Service[/]", style="dim", padding=(0, 1))

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

    # Extract daemon status from system task if not passed directly
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

            pct_str = f"[bold green]{t.percentage:.1f}%[/]" if t.percentage > 0 else "0.0%"

            meta: dict[str, Any] = {}
            if t.metadata_json:
                try:
                    meta = json.loads(t.metadata_json)
                except Exception:
                    pass

            if meta.get("harvested") is not None and meta.get("indexed") is not None:
                if "skipped" in meta:
                    counts_str = f"{meta['indexed']:,} indexed, {meta['skipped']:,} skipped ({meta['harvested']:,} fetched)"
                else:
                    counts_str = f"{meta['indexed']:,} indexed ({meta['harvested']:,} fetched)"
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

            table.add_row(
                t.task_id,
                t.task_type,
                status_badge,
                t.current_stage,
                pct_str,
                counts_str,
                rate_str,
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
    for t in pipeline_tasks:
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
