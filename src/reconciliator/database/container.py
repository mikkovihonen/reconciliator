"""Podman lifecycle management for local containerized PostgreSQL instance."""

from __future__ import annotations

import logging
import shutil
import subprocess
import time
from pathlib import Path
from typing import Optional

from reconciliator.config import PostgresConfig

logger = logging.getLogger(__name__)


def get_podman_path() -> Optional[str]:
    """Find podman executable path."""
    return shutil.which("podman")


def is_container_running(name: Optional[str] = None) -> bool:
    """Check if the postgres container is currently running."""
    cfg = PostgresConfig()
    target_name = name or cfg.container_name
    podman = get_podman_path()
    if not podman:
        return False
    try:
        res = subprocess.run(
            [podman, "ps", "--filter", f"name={target_name}", "--format", "{{.Names}}"],
            capture_output=True,
            text=True,
            check=False,
        )
        return target_name in res.stdout.strip().splitlines()
    except Exception as e:
        logger.debug("Failed to inspect container %s: %s", target_name, e)
        return False


def is_container_present(name: Optional[str] = None) -> bool:
    """Check if the postgres container exists (running or stopped)."""
    cfg = PostgresConfig()
    target_name = name or cfg.container_name
    podman = get_podman_path()
    if not podman:
        return False
    try:
        res = subprocess.run(
            [podman, "ps", "-a", "--filter", f"name={target_name}", "--format", "{{.Names}}"],
            capture_output=True,
            text=True,
            check=False,
        )
        return target_name in res.stdout.strip().splitlines()
    except Exception as e:
        logger.debug("Failed to inspect container existence %s: %s", target_name, e)
        return False


def wait_for_postgres(
    host: Optional[str] = None,
    port: Optional[int] = None,
    database: Optional[str] = None,
    user: Optional[str] = None,
    password: Optional[str] = None,
    timeout_seconds: Optional[float] = None,
) -> bool:
    """Poll postgres connection until ready or timeout."""
    import psycopg

    cfg = PostgresConfig()
    effective_host = host or cfg.host
    effective_port = port or cfg.port
    effective_database = database or cfg.database
    effective_user = user or cfg.user
    effective_password = password or cfg.password
    effective_timeout = (
        timeout_seconds if timeout_seconds is not None else cfg.container_timeout_seconds
    )

    start = time.time()
    conn_str = (
        f"host={effective_host} port={effective_port} dbname={effective_database} "
        f"user={effective_user} password={effective_password} connect_timeout=1"
    )

    while time.time() - start < effective_timeout:
        try:
            with psycopg.connect(conn_str) as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1;")
                    return True
        except Exception:
            time.sleep(0.5)

    return False


