"""SQLAlchemy 2.0 Core progress telemetry tracking and status persistence in progress.db."""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from rich.live import Live
from sqlalchemy import (
    Engine,
    and_,
    delete,
    select,
    update,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert

from reconciliator.database import get_engine, task_progress_table
from reconciliator.models import TaskProgress, TaskStatus, TaskType
from reconciliator.utils.durations import parse_duration_seconds as parse_duration_seconds

logger = logging.getLogger(__name__)


def init_progress_db(engine: Any) -> None:
    """Initialize task_progress table in progress.db."""
    from reconciliator.database import create_schema_tables, task_progress_table

    create_schema_tables(engine, [task_progress_table])
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql("""
                ALTER TABLE task_progress
                    ALTER COLUMN bytes_total TYPE BIGINT,
                    ALTER COLUMN bytes_processed TYPE BIGINT,
                    ALTER COLUMN items_total TYPE BIGINT,
                    ALTER COLUMN items_processed TYPE BIGINT;
            """)
    except Exception:
        pass


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class ProgressTracker:
    """Manages real-time task lifecycle and metrics heartbeats in progress.db via SQLAlchemy 2.0 Core."""

    def __init__(
        self,
        db_path: Path | str | None = None,
        task_id: str = "",
        task_type: TaskType = "reconciliation",
        heartbeat_interval: float = 1.0,
        engine: Optional[Engine] = None,
        initial_items_processed: int = 0,
    ) -> None:
        self.db_path = Path(db_path) if db_path else None
        self.task_id = task_id
        self.task_type = task_type
        self.heartbeat_interval = heartbeat_interval

        if engine is not None:
            self.engine = engine
        else:
            self.engine = get_engine(db_path)
        init_progress_db(self.engine)

        self._start_time_sec = time.time()
        self._last_heartbeat_sec = 0.0
        self._items_processed = initial_items_processed
        self._items_total: Optional[int] = None
        self._bytes_processed = 0
        self._bytes_total: Optional[int] = None
        self._percentage: Optional[float] = None
        self._current_stage = "pending"
        self._status: TaskStatus = "running"
        self._metadata: dict[str, Any] = {}
        self._error_message: Optional[str] = None
        self._error_count = 0
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._live: Optional[Live] = None

        if self.task_id:
            from reconciliator.logging import register_task_tracker, set_current_task_id

            register_task_tracker(self.task_id, self)
            set_current_task_id(self.task_id)

    @property
    def error_count(self) -> int:
        """Current number of errors recorded for this task."""
        with self._lock:
            return self._error_count

    def close(self) -> None:
        """Dispose SQLAlchemy engine and release connection pool."""
        if self.task_id:
            from reconciliator.logging import (
                get_current_task_id,
                set_current_task_id,
                unregister_task_tracker,
            )

            unregister_task_tracker(self.task_id)
            if get_current_task_id() == self.task_id:
                set_current_task_id(None)
        if self.engine:
            self.engine.dispose()

    def start_task(
        self,
        stage: str = "starting",
        items_total: Optional[int] = None,
        bytes_total: Optional[int] = None,
        metadata: Optional[dict[str, Any]] = None,
        items_processed: Optional[int] = None,
    ) -> None:
        """Initialize task row in task_progress with running state and start the heartbeat thread."""
        with self._lock:
            self._start_time_sec = time.time()
            self._current_stage = stage
            self._items_total = items_total
            self._bytes_total = bytes_total
            if items_processed is not None:
                self._items_processed = items_processed
            if metadata:
                self._metadata.update(metadata)
            self._status = "running"
            self._heartbeat_now_locked()

        self._start_heartbeat_thread()

    def update_progress(
        self,
        stage: Optional[str] = None,
        items_processed: Optional[int] = None,
        items_increment: int = 0,
        items_total: Optional[int] = None,
        bytes_processed: Optional[int] = None,
        bytes_increment: int = 0,
        bytes_total: Optional[int] = None,
        percentage: Optional[float] = None,
        metadata: Optional[dict[str, Any]] = None,
        force: bool = False,
    ) -> None:
        """Update metrics in-memory; persists to database immediately if heartbeat interval elapsed."""
        now = time.time()
        should_persist = force or ((now - self._last_heartbeat_sec) >= self.heartbeat_interval)

        with self._lock:
            if stage is not None:
                self._current_stage = stage
            if items_processed is not None:
                self._items_processed = items_processed
            elif items_increment > 0:
                self._items_processed += items_increment

            if items_total is not None:
                self._items_total = items_total

            if bytes_processed is not None:
                self._bytes_processed = bytes_processed
            elif bytes_increment > 0:
                self._bytes_processed += bytes_increment

            if bytes_total is not None:
                self._bytes_total = bytes_total

            if percentage is not None:
                self._percentage = percentage

            if metadata:
                self._metadata.update(metadata)

            if should_persist:
                self._heartbeat_now_locked()

    def record_error(self, n: int = 1) -> None:
        """Increment the error counter for this task and force an immediate heartbeat."""
        with self._lock:
            self._error_count += n
            self._heartbeat_now_locked()

    def complete_task(
        self,
        metadata: Optional[dict[str, Any]] = None,
    ) -> None:
        """Transition task to completed status and persist terminal metrics."""
        self._stop_heartbeat_thread()
        with self._lock:
            self._status = "completed"
            self._current_stage = "done"
            if self._items_total is not None:
                self._items_processed = self._items_total
            if metadata:
                self._metadata.update(metadata)
            self._heartbeat_now_locked()
        self.close()

    def fail_task(
        self,
        error_message: str,
        metadata: Optional[dict[str, Any]] = None,
    ) -> None:
        """Transition task to failed status and log failure details."""
        self._stop_heartbeat_thread()
        with self._lock:
            self._status = "failed"
            self._current_stage = "error"
            self._error_message = error_message
            if metadata:
                self._metadata.update(metadata)
            self._heartbeat_now_locked()
        self.close()

    def interrupt_task(
        self,
        reason: str = "Task interrupted by user",
    ) -> None:
        """Transition task to interrupted status on SIGINT / cancellation."""
        self._stop_heartbeat_thread()
        with self._lock:
            self._status = "interrupted"
            self._current_stage = "interrupted"
            self._error_message = reason
            self._heartbeat_now_locked()
        self.close()

    def _calculate_metrics_locked(self) -> tuple[float, float]:
        """Compute rate_per_sec and percentage based on processed metrics and elapsed time."""
        elapsed = max(time.time() - self._start_time_sec, 0.001)
        rate = self._items_processed / elapsed if self._items_processed > 0 else 0.0

        if self._percentage is not None:
            pct = self._percentage
        elif self._items_total and self._items_total > 0:
            pct = min((self._items_processed / self._items_total) * 100.0, 100.0)
        elif self._bytes_total and self._bytes_total > 0:
            pct = min((self._bytes_processed / self._bytes_total) * 100.0, 100.0)
        else:
            pct = 0.0
        return rate, pct

    def _heartbeat_now_locked(self) -> None:
        """Execute dialect-aware UPSERT to sync task_progress table with current in-memory metrics."""
        now_str = _now_iso()
        rate, pct = self._calculate_metrics_locked()
        meta_str = json.dumps(self._metadata, ensure_ascii=False) if self._metadata else None

        values = {
            "task_id": self.task_id,
            "task_type": self.task_type,
            "status": self._status,
            "current_stage": self._current_stage,
            "items_processed": self._items_processed,
            "items_total": self._items_total,
            "bytes_processed": self._bytes_processed,
            "bytes_total": self._bytes_total,
            "rate_per_sec": rate,
            "percentage": pct,
            "start_time": datetime.fromtimestamp(self._start_time_sec, tz=timezone.utc).isoformat(),
            "last_heartbeat": now_str,
            "error_message": self._error_message,
            "error_count": self._error_count,
            "metadata_json": meta_str,
        }

        try:
            with self.engine.begin() as conn:
                dialect_name = conn.dialect.name
                if dialect_name == "postgresql":
                    stmt = pg_insert(task_progress_table).values(**values)
                    update_dict = {k: v for k, v in values.items() if k != "task_id"}
                    stmt = stmt.on_conflict_do_update(
                        index_elements=["task_id"],
                        set_=update_dict,
                    )
                    conn.execute(stmt)
                elif dialect_name == "sqlite":
                    from sqlalchemy.dialects.sqlite import insert as sqlite_insert

                    stmt = sqlite_insert(task_progress_table).values(**values)
                    update_dict = {k: v for k, v in values.items() if k != "task_id"}
                    stmt = stmt.on_conflict_do_update(
                        index_elements=["task_id"],
                        set_=update_dict,
                    )
                    conn.execute(stmt)
                else:
                    sel = select(task_progress_table.c.task_id).where(
                        task_progress_table.c.task_id == self.task_id
                    )
                    exists = conn.execute(sel).scalar_one_or_none()
                    if exists:
                        upd = (
                            update(task_progress_table)
                            .where(task_progress_table.c.task_id == self.task_id)
                            .values(**values)
                        )
                        conn.execute(upd)
                    else:
                        ins = task_progress_table.insert().values(**values)
                        conn.execute(ins)
            self._last_heartbeat_sec = time.time()
        except Exception as exc:
            logger.debug(f"Progress heartbeat write failed: {exc}")

    def _heartbeat_loop(self) -> None:
        """Background thread worker periodically syncing task status to database."""
        while not self._stop_event.wait(self.heartbeat_interval):
            with self._lock:
                if self._status == "running":
                    self._heartbeat_now_locked()

    def _start_heartbeat_thread(self) -> None:
        if self._thread is None or not self._thread.is_alive():
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._heartbeat_loop,
                daemon=True,
                name=f"heartbeat-{self.task_id}",
            )
            self._thread.start()

    def _stop_heartbeat_thread(self) -> None:
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
            self._thread = None


