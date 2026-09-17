"""Podman lifecycle management for local containerized PostgreSQL instance."""

from __future__ import annotations

import logging
import shutil
import subprocess
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

CONTAINER_NAME = "reconciliator-pg"
DEFAULT_IMAGE = "docker.io/library/postgres:16-alpine"


def get_podman_path() -> Optional[str]:
    """Find podman executable path."""
    return shutil.which("podman")


def is_container_running(name: str = CONTAINER_NAME) -> bool:
    """Check if the postgres container is currently running."""
    podman = get_podman_path()
    if not podman:
        return False
    try:
        res = subprocess.run(
            [podman, "ps", "--filter", f"name={name}", "--format", "{{.Names}}"],
            capture_output=True,
            text=True,
            check=False,
        )
        return name in res.stdout.strip().splitlines()
    except Exception as e:
        logger.debug("Failed to inspect container %s: %s", name, e)
        return False


def is_container_present(name: str = CONTAINER_NAME) -> bool:
    """Check if the postgres container exists (running or stopped)."""
    podman = get_podman_path()
    if not podman:
        return False
    try:
        res = subprocess.run(
            [podman, "ps", "-a", "--filter", f"name={name}", "--format", "{{.Names}}"],
            capture_output=True,
            text=True,
            check=False,
        )
        return name in res.stdout.strip().splitlines()
    except Exception as e:
        logger.debug("Failed to inspect container existence %s: %s", name, e)
        return False


def wait_for_postgres(
    host: str = "localhost",
    port: int = 5432,
    database: str = "reconciliator",
    user: str = "postgres",
    password: str = "postgres",
    timeout_seconds: float = 30.0,
) -> bool:
    """Poll postgres connection until ready or timeout."""
    import psycopg

    start = time.time()
    conn_str = f"host={host} port={port} dbname={database} user={user} password={password} connect_timeout=1"

    while time.time() - start < timeout_seconds:
        try:
            with psycopg.connect(conn_str) as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1;")
                    return True
        except Exception:
            time.sleep(0.5)

    return False


def start_postgres_container(
    host: str = "localhost",
    port: int = 5432,
    database: str = "reconciliator",
    user: str = "postgres",
    password: str = "postgres",
    name: str = CONTAINER_NAME,
    image: str = DEFAULT_IMAGE,
    wait_ready: bool = True,
    timeout_seconds: float = 30.0,
) -> bool:
    """Start local containerized PostgreSQL instance using Podman."""
    podman = get_podman_path()
    if not podman:
        raise RuntimeError(
            "Podman executable not found on PATH. Please install podman or set RECONCILIATOR_DATABASE_URL to an external database."
        )

    # If already running and responsive, we are done
    if is_container_running(name):
        logger.info("PostgreSQL container %s is already running.", name)
        if wait_ready:
            return wait_for_postgres(
                host=host,
                port=port,
                database=database,
                user=user,
                password=password,
                timeout_seconds=timeout_seconds,
            )
        return True

    # If stopped container exists, restart it
    if is_container_present(name):
        logger.info("Restarting stopped PostgreSQL container %s...", name)
        res = subprocess.run([podman, "start", name], capture_output=True, text=True, check=False)
        if res.returncode != 0:
            logger.error("Failed to start existing container %s: %s", name, res.stderr)
            raise RuntimeError(f"Failed to start container {name}: {res.stderr}")
    else:
        # Create and launch new container
        logger.info("Launching new PostgreSQL container %s on port %d...", name, port)
        containers_dir = Path(__file__).resolve().parent.parent.parent / "containers" / "postgres"
        init_sql = containers_dir / "init.sql"
        conf_file = containers_dir / "postgresql.conf"

        cmd = [
            podman,
            "run",
            "-d",
            "--name",
            name,
            "--shm-size=1g",
            "-p",
            f"{port}:5432",
            "-e",
            f"POSTGRES_DB={database}",
            "-e",
            f"POSTGRES_USER={user}",
            "-e",
            f"POSTGRES_PASSWORD={password}",
        ]

        if init_sql.exists():
            cmd.extend(["-v", f"{init_sql}:/docker-entrypoint-initdb.d/init.sql:ro"])
        if conf_file.exists():
            cmd.extend(
                [
                    "-v",
                    f"{conf_file}:/etc/postgresql/postgresql.conf:ro",
                    image,
                    "postgres",
                    "-c",
                    "config_file=/etc/postgresql/postgresql.conf",
                ]
            )
        else:
            cmd.append(image)

        res = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if res.returncode != 0:
            logger.error("Failed to launch PostgreSQL container: %s", res.stderr)
            raise RuntimeError(f"Podman run failed for container {name}: {res.stderr}")

    if wait_ready:
        logger.info("Waiting for PostgreSQL on %s:%d to become ready...", host, port)
        ready = wait_for_postgres(
            host=host,
            port=port,
            database=database,
            user=user,
            password=password,
            timeout_seconds=timeout_seconds,
        )
        if not ready:
            raise TimeoutError(f"Timed out waiting for PostgreSQL to be ready on port {port}")
        logger.info("PostgreSQL container %s is ready and accepting connections.", name)

    return True


def stop_postgres_container(name: str = CONTAINER_NAME) -> bool:
    """Stop the running PostgreSQL container."""
    podman = get_podman_path()
    if not podman or not is_container_running(name):
        return False
    res = subprocess.run([podman, "stop", name], capture_output=True, text=True, check=False)
    return res.returncode == 0


def remove_postgres_container(name: str = CONTAINER_NAME) -> bool:
    """Stop and remove the PostgreSQL container."""
    podman = get_podman_path()
    if not podman:
        return False
    if is_container_running(name):
        stop_postgres_container(name)
    res = subprocess.run([podman, "rm", name], capture_output=True, text=True, check=False)
    return res.returncode == 0
