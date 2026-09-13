"""IPC Client and lifecycle coordinator for Reconciliator Vector Daemon."""

from __future__ import annotations

import fcntl
import logging
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Optional

import numpy as np

from reconciliator.config import VectorDaemonConfig, VectorEngineConfig
from reconciliator.indexer.base import VectorIndex
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
    decode_add_resp,
    decode_error,
    decode_ids_resp,
    decode_json,
    decode_search_resp,
    encode_add_vectors,
    encode_json,
    encode_remove_ids,
    encode_search,
    get_default_lock_path,
    get_default_socket_path,
    recv_frame,
    send_frame,
)

logger = logging.getLogger("reconciliator.indexer.ipc.client")


class DaemonLifecycleManager:
    """Manages the lifecycle of the singleton vector daemon process."""

    def __init__(
        self,
        socket_path: Path | str,
        lock_path: Optional[Path | str] = None,
        config_path: Optional[Path | str] = None,
        auto_start: bool = True,
    ) -> None:
        self.socket_path = Path(socket_path)
        self.lock_path = Path(lock_path) if lock_path else get_default_lock_path(self.socket_path.parent)
        if config_path:
            self.config_path = Path(config_path)
        elif Path("config.yaml").exists():
            self.config_path = Path("config.yaml")
        elif Path("config.example.yaml").exists():
            self.config_path = Path("config.example.yaml")
        else:
            self.config_path = None
        self.auto_start = auto_start

    def is_running(self) -> bool:
        """Check if daemon is reachable and responding to PING."""
        if not self.socket_path.exists():
            return False
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.settimeout(1.0)
                sock.connect(str(self.socket_path))
                send_frame(sock, OP_PING)
                op, _, _ = recv_frame(sock)
                return op == OP_PONG
        except Exception:
            return False

    def ensure_running(self, timeout: float = 15.0) -> None:
        """Ensure daemon is running, launching it via lockfile coordination if absent."""
        if self.is_running():
            return

        if not self.auto_start:
            raise ConnectionRefusedError(
                f"Vector daemon is not running on {self.socket_path} and auto_start is False."
            )

        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a") as lock_f:
            fcntl.flock(lock_f.fileno(), fcntl.LOCK_EX)
            try:
                # Double check after acquiring lock
                if self.is_running():
                    return

                self.socket_path.unlink(missing_ok=True)

                cmd = [
                    sys.executable,
                    "-m",
                    "reconciliator.indexer.ipc.daemon",
                    "--socket-path",
                    str(self.socket_path),
                ]
                if self.config_path and self.config_path.exists():
                    cmd.extend(["--config", str(self.config_path)])

                logger.info("Auto-starting Vector Daemon: %s", " ".join(cmd))
                subprocess.Popen(
                    cmd,
                    start_new_session=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )

                start = time.monotonic()
                while time.monotonic() - start < timeout:
                    if self.is_running():
                        logger.info("Vector Daemon successfully started and responsive.")
                        return
                    time.sleep(0.05)

                raise TimeoutError(
                    f"Vector Daemon failed to respond on {self.socket_path} within {timeout}s."
                )
            finally:
                fcntl.flock(lock_f.fileno(), fcntl.LOCK_UN)

    def reset(self) -> bool:
        """Send reset signal to daemon to purge all in-memory indices without saving."""
        if not self.socket_path.exists():
            return False
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.settimeout(3.0)
                sock.connect(str(self.socket_path))
                send_frame(sock, OP_RESET)
                op, _, _ = recv_frame(sock)
                return op == OP_RESET_RESP
        except Exception:
            return False

    def shutdown(self, save: bool = True) -> bool:
        """Send shutdown signal to daemon."""
        if not self.socket_path.exists():
            return False
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.settimeout(3.0)
                sock.connect(str(self.socket_path))
                send_frame(sock, OP_SHUTDOWN, encode_json({"save": save}))
                op, _, _ = recv_frame(sock)
                return op == OP_SHUTDOWN_RESP
        except Exception:
            return False

    def query_status(self) -> Optional[dict[str, Any]]:
        """Query real-time telemetry dictionary from active daemon."""
        if not self.socket_path.exists():
            return None
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.settimeout(1.0)
                sock.connect(str(self.socket_path))
                send_frame(sock, OP_GET_STATUS)
                op, _, payload = recv_frame(sock)
                if op == OP_STATUS_RESP:
                    return decode_json(payload)
                return None
        except Exception:
            return None


