"""Singleton Vector Index IPC Daemon implementation for Reconciliator."""

from __future__ import annotations

import argparse
import atexit
import json
import logging
import os
import resource
import signal
import socketserver
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Optional

import numpy as np

from reconciliator.config import AppConfig, VectorEngineConfig, load_config
from reconciliator.device import resolve_device
from reconciliator.indexer.base import VectorIndex
from reconciliator.indexer.faiss_engine import FAISSIndexEngine
from reconciliator.indexer.ipc.protocol import (
    OP_ADD_RESP,
    OP_ADD_VECTORS,
    OP_ERROR,
    OP_GET_IDS,
    OP_GET_METADATA,
    OP_GET_STATUS,
    OP_IDS_RESP,
    OP_LOAD,
    OP_LOAD_RESP,
    OP_METADATA_RESP,
    OP_OPEN_INDEX,
    OP_OPEN_RESP,
    OP_PING,
    OP_PONG,
    OP_REMOVE_IDS,
    OP_REMOVE_RESP,
    OP_RESET,
    OP_RESET_RESP,
    OP_SAVE,
    OP_SAVE_RESP,
    OP_SEARCH,
    OP_SEARCH_RESP,
    OP_SHUTDOWN,
    OP_SHUTDOWN_RESP,
    OP_STATUS_RESP,
    OP_TOTAL_RESP,
    OP_TOTAL_VECTORS,
    OP_VERIFY_METADATA,
    OP_VERIFY_RESP,
    decode_add_vectors,
    decode_json,
    decode_remove_ids,
    decode_search,
    encode_add_resp,
    encode_error,
    encode_ids_resp,
    encode_json,
    encode_search_resp,
    get_default_socket_path,
    recv_frame,
    send_frame,
)
from reconciliator.indexer.torch_engine import TorchVectorEngine
from reconciliator.indexer.usearch_engine import USearchVectorEngine

logger = logging.getLogger("reconciliator.indexer.ipc.daemon")


def _get_process_rss_mb() -> float:
    """Return process RSS in Megabytes."""
    try:
        usage = resource.getrusage(resource.RUSAGE_SELF)
        # On macOS ru_maxrss is in bytes; on Linux in kilobytes
        if sys.platform == "darwin":
            return usage.ru_maxrss / (1024 * 1024)
        return usage.ru_maxrss / 1024
    except Exception:
        return 0.0


class ManagedIndex:
    """Encapsulates an in-memory vector index, its synchronization lock, and usage metrics."""

    def __init__(self, key: str, engine: VectorIndex) -> None:
        self.key = key
        self.engine = engine
        self.lock = threading.RLock()
        self.searches_total: int = 0
        self.adds_total: int = 0
        self.removes_total: int = 0
        self.is_dirty: bool = False
        self.last_saved: str = datetime.now(timezone.utc).isoformat()
        self.last_activity: float = time.monotonic()


