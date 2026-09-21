"""Centralized logging subsystem with task-context propagation and telemetry integration."""

from __future__ import annotations

import contextlib
import contextvars
import logging
import logging.handlers
import threading
from typing import TYPE_CHECKING, Any, Literal, Optional

if TYPE_CHECKING:
    from reconciliator.progress import ProgressTracker

_DEFAULT_TASK_ID = "-"
_current_task_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "current_task_id", default=_DEFAULT_TASK_ID
)

_tracker_lock = threading.Lock()
_active_trackers: dict[str, ProgressTracker] = {}


def get_current_task_id() -> str:
    """Return the active task ID from the current context, or '-' if none set."""
    tid = _current_task_id.get()
    return tid if tid else _DEFAULT_TASK_ID


def set_current_task_id(task_id: Optional[str]) -> contextvars.Token[str]:
    """Set the active task ID for the current context."""
    val = str(task_id).strip() if task_id else _DEFAULT_TASK_ID
    return _current_task_id.set(val)


def reset_current_task_id(token: contextvars.Token[str]) -> None:
    """Reset the task ID context variable to its previous value."""
    _current_task_id.reset(token)


@contextlib.contextmanager
def task_context(task_id: Optional[str]):
    """Context manager setting task_id for the duration of the enclosed block."""
    token = set_current_task_id(task_id)
    try:
        yield
    finally:
        reset_current_task_id(token)


def register_task_tracker(task_id: str, tracker: ProgressTracker) -> None:
    """Register an active ProgressTracker so logging handlers can dispatch telemetry."""
    if not task_id:
        return
    with _tracker_lock:
        _active_trackers[task_id] = tracker


def unregister_task_tracker(task_id: str) -> None:
    """Unregister a completed or closed ProgressTracker."""
    if not task_id:
        return
    with _tracker_lock:
        _active_trackers.pop(task_id, None)


def get_task_tracker(task_id: str) -> Optional[ProgressTracker]:
    """Retrieve an active tracker for a given task_id if registered."""
    if not task_id:
        return None
    with _tracker_lock:
        return _active_trackers.get(task_id)





class TaskLogFilter(logging.Filter):
    """Logging filter that injects the current task_id into all LogRecords."""

    def __init__(self, default_task_id: str = _DEFAULT_TASK_ID) -> None:
        super().__init__()
        self.default_task_id = default_task_id

    def filter(self, record: logging.LogRecord) -> bool:
        task_id = getattr(record, "task_id", None)
        if not task_id or task_id == _DEFAULT_TASK_ID:
            ctx_id = get_current_task_id()
            record.task_id = ctx_id if ctx_id != _DEFAULT_TASK_ID else self.default_task_id
        return True


class TaskLogFormatter(logging.Formatter):
    """Logging formatter ensuring record.task_id exists to prevent KeyError in format strings."""

    def __init__(
        self,
        fmt: Optional[str] = None,
        datefmt: Optional[str] = None,
        default_task_id: str = _DEFAULT_TASK_ID,
    ) -> None:
        super().__init__(fmt=fmt, datefmt=datefmt)
        self.default_task_id = default_task_id

    def format(self, record: logging.LogRecord) -> str:
        if not hasattr(record, "task_id") or not getattr(record, "task_id", None):
            ctx_id = get_current_task_id()
            record.task_id = ctx_id if ctx_id != _DEFAULT_TASK_ID else self.default_task_id
        return super().format(record)



class TelemetryLogHandler(logging.Handler):
    """Logging handler that intercepts ERROR+ records and feeds active task telemetry."""

    def __init__(self, level: int = logging.ERROR) -> None:
        super().__init__(level=level)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if record.levelno < logging.ERROR:
                return
            task_id = getattr(record, "task_id", None) or get_current_task_id()
            if not task_id or task_id == _DEFAULT_TASK_ID:
                return
            tracker = get_task_tracker(task_id)
            if tracker is not None:
                tracker.record_error(n=1)
        except Exception:
            self.handleError(record)


def log_sanitization(
    logger: logging.Logger,
    *,
    action: str,
    entity: str,
    source_id: Optional[str] = None,
    reason: str,
    task_id: Optional[str] = None,
    tracker: Optional[ProgressTracker] = None,
    level: Optional[str | int] = None,
) -> None:
    """Emit a standardized sanitization log event and record error telemetry if applicable."""
    sid_part = f" (source_id={source_id})" if source_id else ""
    msg = f"[sanitization] {action} {entity}{sid_part}: {reason}"

    # Determine log level
    if level is None:
        log_level = logging.ERROR
    elif isinstance(level, str):
        log_level = getattr(logging, level.upper(), logging.ERROR)
    else:
        log_level = level

    # Propagate explicit task_id into context if passed
    effective_task_id = task_id or get_current_task_id()
    token = set_current_task_id(effective_task_id) if task_id else None
    try:
        logger.log(log_level, msg, extra={"task_id": effective_task_id})
    finally:
        if token:
            reset_current_task_id(token)

    # If an explicit tracker is provided and not already registered under effective_task_id, record error
    if tracker is not None and log_level >= logging.ERROR:
        if get_task_tracker(effective_task_id) is not tracker:
            tracker.record_error(
                message=msg,
                metadata={
                    "last_error_task_id": effective_task_id,
                    "last_truncated_task_id": effective_task_id,
                    "last_truncated_field": entity,
                    "action": action,
                    "entity": entity,
                },
                n=1,
            )


def log_validation(
    logger: logging.Logger,
    *,
    action: str,
    entity: str,
    source_id: Optional[str] = None,
    reason: str,
    task_id: Optional[str] = None,
    tracker: Optional[ProgressTracker] = None,
    level: Optional[str | int] = None,
) -> None:
    """Emit a standardized validation log event and record error telemetry if applicable."""
    sid_part = f" (source_id={source_id})" if source_id else ""
    msg = f"[validation] {action} {entity}{sid_part}: {reason}"

    if level is None:
        log_level = logging.ERROR
    elif isinstance(level, str):
        log_level = getattr(logging, level.upper(), logging.ERROR)
    else:
        log_level = level

    effective_task_id = task_id or get_current_task_id()
    token = set_current_task_id(effective_task_id) if task_id else None
    try:
        logger.log(log_level, msg, extra={"task_id": effective_task_id})
    finally:
        if token:
            reset_current_task_id(token)

    if tracker is not None and log_level >= logging.ERROR:
        if get_task_tracker(effective_task_id) is not tracker:
            tracker.record_error(
                message=msg,
                metadata={
                    "last_error_task_id": effective_task_id,
                    "action": action,
                    "entity": entity,
                },
                n=1,
            )


def init_worker_logging(
    task_id: Optional[str] = None,
    queue: Optional[Any] = None,
    default_task_id: str = _DEFAULT_TASK_ID,
) -> None:
    """Initialize logging inside a multiprocessing worker process."""
    if task_id:
        set_current_task_id(task_id)

    root = logging.getLogger()
    task_filter = TaskLogFilter(default_task_id=default_task_id)
    if not any(isinstance(f, TaskLogFilter) for f in root.filters):
        root.addFilter(task_filter)

    if queue is not None:
        # Route worker logs to parent process Queue
        qh = logging.handlers.QueueHandler(queue)
        qh.addFilter(task_filter)
        root.handlers.clear()
        root.addHandler(qh)
