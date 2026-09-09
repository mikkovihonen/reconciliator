"""Dedicated progress telemetry tracking and CLI status query engine operating on progress.db."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import time
from typing import Any, Optional

from rich.table import Table

from reconciliator.db import configure_wal_pragmas, get_db_connection
from reconciliator.models import TaskProgress, TaskStatus, TaskType


def init_progress_db(conn: sqlite3.Connection) -> None:
    """Initialize task_progress table in progress.db."""
    with conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS task_progress (
                task_id TEXT PRIMARY KEY,
                task_type TEXT NOT NULL,
                status TEXT NOT NULL,
                current_stage TEXT NOT NULL,
                items_processed INTEGER DEFAULT 0,
                items_total INTEGER,
                bytes_processed INTEGER DEFAULT 0,
                bytes_total INTEGER,
                rate_per_sec REAL DEFAULT 0.0,
                percentage REAL DEFAULT 0.0,
                start_time TEXT NOT NULL,
                last_heartbeat TEXT NOT NULL,
                error_message TEXT,
                metadata_json TEXT
            );
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_progress_type ON task_progress(task_type);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_progress_status ON task_progress(status);")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class ProgressTracker:
    """Manages real-time task lifecycle and metrics heartbeats in progress.db."""

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
        with self.get_connection() as conn:
            init_progress_db(conn)

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

    def get_connection(self) -> sqlite3.Connection:
        return get_db_connection(self.db_path)

    def start_task(
        self,
        stage: str = "running",
        items_total: Optional[int] = None,
        bytes_total: Optional[int] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> None:
        """Start task and record initial row in progress.db."""
        self._start_time_sec = time.time()
        self._current_stage = stage
        self._status = "running"
        self._items_total = items_total
        self._bytes_total = bytes_total
        if metadata:
            self._metadata.update(metadata)

        now = _now_iso()
        with self.get_connection() as conn:
            conn.execute(
                """
                INSERT INTO task_progress (
                    task_id, task_type, status, current_stage,
                    items_processed, items_total, bytes_processed, bytes_total,
                    rate_per_sec, percentage, start_time, last_heartbeat,
                    error_message, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(task_id) DO UPDATE SET
                    status = excluded.status,
                    current_stage = excluded.current_stage,
                    items_total = excluded.items_total,
                    bytes_total = excluded.bytes_total,
                    last_heartbeat = excluded.last_heartbeat,
                    metadata_json = excluded.metadata_json;
                """,
                (
                    self.task_id,
                    self.task_type,
                    self._status,
                    self._current_stage,
                    self._items_processed,
                    self._items_total,
                    self._bytes_processed,
                    self._bytes_total,
                    0.0,
                    0.0,
                    now,
                    now,
                    None,
                    json.dumps(self._metadata, ensure_ascii=False),
                ),
            )
        self._last_heartbeat_sec = time.time()

    def update_progress(
        self,
        stage: Optional[str] = None,
        items_processed: Optional[int] = None,
        items_increment: int = 0,
        bytes_processed: Optional[int] = None,
        metadata: Optional[dict[str, Any]] = None,
        force: bool = False,
    ) -> None:
        """Update metrics and emit heartbeat if interval elapsed or forced."""
        if stage:
            self._current_stage = stage
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
        with self.get_connection() as conn:
            conn.execute(
                """
                UPDATE task_progress SET
                    status = ?,
                    current_stage = ?,
                    items_processed = ?,
                    bytes_processed = ?,
                    rate_per_sec = ?,
                    percentage = ?,
                    last_heartbeat = ?,
                    error_message = ?,
                    metadata_json = ?
                WHERE task_id = ?;
                """,
                (
                    self._status,
                    self._current_stage,
                    self._items_processed,
                    self._bytes_processed,
                    round(rate, 2),
                    round(percentage, 1),
                    now_iso,
                    self._error_message,
                    json.dumps(self._metadata, ensure_ascii=False),
                    self.task_id,
                ),
            )
        self._last_heartbeat_sec = now_sec

    def complete_task(self, metadata: Optional[dict[str, Any]] = None) -> None:
        """Mark task completed with 100% progress."""
        self._status = "completed"
        self._current_stage = "completed"
        if self._items_total and self._items_processed < self._items_total:
            self._items_processed = self._items_total
        if metadata:
            self._metadata.update(metadata)
        self._commit_heartbeat(time.time())

    def fail_task(self, error_message: str, metadata: Optional[dict[str, Any]] = None) -> None:
        """Mark task failed with error details."""
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
    with get_db_connection(path) as conn:
        row = conn.execute("SELECT * FROM task_progress WHERE task_id = ?;", (task_id,)).fetchone()
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


def list_tasks(db_path: Path | str, limit: int = 50) -> list[TaskProgress]:
    """Retrieve all recent task progress records."""
    path = Path(db_path)
    if not path.exists():
        return []
    with get_db_connection(path) as conn:
        rows = conn.execute(
            "SELECT * FROM task_progress ORDER BY last_heartbeat DESC LIMIT ?;",
            (limit,),
        ).fetchall()
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

    for t in tasks:
        status_style = "green" if t.status == "completed" else "yellow" if t.status == "running" else "red"
        status_text = f"[{status_style}]{t.status.upper()}[/{status_style}]"

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


def render_status_json(tasks: list[TaskProgress]) -> str:
    """Render task progress list as structured JSON."""
    return json.dumps([t.model_dump() for t in tasks], indent=2, ensure_ascii=False)