class IndexPoolManager:
    """Manages the pool of active vector indices, synchronizing concurrent operations."""

    def __init__(self, config: Optional[VectorEngineConfig] = None) -> None:
        self.config = config or VectorEngineConfig()
        self.indices: dict[str, ManagedIndex] = {}
        self.pool_lock = threading.Lock()
        self.device_str = resolve_device(self.config.device)

    def get_or_open_index(
        self,
        index_path: Path | str,
        dimension: int,
        overrides: Optional[dict[str, Any]] = None,
    ) -> ManagedIndex:
        """Retrieve existing managed index or instantiate a new one."""
        resolved_key = str(Path(index_path).resolve())
        with self.pool_lock:
            if resolved_key in self.indices:
                managed = self.indices[resolved_key]
                managed.last_activity = time.monotonic()
                return managed

            logger.info("Opening vector index: %s (dim=%d)", resolved_key, dimension)
            # Check if index metadata already exists on disk
            meta_p = Path(f"{resolved_key}.meta.json")
            stored_backend = None
            if meta_p.exists() and Path(resolved_key).exists():
                try:
                    with meta_p.open("r", encoding="utf-8") as f:
                        stored_backend = json.load(f).get("backend")
                except Exception:
                    pass

            backend = self.config.backend
            if overrides and "backend" in overrides:
                backend = overrides["backend"]

            if backend != "auto" and stored_backend and stored_backend != backend:
                raise ValueError(
                    f"Vector index at '{resolved_key}' was serialized with backend '{stored_backend}', "
                    f"but current configuration specifies backend '{backend}'. "
                    f"Either update your configuration to match, or delete/reset the index before switching backends."
                )

            # Auto backend selection
            if backend == "auto":
                if stored_backend:
                    backend = stored_backend
                elif self.device_str in ("mps", "cuda"):
                    backend = "torch"
                else:
                    backend = "faiss"

            torch_kwargs: dict[str, Any] = {
                "dtype": self.config.torch.dtype,
                "mmap": self.config.torch.mmap,
                "chunk_size": self.config.torch.chunk_size,
                "initial_capacity": self.config.torch.initial_capacity,
            }
            faiss_kwargs: dict[str, Any] = {
                "index_type": self.config.faiss.index_type,
                "mmap": self.config.faiss.mmap,
                "nprobe": self.config.faiss.nprobe,
                "nlist": self.config.faiss.nlist,
            }
            usearch_kwargs: dict[str, Any] = {
                "metric": self.config.usearch.metric,
                "dtype": self.config.usearch.dtype,
                "connectivity": self.config.usearch.connectivity,
                "expansion_add": self.config.usearch.expansion_add,
                "expansion_search": self.config.usearch.expansion_search,
                "mmap": self.config.usearch.mmap,
            }
            if overrides:
                for k, v in overrides.items():
                    if k in torch_kwargs:
                        torch_kwargs[k] = v
                    if k in faiss_kwargs:
                        faiss_kwargs[k] = v
                    if k in usearch_kwargs:
                        usearch_kwargs[k] = v

            engine: VectorIndex
            if backend == "torch":
                target_dev: Literal["auto", "mps", "cuda", "cpu"] = (
                    "mps" if self.device_str == "mps" else ("cuda" if self.device_str == "cuda" else "cpu")
                )
                engine = TorchVectorEngine(
                    index_path=resolved_key,
                    dimension=dimension,
                    device=target_dev,
                    **torch_kwargs,
                )
            elif backend == "usearch":
                engine = USearchVectorEngine(
                    index_path=resolved_key,
                    dimension=dimension,
                    **usearch_kwargs,
                )
            else:
                engine = FAISSIndexEngine(
                    index_path=resolved_key,
                    dimension=dimension,
                    threads=self.config.threads,
                    **faiss_kwargs,
                )

            managed = ManagedIndex(resolved_key, engine)
            self.indices[resolved_key] = managed
            return managed

    def get_index(self, index_path: Path | str) -> Optional[ManagedIndex]:
        """Lookup an already opened managed index."""
        resolved_key = str(Path(index_path).resolve())
        with self.pool_lock:
            return self.indices.get(resolved_key)

    def save_dirty(self) -> int:
        """Commit all dirty indices to disk. Returns count of saved indices."""
        saved_count = 0
        with self.pool_lock:
            all_managed = list(self.indices.values())

        for managed in all_managed:
            with managed.lock:
                if managed.is_dirty:
                    try:
                        managed.engine.save()
                        managed.is_dirty = False
                        managed.last_saved = datetime.now(timezone.utc).isoformat()
                        saved_count += 1
                        logger.info("Saved dirty index to disk: %s", managed.key)
                    except Exception as e:
                        logger.error("Failed to auto-save index %s: %s", managed.key, e)
        return saved_count

    def save_all(self) -> None:
        """Synchronously commit all managed indices to disk."""
        with self.pool_lock:
            all_managed = list(self.indices.values())

        for managed in all_managed:
            with managed.lock:
                try:
                    managed.engine.save()
                    managed.is_dirty = False
                    managed.last_saved = datetime.now(timezone.utc).isoformat()
                    logger.info("Synchronously saved index: %s", managed.key)
                except Exception as e:
                    logger.error("Failed to save index %s: %s", managed.key, e)

    def reset_all(self) -> None:
        """Drop all loaded indices without saving, clearing in-memory caches."""
        with self.pool_lock:
            for managed in self.indices.values():
                with managed.lock:
                    managed.is_dirty = False
                    if hasattr(managed.engine, "close"):
                        try:
                            managed.engine.close()
                        except Exception:
                            pass
            self.indices.clear()
            logger.info("Reset all indices in daemon pool.")


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

            # Update progress.db heartbeat if progress_db exists
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
        """Emit telemetry status record to progress.db if initialized."""
        try:
            progress_db = self.config.storage.progress_db
            if not progress_db.exists():
                return

            import sqlite3
            now_iso = datetime.now(timezone.utc).isoformat()
            status_data = self.get_telemetry_dict()
            with sqlite3.connect(str(progress_db), timeout=5.0) as conn:
                conn.execute(
                    """
                    INSERT INTO task_progress (
                        task_id, task_type, status, current_stage,
                        items_processed, items_total, rate_per_sec,
                        start_time, last_heartbeat, metadata_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(task_id) DO UPDATE SET
                        status = excluded.status,
                        current_stage = excluded.current_stage,
                        items_processed = excluded.items_processed,
                        items_total = excluded.items_total,
                        rate_per_sec = excluded.rate_per_sec,
                        last_heartbeat = excluded.last_heartbeat,
                        metadata_json = excluded.metadata_json;
                    """,
                    (
                        "system_vector_daemon",
                        "system",
                        "running",
                        f"{len(self.pool.indices)} active index(es)",
                        sum(m.engine.total_vectors for m in self.pool.indices.values()),
                        None,
                        sum(m.searches_total for m in self.pool.indices.values()) / max(1.0, now_iso and (time.monotonic() - self.start_time)),
                        now_iso,
                        now_iso,
                        json_dumps(status_data),
                    ),
                )
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
            "host_ram_mb": round(_get_process_rss_mb(), 1),
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

        # Shutdown in a separate thread so server doesn't deadlock calling itself
        def _do_shutdown() -> None:
            try:
                self.shutdown()
            except Exception:
                pass

        threading.Thread(target=_do_shutdown, daemon=True).start()