def get_task_progress(db_path: Path | str, task_id: str) -> Optional[TaskProgress]:
    """Retrieve TaskProgress telemetry record for a task_id."""
    engine = get_engine(db_path)
    init_progress_db(engine)
    try:
        with engine.connect() as conn:
            stmt = select(task_progress_table).where(task_progress_table.c.task_id == task_id)
            row = conn.execute(stmt).mappings().fetchone()
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
                error_count=row["error_count"] or 0,
                metadata_json=row["metadata_json"],
            )
    finally:
        engine.dispose()


def list_tasks(db_path: Path | str, limit: int = 50) -> list[TaskProgress]:
    """List recent tasks ordered by last heartbeat descending."""
    engine = get_engine(db_path)
    init_progress_db(engine)
    try:
        with engine.connect() as conn:
            stmt = (
                select(task_progress_table)
                .order_by(task_progress_table.c.last_heartbeat.desc())
                .limit(limit)
            )
            rows = conn.execute(stmt).mappings().fetchall()
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
                    error_count=r["error_count"] or 0,
                    metadata_json=r["metadata_json"],
                )
                for r in rows
            ]
    finally:
        engine.dispose()


def find_latest_task_for_source(
    db_path: Path | str,
    source: str,
    task_type: Optional[str] = None,
) -> Optional[TaskProgress]:
    """Locate the most recent task associated with a specific ingestion/reconciliation source."""
    engine = get_engine(db_path)
    init_progress_db(engine)
    try:
        with engine.connect() as conn:
            stmt = select(task_progress_table)
            if task_type:
                stmt = stmt.where(task_progress_table.c.task_type == task_type)
            stmt = stmt.order_by(task_progress_table.c.last_heartbeat.desc()).limit(100)
            rows = conn.execute(stmt).mappings().fetchall()

            for r in rows:
                meta_json = r["metadata_json"]
                if meta_json:
                    try:
                        m = json.loads(meta_json)
                        if m.get("source") == source:
                            return TaskProgress(
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
                                error_count=r["error_count"] or 0,
                                metadata_json=r["metadata_json"],
                            )
                    except Exception:
                        pass

                tid = str(r["task_id"])
                if source in tid:
                    return TaskProgress(
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
                        error_count=r["error_count"] or 0,
                        metadata_json=r["metadata_json"],
                    )
            return None
    finally:
        engine.dispose()


