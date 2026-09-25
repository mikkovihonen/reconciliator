"""Unit tests for harvest task ID adoption, CLI options, and stale task telemetry lifecycle."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch

from typer.testing import CliRunner

from reconciliator.commands import app
from reconciliator.config import (
    OAIIngestConfig,
    StorageConfig,
    TelemetryConfig,
    get_bundled_defaults,
)
from reconciliator.progress import (
    ProgressTracker,
    find_latest_task_for_source,
    get_task_progress,
    mark_stale_tasks,
    prune_tasks,
)


def test_config_surface_defaults() -> None:
    """Verify defaults.yaml and typed Pydantic models expose adopt_previous_task_id and timeouts."""
    bundled = get_bundled_defaults()
    assert "ingestion" in bundled
    assert bundled["ingestion"]["oai_defaults"]["adopt_previous_task_id"] is True

    assert "telemetry" in bundled
    assert bundled["telemetry"]["adopt_previous_task_id"] is True
    assert bundled["telemetry"]["stale_heartbeat_timeout_seconds"] == 120.0

    oai_cfg = OAIIngestConfig()
    assert oai_cfg.adopt_previous_task_id is True

    telemetry_cfg = TelemetryConfig()
    assert telemetry_cfg.adopt_previous_task_id is True
    assert telemetry_cfg.stale_heartbeat_timeout_seconds == 120.0


def test_mark_stale_tasks(tmp_storage: StorageConfig) -> None:
    """Verify running tasks with heartbeats older than timeout are marked interrupted."""
    db_url = tmp_storage.database_url
    now = datetime.now(timezone.utc)
    stale_time = (now - timedelta(seconds=200)).isoformat()
    fresh_time = (now - timedelta(seconds=10)).isoformat()

    tracker1 = ProgressTracker(db_path=db_url, task_id="task-stale-1", task_type="oai_pmh_harvest")
    tracker1.start_task(stage="harvest")
    # Manually backdate tracker1 heartbeat
    tracker1.update_progress(force=True)
    with tracker1.engine.begin() as conn:
        from sqlalchemy import update

        from reconciliator.database import task_progress_table

        conn.execute(
            update(task_progress_table)
            .where(task_progress_table.c.task_id == "task-stale-1")
            .values(last_heartbeat=stale_time)
        )

    tracker2 = ProgressTracker(db_path=db_url, task_id="task-fresh-2", task_type="oai_pmh_harvest")
    tracker2.start_task(stage="harvest")
    with tracker2.engine.begin() as conn:
        conn.execute(
            update(task_progress_table)
            .where(task_progress_table.c.task_id == "task-fresh-2")
            .values(last_heartbeat=fresh_time)
        )

    tracker3 = ProgressTracker(
        db_path=db_url, task_id="task-completed-3", task_type="oai_pmh_harvest"
    )
    tracker3.start_task(stage="harvest")
    tracker3.complete_task()
    with tracker3.engine.begin() as conn:
        conn.execute(
            update(task_progress_table)
            .where(task_progress_table.c.task_id == "task-completed-3")
            .values(last_heartbeat=stale_time)
        )

    # Run mark_stale_tasks with 120s timeout
    updated = mark_stale_tasks(db_url, timeout_seconds=120.0)
    assert updated == 1

    t1 = get_task_progress(db_url, "task-stale-1")
    assert t1 is not None
    assert t1.status == "interrupted"

    t2 = get_task_progress(db_url, "task-fresh-2")
    assert t2 is not None
    assert t2.status == "running"

    t3 = get_task_progress(db_url, "task-completed-3")
    assert t3 is not None
    assert t3.status == "completed"


def test_find_latest_task_for_source(tmp_storage: StorageConfig) -> None:
    """Verify finding the most recent resumable task for an OAI source."""
    db_url = tmp_storage.database_url
    # Create an interrupted task for melinda
    t_melinda = ProgressTracker(
        db_path=db_url,
        task_id="oai-ingest-melinda-11111",
        task_type="oai_pmh_harvest",
    )
    t_melinda.start_task(stage="harvest_and_index", metadata={"source": "melinda"})
    # Mark it interrupted
    with t_melinda.engine.begin() as conn:
        from sqlalchemy import update

        from reconciliator.database import task_progress_table

        conn.execute(
            update(task_progress_table)
            .where(task_progress_table.c.task_id == "oai-ingest-melinda-11111")
            .values(status="interrupted")
        )

    # Create another source task
    t_finto = ProgressTracker(
        db_path=db_url,
        task_id="oai-ingest-finto-22222",
        task_type="oai_pmh_harvest",
    )
    t_finto.start_task(stage="harvest_and_index", metadata={"source": "finto"})

    found = find_latest_task_for_source(db_url, source_name="melinda")
    assert found is not None
    assert found.task_id == "oai-ingest-melinda-11111"
    assert found.status == "interrupted"

    not_found = find_latest_task_for_source(db_url, source_name="nonexistent")
    assert not_found is None


def test_adopt_task_preserves_cumulative_progress(tmp_storage: StorageConfig) -> None:
    """Verify that adopting an existing task ID preserves cumulative items and original start time."""
    db_url = tmp_storage.database_url
    t1 = ProgressTracker(
        db_path=db_url,
        task_id="oai-ingest-melinda-33333",
        task_type="oai_pmh_harvest",
    )
    t1.start_task(stage="harvest_and_index")
    t1.update_progress(items_processed=5000, force=True)
    orig = get_task_progress(db_url, "oai-ingest-melinda-33333")
    assert orig is not None
    orig_start = orig.start_time
    assert orig.items_processed == 5000

    # Simulate process termination and later resumption adopting the same task_id
    t2 = ProgressTracker(
        db_path=db_url,
        task_id="oai-ingest-melinda-33333",
        task_type="oai_pmh_harvest",
    )
    # start_task should detect existing row and preserve cumulative progress
    t2.start_task(stage="harvest_and_index", items_processed=5000)
    current = get_task_progress(db_url, "oai-ingest-melinda-33333")
    assert current is not None
    assert current.status == "running"
    assert current.items_processed == 5000
    assert current.start_time == orig_start

    # New items ingested in the session
    t2.update_progress(items_processed=7500, force=True)
    res = get_task_progress(db_url, "oai-ingest-melinda-33333")
    assert res is not None
    assert res.items_processed == 7500


def test_prune_tasks_includes_interrupted(tmp_storage: StorageConfig) -> None:
    """Verify prune_tasks with status='terminated' prunes interrupted tasks."""
    db_url = tmp_storage.database_url
    tracker = ProgressTracker(
        db_path=db_url, task_id="task-interrupted-prune", task_type="oai_pmh_harvest"
    )
    tracker.start_task()
    with tracker.engine.begin() as conn:
        from sqlalchemy import update

        from reconciliator.database import task_progress_table

        conn.execute(
            update(task_progress_table)
            .where(task_progress_table.c.task_id == "task-interrupted-prune")
            .values(status="interrupted")
        )

    deleted = prune_tasks(db_url, task_id="task-interrupted-prune", status="terminated")
    assert deleted == 1
    assert get_task_progress(db_url, "task-interrupted-prune") is None


def test_cli_ingest_oai_task_id_and_adoption(tmp_storage: StorageConfig, tmp_path: Path) -> None:
    """Verify --task-id, --adopt-previous-task-id, and --no-adopt-previous-task-id CLI flags."""
    runner = CliRunner()
    cfg_file = tmp_path / "test_oai_cli.yaml"
    cfg_file.write_text(
        f"""