def json_dumps(obj: Any) -> str:
    import json
    return json.dumps(obj, ensure_ascii=False)


class VectorDaemonRequestHandler(socketserver.BaseRequestHandler):
    """Handles an active client socket connection dispatching framed IPC requests."""

    def handle(self) -> None:
        server: VectorDaemonServer = self.server  # type: ignore
        with server.clients_lock:
            server.active_clients += 1

        try:
            while not server._is_shutting_down.is_set():
                try:
                    opcode, msg_id, payload = recv_frame(self.request)
                except EOFError:
                    break  # Client disconnected cleanly
                except Exception as e:
                    logger.debug("Socket read error: %s", e)
                    break

                server.record_activity()

                try:
                    self._dispatch(server, opcode, msg_id, payload)
                except Exception as e:
                    logger.exception("Error processing opcode %d: %s", opcode, e)
                    send_frame(
                        self.request,
                        OP_ERROR,
                        encode_error(str(e), exc_type=type(e).__name__),
                        msg_id=msg_id,
                    )
        finally:
            with server.clients_lock:
                server.active_clients = max(0, server.active_clients - 1)

    def _dispatch(
        self,
        server: VectorDaemonServer,
        opcode: int,
        msg_id: int,
        payload: bytes,
    ) -> None:
        sock = self.request

        if opcode == OP_PING:
            send_frame(sock, OP_PONG, b"", msg_id=msg_id)
            return

        if opcode == OP_GET_STATUS:
            telemetry = server.get_telemetry_dict()
            send_frame(sock, OP_STATUS_RESP, encode_json(telemetry), msg_id=msg_id)
            return

        if opcode == OP_OPEN_INDEX:
            req = decode_json(payload)
            path = req["index_path"]
            dim = req["dimension"]
            overrides = req.get("overrides")
            managed = server.pool.get_or_open_index(path, dim, overrides=overrides)
            resp = {
                "key": managed.key,
                "dimension": managed.engine.dimension,
                "total_vectors": managed.engine.total_vectors,
                "metadata": getattr(managed.engine, "metadata", {}),
            }
            send_frame(sock, OP_OPEN_RESP, encode_json(resp), msg_id=msg_id)
            return

        if opcode == OP_TOTAL_VECTORS:
            req = decode_json(payload)
            managed = server.pool.get_index(req["index_path"])
            total = managed.engine.total_vectors if managed else 0
            send_frame(sock, OP_TOTAL_RESP, encode_json({"total": total}), msg_id=msg_id)
            return

        if opcode == OP_GET_METADATA:
            req = decode_json(payload)
            managed = server.pool.get_index(req["index_path"])
            meta = getattr(managed.engine, "metadata", {}) if managed else {}
            send_frame(sock, OP_METADATA_RESP, encode_json(meta), msg_id=msg_id)
            return

        if opcode == OP_GET_IDS:
            req = decode_json(payload)
            managed = server.pool.get_index(req["index_path"])
            if managed is not None:
                with managed.lock:
                    ids_val = getattr(managed.engine, "ids", None)
                    if ids_val is not None and hasattr(ids_val, "cpu"):
                        id_arr = ids_val.cpu().numpy().astype(np.int64)
                    elif ids_val is not None:
                        id_arr = np.ascontiguousarray(ids_val, dtype=np.int64)
                    elif hasattr(managed.engine, "_id_to_idx"):
                        id_arr = np.array(list(managed.engine._id_to_idx.keys()), dtype=np.int64)
                    else:
                        id_arr = np.empty((0,), dtype=np.int64)
            else:
                id_arr = np.empty((0,), dtype=np.int64)
            send_frame(sock, OP_IDS_RESP, encode_ids_resp(id_arr), msg_id=msg_id)
            return

        if opcode == OP_ADD_VECTORS:
            key, vectors, ids = decode_add_vectors(payload)
            managed = server.pool.get_index(key)
            if managed is None:
                raise KeyError(f"Index not opened on daemon: {key}")
            with managed.lock:
                assigned = managed.engine.add_vectors(vectors, ids=ids)
                managed.adds_total += len(vectors)
                managed.is_dirty = True
                managed.last_activity = time.monotonic()
            send_frame(sock, OP_ADD_RESP, encode_add_resp(assigned), msg_id=msg_id)
            return

        if opcode == OP_REMOVE_IDS:
            key, ids = decode_remove_ids(payload)
            managed = server.pool.get_index(key)
            if managed is None:
                raise KeyError(f"Index not opened on daemon: {key}")
            with managed.lock:
                removed_count = managed.engine.remove_ids(ids)
                managed.removes_total += removed_count
                managed.is_dirty = True
                managed.last_activity = time.monotonic()
            send_frame(
                sock,
                OP_REMOVE_RESP,
                encode_json({"removed_count": removed_count}),
                msg_id=msg_id,
            )
            return

        if opcode == OP_SEARCH:
            key, query_vectors, top_k = decode_search(payload)
            managed = server.pool.get_index(key)
            if managed is None:
                raise KeyError(f"Index not opened on daemon: {key}")
            with managed.lock:
                scores, indices = managed.engine.search(query_vectors, top_k=top_k)
                managed.searches_total += len(query_vectors)
                managed.last_activity = time.monotonic()
            send_frame(sock, OP_SEARCH_RESP, encode_search_resp(scores, indices), msg_id=msg_id)
            return

        if opcode == OP_SAVE:
            req = decode_json(payload)
            managed = server.pool.get_index(req["index_path"])
            if managed is not None:
                with managed.lock:
                    managed.engine.save(metadata=req.get("metadata"))
                    managed.is_dirty = False
                    managed.last_saved = datetime.now(timezone.utc).isoformat()
            send_frame(sock, OP_SAVE_RESP, encode_json({"status": "ok"}), msg_id=msg_id)
            return

        if opcode == OP_LOAD:
            req = decode_json(payload)
            managed = server.pool.get_index(req["index_path"])
            if managed is not None:
                with managed.lock:
                    managed.engine.load()
                    managed.is_dirty = False
            send_frame(sock, OP_LOAD_RESP, encode_json({"status": "ok"}), msg_id=msg_id)
            return

        if opcode == OP_VERIFY_METADATA:
            req = decode_json(payload)
            managed = server.pool.get_index(req["index_path"])
            if managed is not None:
                with managed.lock:
                    managed.engine.verify_metadata(
                        expected_model=req["expected_model"],
                        expected_dimension=req["expected_dimension"],
                        expected_template_hash=req.get("expected_template_hash"),
                        force_template=req.get("force_template", False),
                    )
            send_frame(sock, OP_VERIFY_RESP, encode_json({"status": "ok"}), msg_id=msg_id)
            return

        if opcode == OP_RESET:
            server.pool.reset_all()
            send_frame(sock, OP_RESET_RESP, encode_json({"status": "ok"}), msg_id=msg_id)
            server.record_activity()
            return

        if opcode == OP_SHUTDOWN:
            save_indices = True
            if payload:
                try:
                    req = decode_json(payload)
                    save_indices = bool(req.get("save", True))
                except Exception:
                    pass
            send_frame(sock, OP_SHUTDOWN_RESP, encode_json({"status": "shutting_down"}), msg_id=msg_id)
            server.shutdown_gracefully(save=save_indices)
            return

        raise ValueError(f"Unsupported opcode: {opcode}")