def mark_stale_tasks(
    db_path: Path | str,
    timeout_seconds: float = 30.0,
) -> int:
    """Transition tasks with missing heartbeats from 'running' to 'interrupted'."""
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=timeout_seconds)
    cutoff_iso = cutoff.isoformat()

    engine = get_engine(db_path)
    init_progress_db(engine)
    try:
        with engine.begin() as conn:
            stmt = (
                update(task_progress_table)
                .where(
                    task_progress_table.c.status == "running",
                    task_progress_table.c.last_heartbeat < cutoff_iso,
                )
                .values(
                    status="interrupted",
                    error_message=f"Task timed out (no heartbeat for >{timeout_seconds}s)",
                )
            )
            result = conn.execute(stmt)
            count = result.rowcount
            if count > 0:
                logger.warning(
                    f"Marked {count} stale tasks as interrupted (heartbeat < {timeout_seconds}s ago)"
                )
            return count
    finally:
        engine.dispose()


def prune_tasks(
    db_path: Path | str,
    task_id: Optional[str] = None,
    status: Optional[str] = None,
    older_than_seconds: Optional[float] = None,
    keep_last: Optional[int] = None,
    include_running: bool = False,
) -> int:
    """Prune task telemetry records from progress.db."""
    engine = get_engine(db_path)
    init_progress_db(engine)
    try:
        with engine.begin() as conn:
            conditions = []

            if not include_running:
                conditions.append(task_progress_table.c.status != "running")

            if task_id:
                conditions.append(task_progress_table.c.task_id == task_id)

            if status:
                if status == "terminated":
                    conditions.append(
                        task_progress_table.c.status.in_(["completed", "failed", "interrupted"])
                    )
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

            return max(deleted_count, 0)
    finally:
        engine.dispose()
