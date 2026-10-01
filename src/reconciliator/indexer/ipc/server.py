"""Unix domain socket server hosting the Vector Daemon service."""

from __future__ import annotations

import json
import logging
import os
import socketserver
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from reconciliator.config import AppConfig
from reconciliator.indexer.ipc.handler import VectorDaemonRequestHandler
from reconciliator.indexer.ipc.pool import IndexPoolManager, get_process_rss_mb

logger = logging.getLogger("reconciliator.indexer.ipc.server")


def json_dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False)


class VectorDaemonServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    """Threaded Unix domain stream socket server hosting VectorDaemon services."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        socket_path: Path | str,
        config: Optional[AppConfig] = None,
    ) -> None:
        self.socket_path = Path(socket_path)
        self.config = config or AppConfig()
        v_cfg = self.config.storage.vector_engine
        self.pool = IndexPoolManager(config=v_cfg)
        self.start_time: float = time.monotonic()
        self.last_request_time: float = time.monotonic()
        self.idle_timeout_seconds: Optional[int] = v_cfg.daemon.idle_timeout_seconds
        self.auto_save_interval: int = v_cfg.daemon.auto_save_interval_seconds
        self.heartbeat_interval: float = v_cfg.daemon.heartbeat_interval_seconds
        self.active_clients: int = 0
        self.clients_lock = threading.Lock()
        self._is_shutting_down = threading.Event()

        # Clean existing stale socket if not running
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        if self.socket_path.exists():
            self.socket_path.unlink()

        super().__init__(str(self.socket_path), VectorDaemonRequestHandler)

        # Background maintenance threads
        self._flusher_thread = threading.Thread(
            target=self._auto_save_loop, daemon=True, name="vector-daemon-flusher"
        )
        self._watchdog_thread = threading.Thread(
            target=self._watchdog_loop, daemon=True, name="vector-daemon-watchdog"
        )
        self._flusher_thread.start()
        self._watchdog_thread.start()

    def record_activity(self) -> None:
        """Mark daemon as active, resetting idle watchdog timer."""
        self.last_request_time = time.monotonic()

    def _auto_save_loop(self) -> None:
        """Background loop to flush dirty indices at configured intervals."""
        while not self._is_shutting_down.is_set():
            time.sleep(max(1.0, float(self.auto_save_interval)))
            if self._is_shutting_down.is_set():
                break
            self.pool.save_dirty()

    def _watchdog_loop(self) -> None:
        """Background loop checking idle timeout and updating telemetry."""
        while not self._is_shutting_down.is_set():
            time.sleep(max(1.0, self.heartbeat_interval))
            if self._is_shutting_down.is_set():
                break

            now = time.monotonic()
            idle_duration = now - self.last_request_time

            self._write_progress_heartbeat(idle_duration)

            if (
                self.idle_timeout_seconds is not None
                and self.idle_timeout_seconds > 0
                and idle_duration >= self.idle_timeout_seconds
            ):
                logger.info(
                    "Idle timeout reached (%.1fs >= %ds). Initiating graceful shutdown.",
                    idle_duration,
                    self.idle_timeout_seconds,
                )
                self.shutdown_gracefully()
                break

    def _write_progress_heartbeat(self, idle_duration: float) -> None:
        """Emit telemetry status record to task progress table if initialized."""
        try:
            target = self.config.storage.database_url
            if not target:
                return
            from sqlalchemy.dialects.postgresql import insert as pg_insert

            from reconciliator.database import get_engine, task_progress_table

            now_iso = datetime.now(timezone.utc).isoformat()
            status_data = self.get_telemetry_dict()
            engine = get_engine(target)
            try:
                with engine.begin() as conn:
                    stmt = pg_insert(task_progress_table).values(
                        task_id="system_vector_daemon",
                        task_type="system",
                        status="running",
                        current_stage=f"{len(self.pool.indices)} active index(es)",
                        items_processed=sum(
                            m.engine.total_vectors for m in self.pool.indices.values()
                        ),
                        items_total=None,
                        rate_per_sec=sum(m.searches_total for m in self.pool.indices.values())
                        / max(1.0, time.monotonic() - self.start_time),
                        start_time=now_iso,
                        last_heartbeat=now_iso,
                        metadata_json=json_dumps(status_data),
                    )
                    stmt = stmt.on_conflict_do_update(
                        index_elements=["task_id"],
                        set_={
                            "status": stmt.excluded.status,
                            "current_stage": stmt.excluded.current_stage,
                            "items_processed": stmt.excluded.items_processed,
                            "items_total": stmt.excluded.items_total,
                            "rate_per_sec": stmt.excluded.rate_per_sec,
                            "last_heartbeat": stmt.excluded.last_heartbeat,
                            "metadata_json": stmt.excluded.metadata_json,
                        },
                    )
                    conn.execute(stmt)
            finally:
                engine.dispose()
        except Exception:
            pass

    def get_telemetry_dict(self) -> dict[str, Any]:
        """Compile detailed real-time daemon metrics dictionary."""
        now = time.monotonic()
        indices_info: list[dict[str, Any]] = []

        with self.pool.pool_lock:
            for key, m in self.pool.indices.items():
                with m.lock:
                    indices_info.append(
                        {
                            "key": Path(key).name,
                            "path": key,
                            "dimension": m.engine.dimension,
                            "total_vectors": m.engine.total_vectors,
                            "backend": getattr(m.engine, "metadata", {}).get("backend", "unknown"),
                            "dtype": getattr(m.engine, "dtype_str", "float32"),
                            "searches_total": m.searches_total,
                            "adds_total": m.adds_total,
                            "removes_total": m.removes_total,
                            "is_dirty": m.is_dirty,
                            "last_saved": m.last_saved,
                        }
                    )

        return {
            "pid": os.getpid(),
            "status": "running",
            "uptime_seconds": round(now - self.start_time, 1),
            "idle_seconds": round(now - self.last_request_time, 1),
            "idle_timeout_seconds": self.idle_timeout_seconds,
            "host_ram_mb": round(get_process_rss_mb(), 1),
            "device": self.pool.device_str,
            "active_clients": self.active_clients,
            "socket_path": str(self.socket_path),
            "indices": indices_info,
        }

    def shutdown_gracefully(self, save: bool = True) -> None:
        """Flush all indices and shut down the socket server."""
        if self._is_shutting_down.is_set():
            return
        self._is_shutting_down.set()
        if save:
            logger.info("Graceful shutdown requested: saving all active indices...")
            self.pool.save_all()
        else:
            logger.info("Shutdown requested without saving: resetting all indices...")
            self.pool.reset_all()

        try:
            if self.socket_path.exists():
                self.socket_path.unlink()
        except Exception:
            pass

        def _do_shutdown() -> None:
            try:
                self.shutdown()
            except Exception:
                pass

        threading.Thread(target=_do_shutdown, daemon=True).start()