def run_daemon(
    socket_path: Path | str,
    config_path: Optional[Path | str] = None,
) -> None:
    """Run the standalone vector daemon process."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    cfg_p: Optional[Path] = Path(config_path) if config_path else None
    if cfg_p is None or not cfg_p.exists():
        if Path("config.yaml").exists():
            cfg_p = Path("config.yaml")
        elif Path("config.example.yaml").exists():
            cfg_p = Path("config.example.yaml")

    cfg = load_config(cfg_p) if cfg_p and cfg_p.exists() else AppConfig()
    sock_p = Path(socket_path)

    logger.info("Initializing Reconciliator Vector Daemon on socket: %s", sock_p)
    server = VectorDaemonServer(sock_p, config=cfg)

    def _sig_handler(sig: int, frame: Any) -> None:
        logger.info("Received signal %d; shutting down daemon.", sig)
        server.shutdown_gracefully()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _sig_handler)
    signal.signal(signal.SIGINT, _sig_handler)
    atexit.register(server.shutdown_gracefully)

    try:
        server.serve_forever()
    finally:
        server.server_close()
        sock_p.unlink(missing_ok=True)
        logger.info("Vector Daemon terminated cleanly.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Reconciliator Singleton Vector Index Daemon")
    parser.add_argument(
        "--socket-path",
        type=str,
        default=None,
        help="Path to the Unix domain socket",
    )
    parser.add_argument(
        "--config",
        type=str,
        default=None,
        help="Path to configuration YAML file",
    )
    parser.add_argument(
        "--idle-timeout",
        type=int,
        default=None,
        help="Idle shutdown timeout in seconds override",
    )
    args = parser.parse_args()

    cfg_arg = args.config
    if cfg_arg is None:
        if Path("config.yaml").exists():
            cfg_arg = "config.yaml"
        elif Path("config.example.yaml").exists():
            cfg_arg = "config.example.yaml"

    sock = Path(args.socket_path) if args.socket_path else get_default_socket_path(Path.cwd())
    run_daemon(sock, config_path=cfg_arg)


if __name__ == "__main__":
    main()

