"""Request handler for framed IPC operations on the Vector Daemon."""

from __future__ import annotations

import logging
import socketserver
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import numpy as np

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
    recv_frame,
    send_frame,
)

if TYPE_CHECKING:
    from reconciliator.indexer.ipc.server import VectorDaemonServer

logger = logging.getLogger("reconciliator.indexer.ipc.handler")


class VectorDaemonRequestHandler(socketserver.BaseRequestHandler):
    """Handles an active client socket connection dispatching framed IPC requests."""

    def handle(self) -> None:
        server: VectorDaemonServer = self.server  # type: ignore[assignment]
        with server.clients_lock:
            server.active_clients += 1

        try:
            while not server._is_shutting_down.is_set():
                try:
                    opcode, msg_id, payload = recv_frame(self.request)
                except EOFError:
                    break
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
            send_frame(
                sock, OP_SHUTDOWN_RESP, encode_json({"status": "shutting_down"}), msg_id=msg_id
            )
            server.shutdown_gracefully(save=save_indices)
            return

        raise ValueError(f"Unsupported opcode: {opcode}")
