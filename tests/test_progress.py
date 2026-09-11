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
    render_telemetry_tui,
    run_telemetry_tui,
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


def test_render_telemetry_tui_empty(progress_db: Path) -> None:
    tui = render_telemetry_tui([], db_path=progress_db, interval=1.0)
    assert tui is not None


def test_render_telemetry_tui_active_and_failed(progress_db: Path) -> None:
    tracker1 = ProgressTracker(
        db_path=progress_db,
        task_id="task-running",
        task_type="wikidata_ingest",
    )
    tracker1.start_task(items_total=500)
    tracker1.update_progress(items_processed=100, bytes_processed=1024 * 1024 * 5, force=True)

    tracker2 = ProgressTracker(
        db_path=progress_db,
        task_id="task-failed",
        task_type="oai_pmh_harvest",
    )
    tracker2.start_task(items_total=100)
    tracker2.fail_task("Connection refused by endpoint")

    tasks = list_tasks(progress_db)
    assert len(tasks) == 2

    tui = render_telemetry_tui(tasks, db_path=progress_db, interval=0.5)
    assert tui is not None


def test_run_telemetry_tui(progress_db: Path) -> None:
    tracker = ProgressTracker(
        db_path=progress_db,
        task_id="task-done",
        task_type="reconciliation",
    )
    tracker.start_task(items_total=10)
    tracker.complete_task()

    from rich.console import Console

    console = Console(record=True, width=120)
    run_telemetry_tui(
        fetch_tasks=lambda: list_tasks(progress_db),
        db_path=progress_db,
        interval=0.1,
        console=console,
    )
    output = console.export_text()
    assert "RECONCILIATOR" in output
    assert "TASK TELEMETRY MONITOR" in output


def test_cli_status_watch_tui(tmp_path: Path) -> None:
    from typer.testing import CliRunner

    from reconciliator.cli import app

    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        f"""
storage:
  authority_db: "{tmp_path}/auth.db"
  finto_db: "{tmp_path}/finto.db"
  progress_db: "{tmp_path}/prog.db"
  index_path: "{tmp_path}/auth.faiss"
telemetry:
  watch_interval_seconds: 0.1
""",
        encoding="utf-8",
    )

    # Initialize a completed task so watch terminates
    tracker = ProgressTracker(
        db_path=tmp_path / "prog.db",
        task_id="task-cli-watch",
        task_type="reconciliation",
    )
    tracker.start_task(items_total=10)
    tracker.complete_task()

    runner = CliRunner()
    result = runner.invoke(app, ["status", "--config", str(cfg_file), "--watch"])
    assert result.exit_code == 0
    assert "RECONCILIATOR" in result.output
    assert "TASK TELEMETRY MONITOR" in result.output


def test_cli_status_watch_json(tmp_path: Path) -> None:
    from typer.testing import CliRunner

    from reconciliator.cli import app

    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        f"""
storage:
  authority_db: "{tmp_path}/auth.db"
  finto_db: "{tmp_path}/finto.db"
  progress_db: "{tmp_path}/prog.db"
  index_path: "{tmp_path}/auth.faiss"
telemetry:
  watch_interval_seconds: 0.1
""",
        encoding="utf-8",
    )

    tracker = ProgressTracker(
        db_path=tmp_path / "prog.db",
        task_id="task-cli-json",
        task_type="reconciliation",
    )
    tracker.start_task(items_total=5)
    tracker.complete_task()

    runner = CliRunner()
    result = runner.invoke(app, ["status", "--config", str(cfg_file), "--watch", "--json"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert len(data) == 1
    assert data[0]["task_id"] == "task-cli-json"
