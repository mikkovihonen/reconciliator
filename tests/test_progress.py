"""Unit tests for ProgressTracker, database heartbeats, and status formatters."""

import json
from pathlib import Path

import pytest

from reconciliator.config import load_config
from reconciliator.progress import (
    ProgressTracker,
    format_task_table,
    get_task_progress,
    list_tasks,
    parse_duration_seconds,
    prune_tasks,
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


def test_progress_large_byte_totals(progress_db: Path) -> None:
    """Verify bytes_total and bytes_processed exceeding 32-bit signed integer (>2.14GB, e.g. 156GB) do not overflow."""
    dump_size_156gb = 156_008_813_215
    tracker = ProgressTracker(
        db_path=progress_db,
        task_id="task-large-dump",
        task_type="wikidata_ingest",
        heartbeat_interval=0.01,
    )

    tracker.start_task(stage="wikidata_prescan", bytes_total=dump_size_156gb)
    task = get_task_progress(progress_db, "task-large-dump")
    assert task is not None
    assert task.bytes_total == dump_size_156gb

    tracker.update_progress(bytes_processed=50_000_000_000, force=True)
    task = get_task_progress(progress_db, "task-large-dump")
    assert task is not None
    assert task.bytes_processed == 50_000_000_000

    tracker.complete_task()
    task = get_task_progress(progress_db, "task-large-dump")
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
        exit_when_done=True,
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
  data_dir: "{tmp_path}"
telemetry:
  watch_interval_seconds: 0.1
""",
        encoding="utf-8",
    )
    conf = load_config(cfg_file)

    # Initialize a completed task so watch terminates
    tracker = ProgressTracker(
        db_path=conf.storage.database_url,
        task_id="task-cli-watch",
        task_type="reconciliation",
    )
    tracker.start_task(items_total=10)
    tracker.complete_task()

    runner = CliRunner()
    result = runner.invoke(
        app, ["status", "--config", str(cfg_file), "--watch", "--exit-when-done"]
    )
    assert result.exit_code == 0
    assert "RECONCILIATOR" in result.output
    assert "TASK TELEMETRY MONITOR" in result.output


def test_cli_status_watch_alias(tmp_path: Path) -> None:
    from typer.testing import CliRunner

    from reconciliator.cli import app

    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        f"""
storage:
  data_dir: "{tmp_path}"
telemetry:
  watch_interval_seconds: 0.1
""",
        encoding="utf-8",
    )
    conf = load_config(cfg_file)

    tracker = ProgressTracker(
        db_path=conf.storage.database_url,
        task_id="task-cli-watch-alias",
        task_type="reconciliation",
    )
    tracker.start_task(items_total=10)
    tracker.complete_task()

    runner = CliRunner()
    result = runner.invoke(app, ["status", "watch", "--config", str(cfg_file), "--exit-when-done"])
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
  data_dir: "{tmp_path}"
telemetry:
  watch_interval_seconds: 0.1
""",
        encoding="utf-8",
    )
    conf = load_config(cfg_file)

    tracker = ProgressTracker(
        db_path=conf.storage.database_url,
        task_id="task-cli-json",
        task_type="reconciliation",
    )
    tracker.start_task(items_total=5)
    tracker.complete_task()

    runner = CliRunner()
    result = runner.invoke(
        app, ["status", "--config", str(cfg_file), "--watch", "--json", "--exit-when-done"]
    )
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert len(data) == 1
    assert data[0]["task_id"] == "task-cli-json"


def test_parse_duration_seconds() -> None:
    assert parse_duration_seconds("7d") == 7 * 86400.0
    assert parse_duration_seconds("2days") == 2 * 86400.0
    assert parse_duration_seconds("1day") == 86400.0
    assert parse_duration_seconds("24h") == 24 * 3600.0
    assert parse_duration_seconds("2hours") == 7200.0
    assert parse_duration_seconds("1hr") == 3600.0
    assert parse_duration_seconds("30m") == 1800.0
    assert parse_duration_seconds("15min") == 900.0
    assert parse_duration_seconds("45minutes") == 2700.0
    assert parse_duration_seconds("60s") == 60.0
    assert parse_duration_seconds("10secs") == 10.0
    assert parse_duration_seconds("5seconds") == 5.0
    assert parse_duration_seconds("3") == 3 * 86400.0

    with pytest.raises(ValueError):
        parse_duration_seconds("")

    with pytest.raises(ValueError):
        parse_duration_seconds("invalid_str")