class IPCVectorIndexClient:
    """Client implementing VectorIndex protocol communicating over Unix Domain Sockets."""

    def __init__(
        self,
        index_path: Path | str,
        dimension: int,
        config: Optional[VectorEngineConfig] = None,
        socket_path: Optional[Path | str] = None,
        auto_start: bool = True,
        existing_socket: Optional[socket.socket] = None,
        **kwargs: Any,
    ) -> None:
        self.index_path = Path(index_path).resolve()
        self.meta_path = Path(f"{self.index_path}.meta.json")
        self.dimension = dimension
        self.config = config or VectorEngineConfig()
        self.daemon_cfg: VectorDaemonConfig = self.config.daemon
        config_path = kwargs.pop("config_path", None)
        self.overrides = kwargs

        sock_p = socket_path or self.daemon_cfg.socket_path
        if not sock_p:
            sock_p = get_default_socket_path(self.index_path.parent)
        self.socket_path = Path(sock_p)

        self.lifecycle = DaemonLifecycleManager(
            socket_path=self.socket_path,
            config_path=config_path,
            auto_start=self.daemon_cfg.auto_start if auto_start is None else auto_start,
        )

        self._sock: Optional[socket.socket] = existing_socket
        self._lock = threading.Lock()
        self._total_vectors_cached: Optional[int] = None
        self.metadata: dict[str, Any] = {}

        # Connect and initialize index on daemon
        self._ensure_connected()
        self._open_remote_index()

    def _ensure_connected(self) -> socket.socket:
        """Ensure connection to daemon is active, re-establishing if needed."""
        if self._sock is not None:
            return self._sock

        self.lifecycle.ensure_running()
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(60.0)  # Generous timeout for large batch operations
        sock.connect(str(self.socket_path))
        self._sock = sock
        return sock

    def _close(self) -> None:
        """Close socket connection."""
        if self._sock is not None:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None

    def _rpc(self, opcode: int, payload: bytes = b"") -> tuple[int, bytes]:
        """Execute synchronized RPC frame request/response with auto-reconnect."""
        with self._lock:
            for attempt in range(2):
                try:
                    sock = self._ensure_connected()
                    send_frame(sock, opcode, payload)
                    resp_op, _, resp_payload = recv_frame(sock)
                    if resp_op == OP_ERROR:
                        msg, exc_type = decode_error(resp_payload)
                        if exc_type == "ValueError":
                            raise ValueError(msg)
                        elif exc_type == "FileNotFoundError":
                            raise FileNotFoundError(msg)
                        raise RuntimeError(f"Daemon error: {msg}")
                    return resp_op, resp_payload
                except (EOFError, ConnectionResetError, BrokenPipeError):
                    self._close()
                    if attempt == 1:
                        raise
                    # Reconnect and retry
                    time.sleep(0.1)

            raise ConnectionError("Failed to communicate with Vector Daemon after retry.")

    def _open_remote_index(self) -> None:
        """Instruct daemon to open or register this index."""
        overrides = dict(self.overrides)
        if "backend" not in overrides and self.config.backend:
            overrides["backend"] = self.config.backend
        if "device" not in overrides and self.config.device:
            overrides["device"] = self.config.device
        if "threads" not in overrides and self.config.threads is not None:
            overrides["threads"] = self.config.threads

        if self.config.usearch is not None:
            for k, v in self.config.usearch.model_dump().items():
                if k not in overrides:
                    overrides[k] = v
        if self.config.torch is not None:
            for k, v in self.config.torch.model_dump().items():
                if k not in overrides:
                    overrides[k] = v
        if self.config.faiss is not None:
            for k, v in self.config.faiss.model_dump().items():
                if k not in overrides:
                    overrides[k] = v

        req = {
            "index_path": str(self.index_path),
            "dimension": self.dimension,
            "overrides": overrides,
        }
        op, payload = self._rpc(OP_OPEN_INDEX, encode_json(req))
        if op == OP_OPEN_RESP:
            resp = decode_json(payload)
            self.dimension = resp.get("dimension", self.dimension)
            self._total_vectors_cached = resp.get("total_vectors", 0)
            self.metadata = resp.get("metadata", {})

    @property
    def total_vectors(self) -> int:
        """Return current number of vectors in index."""
        req = {"index_path": str(self.index_path)}
        op, payload = self._rpc(OP_TOTAL_VECTORS, encode_json(req))
        if op == OP_TOTAL_RESP:
            res = decode_json(payload)
            self._total_vectors_cached = res.get("total", 0)
            return self._total_vectors_cached
        return self._total_vectors_cached or 0

    @property
    def ids(self) -> np.ndarray:
        """Return active slice of IDs for inspection / backward compatibility."""
        req = {"index_path": str(self.index_path)}
        op, payload = self._rpc(OP_GET_IDS, encode_json(req))
        if op == OP_IDS_RESP:
            return decode_ids_resp(payload)
        return np.empty((0,), dtype=np.int64)

    def add_vectors(
        self,
        vectors: np.ndarray,
        ids: Optional[list[int] | np.ndarray] = None,
    ) -> list[int]:
        """Add batch of vectors with optional IDs to remote index."""
        payload = encode_add_vectors(str(self.index_path), vectors, ids=ids)
        op, resp_payload = self._rpc(OP_ADD_VECTORS, payload)
        if op == OP_ADD_RESP:
            assigned = decode_add_resp(resp_payload)
            if self._total_vectors_cached is not None:
                self._total_vectors_cached += len(assigned)
            return assigned
        return []

    def remove_ids(self, ids: list[int] | np.ndarray) -> int:
        """Remove vectors with given IDs from remote index."""
        payload = encode_remove_ids(str(self.index_path), ids)
        op, resp_payload = self._rpc(OP_REMOVE_IDS, payload)
        if op == OP_REMOVE_RESP:
            res = decode_json(resp_payload)
            rem = res.get("removed_count", 0)
            if self._total_vectors_cached is not None:
                self._total_vectors_cached = max(0, self._total_vectors_cached - rem)
            return rem
        return 0

    def search(
        self,
        query_vectors: np.ndarray,
        top_k: int = 50,
    ) -> tuple[np.ndarray, np.ndarray]:
        """Search top-k nearest neighbors for query vectors."""
        payload = encode_search(str(self.index_path), query_vectors, top_k=top_k)
        op, resp_payload = self._rpc(OP_SEARCH, payload)
        if op == OP_SEARCH_RESP:
            return decode_search_resp(resp_payload)
        return np.empty((0, 0), dtype=np.float32), np.empty((0, 0), dtype=np.int64)

    def save(self, metadata: Optional[dict[str, Any]] = None) -> None:
        """Persist index binary and metadata JSON to disk via daemon."""
        req = {"index_path": str(self.index_path), "metadata": metadata}
        self._rpc(OP_SAVE, encode_json(req))

    def load(self) -> None:
        """Instruct daemon to reload index from disk."""
        req = {"index_path": str(self.index_path)}
        self._rpc(OP_LOAD, encode_json(req))

    def verify_metadata(
        self,
        expected_model: str,
        expected_dimension: int,
        expected_template_hash: Optional[str] = None,
        force_template: bool = False,
    ) -> None:
        """Verify that remote index matches expected configuration."""
        req = {
            "index_path": str(self.index_path),
            "expected_model": expected_model,
            "expected_dimension": expected_dimension,
            "expected_template_hash": expected_template_hash,
            "force_template": force_template,
        }
        self._rpc(OP_VERIFY_METADATA, encode_json(req))

    def close(self) -> None:
        """Close client connection."""
        self._close()

    def __del__(self) -> None:
        self._close()