storage:
  data_dir: "{tmp_storage.data_dir}"
  database_url: "{tmp_storage.database_url}"

ingestion:
  oai:
    test_src:
      enabled: true
      source_type: "oai"
      oai_url: "https://test.example.org/oai"
      workers: 1
      adopt_previous_task_id: true
"""
    )

    async def fake_pipeline(*args: Any, **kwargs: Any) -> int:
        tracker = kwargs.get("tracker")
        if tracker:
            tracker.start_task(
                stage="harvest_and_index", metadata={"source": kwargs.get("source_name")}
            )
        return 10

    with patch(
        "reconciliator.commands.ingest.ingest_oai_pipeline", side_effect=fake_pipeline
    ) as mock_pipe:
        # 1. Explicit --task-id
        res = runner.invoke(
            app,
            [
                "ingest-oai",
                "--config",
                str(cfg_file),
                "--source",
                "test_src",
                "--task-id",
                "my-custom-task-42",
            ],
        )
        assert res.exit_code == 0
        call_kwargs = mock_pipe.call_args.kwargs
        tracker = call_kwargs["tracker"]
        assert tracker.task_id == "my-custom-task-42"

        # 2. Resuming with adopt_previous_task_id:
        # "my-custom-task-42" exists in DB as running/interrupted
        mock_pipe.reset_mock()
        res = runner.invoke(
            app,
            ["ingest-oai", "--config", str(cfg_file), "--source", "test_src"],
        )
        assert res.exit_code == 0
        call_kwargs = mock_pipe.call_args.kwargs
        tracker = call_kwargs["tracker"]
        assert tracker.task_id == "my-custom-task-42"
        assert "Adopting previous task ID 'my-custom-task-42'" in res.output

        # 3. Disabling adoption via --no-adopt-previous-task-id
        mock_pipe.reset_mock()
        res = runner.invoke(
            app,
            [
                "ingest-oai",
                "--config",
                str(cfg_file),
                "--source",
                "test_src",
                "--no-adopt-previous-task-id",
            ],
        )
        assert res.exit_code == 0
        call_kwargs = mock_pipe.call_args.kwargs
        tracker = call_kwargs["tracker"]
        assert tracker.task_id != "my-custom-task-42"
        assert tracker.task_id.startswith("oai-ingest-test_src-")