def test_prune_tasks_filtering(progress_db: Path) -> None:
    # 1. Test pruning non-existent DB
    assert prune_tasks(progress_db / "non_existent.db") == 0

    # 2. Setup 3 tasks: completed, failed, running
    t_comp = ProgressTracker(progress_db, task_id="comp-1", task_type="reconciliation")
    t_comp.start_task()
    t_comp.complete_task()

    t_fail = ProgressTracker(progress_db, task_id="fail-1", task_type="wikidata_ingest")
    t_fail.start_task()
    t_fail.fail_task("Fatal network error")

    t_run = ProgressTracker(progress_db, task_id="run-1", task_type="oai_pmh_harvest")
    t_run.start_task()

    assert len(list_tasks(progress_db)) == 3

    # Prune running without include_running should NOT prune anything
    deleted = prune_tasks(progress_db, task_id="run-1", include_running=False)
    assert deleted == 0
    assert len(list_tasks(progress_db)) == 3

    # Prune completed
    deleted = prune_tasks(progress_db, status="completed")
    assert deleted == 1
    tasks = list_tasks(progress_db)
    assert len(tasks) == 2
    assert {t.task_id for t in tasks} == {"fail-1", "run-1"}

    # Prune failed
    deleted = prune_tasks(progress_db, status="failed")
    assert deleted == 1
    tasks = list_tasks(progress_db)
    assert len(tasks) == 1
    assert tasks[0].task_id == "run-1"

    # Prune running with include_running=True
    deleted = prune_tasks(progress_db, include_running=True)
    assert deleted == 1
    assert len(list_tasks(progress_db)) == 0


def test_prune_tasks_older_than_and_keep_last(progress_db: Path) -> None:
    from datetime import datetime, timedelta, timezone

    from reconciliator.database import get_db_connection

    # Insert 4 completed tasks with manual timestamps
    now = datetime.now(timezone.utc)
    t1_time = (now - timedelta(days=10)).isoformat()
    t2_time = (now - timedelta(days=5)).isoformat()
    t3_time = (now - timedelta(days=2)).isoformat()
    t4_time = (now - timedelta(hours=1)).isoformat()

    with get_db_connection(progress_db) as conn:
        from reconciliator.progress import init_progress_db

        init_progress_db(conn)
        for tid, hb in [("t1", t1_time), ("t2", t2_time), ("t3", t3_time), ("t4", t4_time)]:
            conn.exec_driver_sql(
                """
                INSERT INTO task_progress (task_id, task_type, status, current_stage, start_time, last_heartbeat)
                VALUES (%s, 'reconciliation', 'completed', 'done', %s, %s);
                """,
                (tid, hb, hb),
            )

    assert len(list_tasks(progress_db)) == 4

    # Prune older than 3 days: should delete t1 (10d) and t2 (5d)
    deleted = prune_tasks(progress_db, older_than_seconds=3 * 86400)
    assert deleted == 2
    tasks = list_tasks(progress_db)
    assert len(tasks) == 2
    assert {t.task_id for t in tasks} == {"t3", "t4"}

    # Insert t5 and t6
    t5_time = (now - timedelta(minutes=10)).isoformat()
    t6_time = now.isoformat()
    with get_db_connection(progress_db) as conn:
        for tid, hb in [("t5", t5_time), ("t6", t6_time)]:
            conn.exec_driver_sql(
                """
                INSERT INTO task_progress (task_id, task_type, status, current_stage, start_time, last_heartbeat)
                VALUES (%s, 'reconciliation', 'completed', 'done', %s, %s);
                """,
                (tid, hb, hb),
            )

    assert len(list_tasks(progress_db)) == 4  # t3, t4, t5, t6

    # Keep last 2: should delete t3 and t4, keeping t5 and t6
    deleted = prune_tasks(progress_db, keep_last=2)
    assert deleted == 2
    tasks = list_tasks(progress_db)
    assert len(tasks) == 2
    assert {t.task_id for t in tasks} == {"t5", "t6"}


def test_cli_prune_commands(tmp_path: Path) -> None:
    from typer.testing import CliRunner

    from reconciliator.cli import app

    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        f"""
storage:
  data_dir: "{tmp_path}"
""",
        encoding="utf-8",
    )
    conf = load_config(cfg_file)
    db_target = conf.storage.database_url

    runner = CliRunner()

    # 1. No flags provided -> BadParameter
    res = runner.invoke(app, ["prune", "--config", str(cfg_file)])
    assert res.exit_code != 0
    assert "No prune filter specified" in res.output

    # 2. Both completed and failed -> BadParameter
    res = runner.invoke(app, ["prune", "--config", str(cfg_file), "--completed", "--failed"])
    assert res.exit_code != 0
    assert "Cannot specify both --completed and --failed" in res.output

    # 3. Invalid duration -> BadParameter
    res = runner.invoke(
        app, ["prune", "--config", str(cfg_file), "--older-than", "invalid_dur", "-y"]
    )
    assert res.exit_code != 0

    # 4. Confirmation aborted
    t_comp = ProgressTracker(db_target, task_id="comp-cli", task_type="reconciliation")
    t_comp.start_task()
    t_comp.complete_task()
    res = runner.invoke(app, ["prune", "--config", str(cfg_file), "--completed"], input="n\n")
    assert res.exit_code != 0
    assert "Prune aborted" in res.output

    # 5. Successful prune with -y
    res = runner.invoke(app, ["prune", "--config", str(cfg_file), "--completed", "-y"])
    assert res.exit_code == 0
    assert "Successfully pruned 1 task(s)" in res.output

    # 6. Test status --prune
    t_comp = ProgressTracker(db_target, task_id="comp-status", task_type="reconciliation")
    t_comp.start_task()
    t_comp.complete_task()
    res = runner.invoke(app, ["status", "--config", str(cfg_file), "--prune"])
    assert res.exit_code == 0
    assert "Pruned 1 completed task(s)" in res.output


