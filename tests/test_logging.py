"""Unit tests for centralized task-context logging, TaskLogFilter, and TelemetryLogHandler."""

import logging
from unittest.mock import MagicMock

import pytest

from reconciliator.config import LoggingConfig, configure_logging
from reconciliator.logging import (
    TaskLogFilter,
    TelemetryLogHandler,
    get_current_task_id,
    init_worker_logging,
    log_sanitization,
    log_validation,
    register_task_tracker,
    set_current_task_id,
    task_context,
    unregister_task_tracker,
)


@pytest.fixture(autouse=True)
def _reset_task_context():
    set_current_task_id(None)
    yield
    set_current_task_id(None)


def test_task_id_context() -> None:
    """Verify set_current_task_id, get_current_task_id, and task_context."""
    assert get_current_task_id() == "-"

    token = set_current_task_id("task-100")
    assert get_current_task_id() == "task-100"

    with task_context("task-200"):
        assert get_current_task_id() == "task-200"

    assert get_current_task_id() == "task-100"
    set_current_task_id(None)
    assert get_current_task_id() == "-"


def test_task_log_filter() -> None:
    """Verify TaskLogFilter attaches record.task_id."""
    filter_ = TaskLogFilter(default_task_id="no-task")
    record = logging.LogRecord("test", logging.INFO, "test.py", 1, "test message", (), None)

    with task_context("active-task-99"):
        filter_.filter(record)
        assert record.task_id == "active-task-99"

    record_no_task = logging.LogRecord(
        "test", logging.INFO, "test.py", 1, "test message", (), None
    )
    filter_.filter(record_no_task)
    assert record_no_task.task_id == "no-task"


def test_telemetry_log_handler_notifies_tracker() -> None:
    """Verify TelemetryLogHandler intercepts ERROR logs and increments tracker error count."""
    tracker = MagicMock()
    register_task_tracker("test-task-err", tracker)
    try:
        handler = TelemetryLogHandler(level=logging.ERROR)
        record_info = logging.LogRecord(
            "test", logging.INFO, "test.py", 1, "info msg", (), None
        )
        record_info.task_id = "test-task-err"
        handler.emit(record_info)
        tracker.record_error.assert_not_called()

        record_err = logging.LogRecord(
            "test", logging.ERROR, "test.py", 1, "error msg", (), None
        )
        record_err.task_id = "test-task-err"
        handler.emit(record_err)
        tracker.record_error.assert_called_once_with(n=1)
    finally:
        unregister_task_tracker("test-task-err")


def test_log_sanitization_and_validation_formats(caplog: pytest.LogCaptureFixture) -> None:
    """Verify structured prefixes and context task_id in log_sanitization and log_validation."""
    logger = logging.getLogger("reconciliator.test")
    tracker = MagicMock()

    with caplog.at_level(logging.ERROR):
        with task_context("task-structured-1"):
            log_sanitization(
                logger,
                action="truncate",
                entity="test_table.col",
                source_id="item-123",
                reason="exceeded limit",
                tracker=tracker,
            )
            log_validation(
                logger,
                action="quarantine",
                entity="test_table.fk",
                source_id="item-456",
                reason="unresolvable parent",
                tracker=tracker,
            )

    assert "[sanitization] truncate test_table.col (source_id=item-123): exceeded limit" in caplog.text
    assert "[validation] quarantine test_table.fk (source_id=item-456): unresolvable parent" in caplog.text
    assert tracker.record_error.call_count == 2


def _sample_worker_task(task_id: str) -> tuple[str, int]:
    from reconciliator.logging import get_current_task_id, init_worker_logging

    init_worker_logging(task_id)
    return get_current_task_id(), 1


def test_worker_logging_initialization() -> None:
    """Verify init_worker_logging sets task_id in current context."""
    init_worker_logging(task_id="worker-task-77")
    assert get_current_task_id() == "worker-task-77"
    set_current_task_id(None)


def test_multiprocess_worker_logging() -> None:
    """Verify task_id is initialized and returned across ProcessPoolExecutor."""
    from concurrent.futures import ProcessPoolExecutor

    with ProcessPoolExecutor(max_workers=1) as pool:
        future = pool.submit(_sample_worker_task, "mp-task-999")
        worker_task_id, err_cnt = future.result()
    assert worker_task_id == "mp-task-999"
    assert err_cnt == 1


def test_child_logger_formatting_with_task_id() -> None:
    """Verify child loggers format without KeyError('task_id') after configure_logging."""
    import io

    stream = io.StringIO()
    cfg = LoggingConfig(format="[task_id=%(task_id)s] %(message)s")
    configure_logging(cfg)

    root = logging.getLogger()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter(fmt=cfg.format))
    handler.addFilter(TaskLogFilter())
    root.addHandler(handler)

    try:
        child_logger = logging.getLogger("reconciliator.ingestion.wikidata")
        with task_context("wikidata-test-task"):
            child_logger.info("Pass 1/2: Pre-scanning author entities...")

        output = stream.getvalue()
        assert "[task_id=wikidata-test-task] Pass 1/2: Pre-scanning author entities..." in output
    finally:
        root.removeHandler(handler)
