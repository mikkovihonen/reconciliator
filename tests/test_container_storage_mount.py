"""Tests for PostgreSQL container bind mount configuration and storage migration."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

from reconciliator.config import AppConfig, PostgresConfig
from reconciliator.database.container import (
    migrate_container_storage_to_host,
    start_postgres_container,
)


def test_postgres_config_data_dir(tmp_path: Path):
    """Verify data_dir configuration in PostgresConfig and env var override."""
    cfg = PostgresConfig(data_dir=tmp_path / "custom_pg")
    assert cfg.data_dir == tmp_path / "custom_pg"
    assert cfg.get_effective_data_dir() == (tmp_path / "custom_pg").resolve()

    # Test environment variable override
    env_target = tmp_path / "env_pg"
    with patch.dict(os.environ, {"RECONCILIATOR_POSTGRES_DATA_DIR": str(env_target)}):
        assert cfg.get_effective_data_dir() == env_target.resolve()


def test_start_postgres_container_with_bind_mount(tmp_path: Path):
    """Verify start_postgres_container generates the correct Podman -v bind mount arguments."""
    target_data_dir = tmp_path / "pgdata_host"

    with patch("reconciliator.database.container.get_podman_path", return_value="/usr/bin/podman"), \
         patch("reconciliator.database.container.is_container_running", return_value=False), \
         patch("reconciliator.database.container.is_container_present", return_value=False), \
         patch("reconciliator.database.container.wait_for_postgres", return_value=True), \
         patch("subprocess.run") as mock_run:

        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        started = start_postgres_container(
            name="test-reconciliator-pg",
            data_dir=target_data_dir,
            wait_ready=True,
        )

        assert started is True
        assert mock_run.called
        cmd_args = mock_run.call_args[0][0]

        # Verify Podman run flags
        assert "/usr/bin/podman" in cmd_args
        assert "run" in cmd_args
        assert "-v" in cmd_args
        expected_mount = f"{target_data_dir.resolve()}:/var/lib/postgresql/data:Z"
        assert expected_mount in cmd_args
        assert "PGDATA=/var/lib/postgresql/data" in " ".join(cmd_args)
        assert target_data_dir.exists()


def test_start_postgres_container_recreate_flag():
    """Verify recreate=True removes existing container before launching."""
    with patch("reconciliator.database.container.get_podman_path", return_value="/usr/bin/podman"), \
         patch("reconciliator.database.container.is_container_running", return_value=False), \
         patch("reconciliator.database.container.is_container_present", return_value=True), \
         patch("reconciliator.database.container.remove_postgres_container") as mock_remove, \
         patch("reconciliator.database.container.wait_for_postgres", return_value=True), \
         patch("subprocess.run") as mock_run:

        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        start_postgres_container(
            name="test-pg",
            recreate=True,
            wait_ready=False,
        )

        assert mock_remove.called
        mock_remove.assert_called_with("test-pg")


def test_migrate_container_storage_to_host(tmp_path: Path):
    """Verify migrate_container_storage_to_host runs podman cp and recreates container."""
    host_target = tmp_path / "migrated_pg"

    with patch("reconciliator.database.container.get_podman_path", return_value="/usr/bin/podman"), \
         patch("reconciliator.database.container.is_container_running", return_value=True), \
         patch("reconciliator.database.container.stop_postgres_container") as mock_stop, \
         patch("reconciliator.database.container.start_postgres_container") as mock_start, \
         patch("subprocess.run") as mock_run:

        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        mock_start.return_value = True

        migrated = migrate_container_storage_to_host(
            target_host_dir=host_target,
            name="test-pg",
        )

        assert migrated is True
        assert mock_stop.called
        # Verify podman cp was invoked
        calls = [call[0][0] for call in mock_run.call_args_list]
        cp_called = any("cp" in cmd and f"test-pg:/var/lib/postgresql/data/." in cmd for cmd in calls)
        assert cp_called is True
        # Verify start_postgres_container called with new target and recreate=True
        mock_start.assert_called_with(name="test-pg", data_dir=host_target.resolve(), recreate=True)