def test_telemetry_clarification_display(progress_db: Path) -> None:
    tracker = ProgressTracker(
        db_path=progress_db,
        task_id="melinda-ingest-test",
        task_type="oai_pmh_harvest",
    )
    tracker.start_task(stage="harvest_and_index", metadata={"harvested": 0, "indexed": 0})
    tracker.update_progress(
        stage="harvest_and_index",
        items_processed=1000,
        metadata={"harvested": 1500, "indexed": 1000},
        force=True,
    )

    tasks = list_tasks(progress_db)
    assert len(tasks) == 1
    assert tasks[0].current_stage == "harvest_and_index"

    # Test table formatting
    from rich.console import Console

    console = Console(record=True, width=200)
    table = format_task_table(tasks)
    console.print(table)
    table_text = console.export_text()
    assert "1,000 indexed" in table_text
    assert "1,500" in table_text
    assert "harvest_and_index" in table_text

    # Test TUI rendering
    tui = render_telemetry_tui(tasks, db_path=progress_db)
    console = Console(record=True, width=200)
    console.print(tui)
    tui_text = console.export_text()
    assert "1,000 indexed" in tui_text
    assert "1,500" in tui_text
    assert "harvest_and_index" in tui_text


def test_telemetry_skipped_display(progress_db: Path) -> None:
    tracker = ProgressTracker(
        db_path=progress_db,
        task_id="melinda-ingest-skip-test",
        task_type="oai_pmh_harvest",
    )
    tracker.start_task(
        stage="harvest_and_index", metadata={"harvested": 0, "indexed": 0, "skipped": 0}
    )
    tracker.update_progress(
        stage="harvest_and_index",
        items_processed=1000,
        metadata={"harvested": 1500, "indexed": 1000, "skipped": 450},
        force=True,
    )

    tasks = list_tasks(progress_db)
    assert len(tasks) == 1

    from rich.console import Console

    console = Console(record=True, width=200)
    table = format_task_table(tasks)
    console.print(table)
    table_text = console.export_text()
    assert "1,000 indexed, 450 skipped (1,500 fetched)" in table_text

    tui = render_telemetry_tui(tasks, db_path=progress_db)
    console = Console(record=True, width=200)
    console.print(tui)
    tui_text = console.export_text()
    assert "1,000 indexed, 450 skipped (1,500 fetched)" in tui_text
    assert "Skipped: 450" in tui_text


def test_render_vector_daemon_panel_unsaved_rendering() -> None:
    from rich.console import Console

    from reconciliator.progress import render_vector_daemon_panel

    status = {
        "status": "running",
        "pid": 12345,
        "device": "mps",
        "host_ram_mb": 512.0,
        "uptime_seconds": 120,
        "idle_seconds": 10,
        "active_clients": 1,
        "indices": [
            {
                "key": "authority",
                "total_vectors": 1000,
                "dimension": 768,
                "dtype": "float32",
                "backend": "torch",
                "searches_total": 5,
                "adds_total": 10,
                "is_dirty": True,
            }
        ],
    }
    panel = render_vector_daemon_panel(status)
    console = Console(record=True, width=120)
    console.print(panel)
    text = console.export_text()
    assert "*UNSAVED" in text
    assert "[bold red]" not in text


def test_telemetry_slices_display(progress_db: Path) -> None:
    from rich.console import Console

    tracker = ProgressTracker(
        db_path=progress_db,
        task_id="oai-ingest-partition-test",
        task_type="oai_pmh_harvest",
    )
    tracker.start_task(
        stage="harvest_and_index",
        metadata={"harvested": 0, "indexed": 0, "skipped": 0, "slices_done": 0, "slices_total": 10},
    )
    tracker.update_progress(
        stage="harvest_and_index",
        items_processed=197,
        percentage=25.0,
        metadata={
            "harvested": 197,
            "indexed": 197,
            "skipped": 0,
            "slices_done": 5,
            "slices_total": 20,
        },
        force=True,
    )

    tasks = list_tasks(progress_db)
    assert len(tasks) == 1
    task = tasks[0]
    assert task.percentage == 25.0
    assert task.items_processed == 197

    console = Console(record=True, width=200)
    table = format_task_table(tasks)
    console.print(table)
    table_text = console.export_text()
    assert "197 indexed, 0 skipped (197 fetched) [5/20 slices]" in table_text
    assert "25.0%" in table_text

    tui = render_telemetry_tui(tasks, db_path=progress_db)
    console = Console(record=True, width=200)
    console.print(tui)
    tui_text = console.export_text()
    assert "197 indexed, 0 skipped (197 fetched) [5/20 slices]" in tui_text
    assert "25.0%" in tui_text
