"""Unit tests for ProgressTracker, SQLite heartbeats, and status formatters."""

import json
from pathlib import Path

import pytest

from reconciliator.progress import (
    ProgressTracker,
    format_task_table,
    get_task_progress,
    list_tasks,
    render_status_json,
)


@pytest.fixture
def progress_db(tmp_path: Path) -> Path:
    return tmp_path / "progress.db"


def test_progress_lifecycle(progress_db: Path) -> None:
    tracker = ProgressTracker(
        db_path=progress_db,
        task_id="task-1",
        task_type="reconciliation",
        heartbeat_interval=0.01,
    )

    tracker.start_task(stage="parsing", items_total=100)
    task = get_task_progress(progress_db, "task-1")
    assert task is not None
    assert task.status == "running"
    assert task.current_stage == "parsing"
    assert task.items_total == 100

    tracker.update_progress(stage="matching", items_processed=50, force=True)
    task = get_task_progress(progress_db, "task-1")
    assert task is not None
    assert task.current_stage == "matching"
    assert task.items_processed == 50
    assert task.percentage == 50.0

    tracker.complete_task(metadata={"completed": True})
    task = get_task_progress(progress_db, "task-1")
    assert task is not None
    assert task.status == "completed"


def test_table_and_json_rendering(progress_db: Path) -> None:
    tracker = ProgressTracker(
        db_path=progress_db,
        task_id="task-2",
        task_type="wikidata_ingest",
    )
    tracker.start_task(items_total=1000)
    tracker.update_progress(items_processed=250, force=True)

    tasks = list_tasks(progress_db)
    assert len(tasks) == 1

    # Format table
    table = format_task_table(tasks)
    assert table is not None

    # Render JSON
    json_str = render_status_json(tasks)
    data = json.loads(json_str)
    assert len(data) == 1
    assert data[0]["task_id"] == "task-2"
    assert data[0]["items_processed"] == 250
