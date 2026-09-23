"""Integration and pipeline tests for OAI-PMH overlong value truncation and error telemetry."""

import logging
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from sqlalchemy.exc import DataError

from reconciliator.config import OAIIngestConfig
from reconciliator.database import WEMIRepository
from reconciliator.ingestion.oai import write_wemi_batch
from reconciliator.models import EntitySubject
from reconciliator.progress import (
    ProgressTracker,
    format_task_table,
    get_task_progress,
    list_tasks,
    render_telemetry_tui,
)


def test_write_wemi_batch_truncation_with_tracker() -> None:
    """Verify write_wemi_batch truncates fields and records error on ProgressTracker."""
    repo = MagicMock(spec=WEMIRepository)
    tracker = MagicMock(spec=ProgressTracker)
    tracker.task_id = "oai-harvest-test-1"

    long_label = "M" * 750
    subject = EntitySubject(
        entity_type="manifestation",
        entity_id=10,
        source="fennica",
        vocabulary="yso",
        concept_id="c999",
        raw_label=long_label,
    )

    payload = {
        "works": [MagicMock()],
        "manifestations": [],
        "entity_subjects": [subject],
        "record_origin": "fennica",
        "record_count": 1,
    }
    config = OAIIngestConfig(truncate_overlong_values=True)

    result = write_wemi_batch(
        repo=repo,
        payload=payload,
        config=config,
        task_id=tracker.task_id,
        tracker=tracker,
    )

    assert result == 1
    # Verify entity subject was truncated before repo.insert_wemi_batch was called
    assert len(subject.raw_label) == 512
    repo.insert_wemi_batch.assert_called_once()

    # Verify tracker recorded the error
    tracker.record_error.assert_called_once()


def test_write_wemi_batch_data_error_recovery(caplog: pytest.LogCaptureFixture) -> None:
    """Verify write_wemi_batch recovers from DataError when truncation enabled and logs with task_id."""
    repo = MagicMock(spec=WEMIRepository)
    repo.insert_wemi_batch.side_effect = DataError(
        "value too long for type character varying(50)", {}, None
    )

    tracker = MagicMock(spec=ProgressTracker)
    tracker.task_id = "task-recovering-1"

    payload = {
        "works": [MagicMock()],
        "record_origin": "test_src",
        "record_count": 1,
    }
    config = OAIIngestConfig(truncate_overlong_values=True)

    with caplog.at_level(logging.ERROR):
        result = write_wemi_batch(
            repo=repo,
            payload=payload,
            config=config,
            task_id=tracker.task_id,
            tracker=tracker,
        )

    # Must return 0 without raising
    assert result == 0
    assert "[validation] skip_batch wemi_batch" in caplog.text
    assert any(getattr(r, "task_id", None) == "task-recovering-1" for r in caplog.records)
    tracker.record_error.assert_called_once()


def test_write_wemi_batch_data_error_raises_when_truncation_disabled() -> None:
    """Verify write_wemi_batch raises DataError when truncate_overlong_values is False."""
    repo = MagicMock(spec=WEMIRepository)
    repo.insert_wemi_batch.side_effect = DataError("value too long", {}, None)

    payload = {
        "works": [MagicMock()],
        "record_origin": "test_src",
        "record_count": 1,
    }
    config = OAIIngestConfig(truncate_overlong_values=False)

    with pytest.raises(DataError):
        write_wemi_batch(
            repo=repo,
            payload=payload,
            config=config,
            task_id="task-fail",
        )


def test_progress_tracker_error_count_persistence(tmp_path: Path) -> None:
    """Verify error_count is persisted in progress.db and queryable via get_task_progress."""
    db_file = tmp_path / "progress.db"
    tracker = ProgressTracker(
        db_path=db_file,
        task_id="task-with-errors",
        task_type="oai_pmh_harvest",
    )
    tracker.start_task(items_total=100)
    assert tracker.error_count == 0

    tracker.record_error("Field too long in record 1")
    tracker.record_error("Field too long in record 2")
    assert tracker.error_count == 2

    # Query from db
    task = get_task_progress(db_file, "task-with-errors")
    assert task is not None
    assert task.error_count == 2

    tasks = list_tasks(db_file)
    assert len(tasks) == 1
    assert tasks[0].error_count == 2


def test_telemetry_monitor_renders_error_column(tmp_path: Path) -> None:
    """Verify render_telemetry_tui and format_task_table display Errors column."""
    db_file = tmp_path / "progress.db"
    tracker = ProgressTracker(
        db_path=db_file,
        task_id="oai-task-errors-view",
        task_type="oai_pmh_harvest",
    )
    tracker.start_task(items_total=50)
    tracker.record_error("Field truncated")
    tracker.record_error("Field truncated 2")

    tasks = list_tasks(db_file)
    assert len(tasks) == 1
    assert tasks[0].error_count == 2

    # Test format_task_table
    table = format_task_table(tasks)
    col_names = [col.header for col in table.columns]
    assert "Errors" in col_names

    # Test render_telemetry_tui
    tui = render_telemetry_tui(tasks, db_path=db_file)
    assert tui is not None