def start_postgres_container(
    host: Optional[str] = None,
    port: Optional[int] = None,
    database: Optional[str] = None,
    user: Optional[str] = None,
    password: Optional[str] = None,
    name: Optional[str] = None,
    image: Optional[str] = None,
    wait_ready: bool = True,
    timeout_seconds: Optional[float] = None,
    locale: Optional[str] = None,
    lc_collate: Optional[str] = None,
    lc_ctype: Optional[str] = None,
    initdb_args: Optional[str] = None,
) -> bool:
    """Start local containerized PostgreSQL instance using Podman.

    Parameters default to values in the configuration surface (PostgresConfig).
    """
    cfg = PostgresConfig()
    effective_host = host or cfg.host
    effective_port = port or cfg.port
    effective_database = database or cfg.database
    effective_user = user or cfg.user
    effective_password = password or cfg.password
    effective_name = name or cfg.container_name
    effective_image = image or cfg.container_image
    effective_timeout = (
        timeout_seconds if timeout_seconds is not None else cfg.container_timeout_seconds
    )
    effective_locale = locale or cfg.locale
    effective_lc_collate = lc_collate or cfg.lc_collate
    effective_lc_ctype = lc_ctype or cfg.lc_ctype
    effective_initdb_args = initdb_args or cfg.initdb_args

    podman = get_podman_path()
    if not podman:
        raise RuntimeError(
            "Podman executable not found on PATH. Please install podman or set RECONCILIATOR_DATABASE_URL to an external database."
        )

    # If already running and responsive, we are done
    if is_container_running(effective_name):
        logger.info("PostgreSQL container %s is already running.", effective_name)
        if wait_ready:
            return wait_for_postgres(
                host=effective_host,
                port=effective_port,
                database=effective_database,
                user=effective_user,
                password=effective_password,
                timeout_seconds=effective_timeout,
            )
        return True

    # If stopped container exists, restart it
    if is_container_present(effective_name):
        logger.info("Restarting stopped PostgreSQL container %s...", effective_name)
        res = subprocess.run(
            [podman, "start", effective_name], capture_output=True, text=True, check=False
        )
        if res.returncode != 0:
            logger.error("Failed to start existing container %s: %s", effective_name, res.stderr)
            raise RuntimeError(f"Failed to start container {effective_name}: {res.stderr}")
    else:
        # Create and launch new container
        logger.info("Launching new PostgreSQL container %s on port %d...", effective_name, effective_port)
        containers_dir = (
            Path(__file__).resolve().parent.parent.parent.parent / "containers" / "postgres"
        )
        init_sql = containers_dir / "init.sql"
        conf_file = containers_dir / "postgresql.conf"

        cmd = [
            podman,
            "run",
            "-d",
            "--name",
            effective_name,
            "--shm-size=1g",
            "-p",
            f"{effective_port}:5432",
            "-e",
            f"POSTGRES_DB={effective_database}",
            "-e",
            f"POSTGRES_USER={effective_user}",
            "-e",
            f"POSTGRES_PASSWORD={effective_password}",
        ]

        if effective_locale:
            cmd.extend(["-e", f"LANG={effective_locale}"])
        if effective_lc_collate:
            cmd.extend(["-e", f"LC_COLLATE={effective_lc_collate}"])
        if effective_lc_ctype:
            cmd.extend(["-e", f"LC_CTYPE={effective_lc_ctype}"])

        # Construct initdb arguments if needed
        actual_initdb_args = effective_initdb_args
        if actual_initdb_args is None:
            parts = []
            if effective_locale:
                parts.append(f"--locale={effective_locale}")
            if effective_lc_collate:
                parts.append(f"--lc-collate={effective_lc_collate}")
            if effective_lc_ctype:
                parts.append(f"--lc-ctype={effective_lc_ctype}")
            if parts:
                actual_initdb_args = " ".join(parts)

        if actual_initdb_args:
            cmd.extend(["-e", f"POSTGRES_INITDB_ARGS={actual_initdb_args}"])

        if init_sql.exists():
            cmd.extend(["-v", f"{init_sql}:/docker-entrypoint-initdb.d/init.sql:ro"])
        if conf_file.exists():
            cmd.extend(
                [
                    "-v",
                    f"{conf_file}:/etc/postgresql/postgresql.conf:ro",
                    effective_image,
                    "postgres",
                    "-c",
                    "config_file=/etc/postgresql/postgresql.conf",
                ]
            )
        else:
            cmd.append(effective_image)

        res = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if res.returncode != 0:
            logger.error("Failed to launch PostgreSQL container: %s", res.stderr)
            raise RuntimeError(f"Podman run failed for container {effective_name}: {res.stderr}")

    if wait_ready:
        logger.info(
            "Waiting for PostgreSQL on %s:%d to become ready...", effective_host, effective_port
        )
        ready = wait_for_postgres(
            host=effective_host,
            port=effective_port,
            database=effective_database,
            user=effective_user,
            password=effective_password,
            timeout_seconds=effective_timeout,
        )
        if not ready:
            raise TimeoutError(f"Timed out waiting for PostgreSQL to be ready on port {effective_port}")
        logger.info(
            "PostgreSQL container %s is ready and accepting connections.", effective_name
        )

    return True


def stop_postgres_container(name: Optional[str] = None) -> bool:
    """Stop the running PostgreSQL container."""
    cfg = PostgresConfig()
    target_name = name or cfg.container_name
    podman = get_podman_path()
    if not podman or not is_container_running(target_name):
        return False
    res = subprocess.run([podman, "stop", target_name], capture_output=True, text=True, check=False)
    return res.returncode == 0


def remove_postgres_container(name: Optional[str] = None) -> bool:
    """Stop and remove the PostgreSQL container."""
    cfg = PostgresConfig()
    target_name = name or cfg.container_name
    podman = get_podman_path()
    if not podman:
        return False
    if is_container_running(target_name):
        stop_postgres_container(target_name)
    res = subprocess.run([podman, "rm", target_name], capture_output=True, text=True, check=False)
    return res.returncode == 0


__all__ = [
    "get_podman_path",
    "is_container_running",
    "is_container_present",
    "wait_for_postgres",
    "start_postgres_container",
    "stop_postgres_container",
    "remove_postgres_container",
]

