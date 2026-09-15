"""Comprehensive unit and integration tests for Reconciliator Vector IPC Daemon and Client."""

from __future__ import annotations

import json
import socket
import threading
import time
from pathlib import Path

import numpy as np
import pytest

from reconciliator.config import AppConfig, StorageConfig, VectorEngineConfig
from reconciliator.indexer.ipc.client import DaemonLifecycleManager, IPCVectorIndexClient
from reconciliator.indexer.ipc.daemon import (
    IndexPoolManager,
    VectorDaemonRequestHandler,
    VectorDaemonServer,
)
from reconciliator.indexer.ipc.protocol import (
    OP_PING,
    decode_add_vectors,
    decode_error,
    decode_remove_ids,
    decode_search,
    decode_search_resp,
    encode_add_vectors,
    encode_error,
    encode_remove_ids,
    encode_search,
    encode_search_resp,
    get_default_lock_path,
    get_default_socket_path,
    recv_frame,
    send_frame,
)


def test_protocol_path_hashing() -> None:
    """Verify deterministic, short path hashing for Unix Domain Sockets on macOS."""
    p1 = Path("/Users/test/Workspace/reconciliator/data")
    sock_p = get_default_socket_path(p1)
    lock_p = get_default_lock_path(p1)

    assert str(sock_p).startswith("/tmp/rec_vectord_")
    assert str(sock_p).endswith(".sock")
    assert len(str(sock_p)) < 50  # Must be well under macOS 104-byte AF_UNIX limit
    assert str(lock_p).endswith(".lock")


def test_protocol_frame_serialization() -> None:
    """Test binary roundtrip encoding and decoding across all message types."""
    # Ping / Pong
    a, b = socket.socketpair()
    send_frame(a, OP_PING, b"test_payload", msg_id=42)
    op, mid, pl = recv_frame(b)
    assert op == OP_PING
    assert mid == 42
    assert pl == b"test_payload"

    # Error
    err_bytes = encode_error("Sample failure", exc_type="ValueError")
    msg, exc_type = decode_error(err_bytes)
    assert msg == "Sample failure"
    assert exc_type == "ValueError"

    # Vectors
    vecs = np.random.randn(8, 32).astype(np.float32)
    ids = list(range(200, 208))
    raw = encode_add_vectors("idx_key", vecs, ids=ids)
    key, dec_vecs, dec_ids = decode_add_vectors(raw)
    assert key == "idx_key"
    assert np.allclose(vecs, dec_vecs)
    assert dec_ids == ids

    # Search
    q = vecs[:2]
    s_raw = encode_search("idx_key", q, top_k=4)
    s_key, s_q, s_k = decode_search(s_raw)
    assert s_key == "idx_key"
    assert np.allclose(q, s_q)
    assert s_k == 4

    # Search response
    scores = np.array([[0.95, 0.85], [0.88, 0.70]], dtype=np.float32)
    indices = np.array([[200, 201], [202, 203]], dtype=np.int64)
    resp_raw = encode_search_resp(scores, indices)
    dec_scores, dec_indices = decode_search_resp(resp_raw)
    assert np.allclose(scores, dec_scores)
    assert np.array_equal(indices, dec_indices)

    # Remove IDs
    r_raw = encode_remove_ids("idx_key", [200, 205])
    r_key, r_ids = decode_remove_ids(r_raw)
    assert r_key == "idx_key"
    assert r_ids == [200, 205]

    a.close()
    b.close()


def create_in_process_daemon_client(
    tmp_path: Path,
    dim: int = 16,
) -> tuple[IPCVectorIndexClient, VectorDaemonServer, Path]:
    """Helper to spin up an in-memory client-server pair connected via socketpair."""
    server_sock, client_sock = socket.socketpair()

    app_cfg = AppConfig(storage=StorageConfig(data_dir=tmp_path))
    server = VectorDaemonServer.__new__(VectorDaemonServer)
    server.config = app_cfg
    server.pool = IndexPoolManager(config=app_cfg.storage.vector_engine)
    server.start_time = time.monotonic()
    server.last_request_time = time.monotonic()
    server.idle_timeout_seconds = None
    server.auto_save_interval = 60
    server.heartbeat_interval = 2.0
    server.active_clients = 0
    server.clients_lock = threading.Lock()
    server._is_shutting_down = threading.Event()
    server.socket_path = tmp_path / "test.sock"

    def _srv():
        VectorDaemonRequestHandler(server_sock, ("", 0), server)

    th = threading.Thread(target=_srv, daemon=True)
    th.start()

    idx_path = tmp_path / "test_managed.index"
    client = IPCVectorIndexClient(
        index_path=idx_path,
        dimension=dim,
        existing_socket=client_sock,
        auto_start=False,
    )
    return client, server, idx_path


def test_ipc_client_vector_operations(tmp_path: Path) -> None:
    """Test end-to-end vector lifecycle: add, search, remove, save, reload."""
    dim = 16
    client, server, idx_path = create_in_process_daemon_client(tmp_path, dim=dim)

    assert client.total_vectors == 0
    assert len(client.ids) == 0

    # Add 20 vectors
    vecs = np.random.randn(20, dim).astype(np.float32)
    ids = list(range(100, 120))
    assigned = client.add_vectors(vecs, ids=ids)
    assert assigned == ids
    assert client.total_vectors == 20
    assert len(client.ids) == 20

    # Search top-3
    q = vecs[:2]
    scores, indices = client.search(q, top_k=3)
    assert scores.shape == (2, 3)
    assert indices.shape == (2, 3)
    assert indices[0, 0] == 100
    assert indices[1, 0] == 101

    # Remove vectors
    removed = client.remove_ids([100, 105])
    assert removed == 2
    assert client.total_vectors == 18

    # Save to disk
    client.save(metadata={"test_run": "verified"})
    assert idx_path.exists()
    assert Path(f"{idx_path}.meta.json").exists()

    # Query status from server
    telemetry = server.get_telemetry_dict()
    assert telemetry["status"] == "running"
    assert len(telemetry["indices"]) == 1
    assert telemetry["indices"][0]["total_vectors"] == 18

    client.close()


def test_ipc_concurrent_writes_and_searches(tmp_path: Path) -> None:
    """Verify that multiple concurrent threads writing to the same index do not corrupt vectors."""
    dim = 16
    client, server, idx_path = create_in_process_daemon_client(tmp_path, dim=dim)

    num_threads = 4
    vectors_per_thread = 25
    errors: list[Exception] = []

    def worker(worker_id: int) -> None:
        try:
            start_id = worker_id * 1000
            vecs = np.random.randn(vectors_per_thread, dim).astype(np.float32)
            ids = list(range(start_id, start_id + vectors_per_thread))
            assigned = client.add_vectors(vecs, ids=ids)
            assert len(assigned) == vectors_per_thread

            # Concurrent search
            scores, indices = client.search(vecs[:1], top_k=5)
            assert scores.shape == (1, 5)
        except Exception as ex:
            errors.append(ex)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(num_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"Errors during concurrent writes: {errors}"
    assert client.total_vectors == num_threads * vectors_per_thread

    # Synchronously save
    client.save()
    assert idx_path.exists()

    client.close()


def test_daemon_lifecycle_manager_offline_query(tmp_path: Path) -> None:
    """Verify DaemonLifecycleManager reports inactive when daemon socket is absent."""
    non_existent = tmp_path / "absent.sock"
    mgr = DaemonLifecycleManager(non_existent, auto_start=False)
    assert not mgr.is_running()
    assert mgr.query_status() is None
    assert not mgr.shutdown()
    assert not mgr.reset()


def test_daemon_reset_and_shutdown_without_save(tmp_path: Path) -> None:
    """Verify OP_RESET drops in-memory dirty indices without saving to disk."""
    dim = 16
    client, server, idx_path = create_in_process_daemon_client(tmp_path, dim=dim)

    # Add 10 vectors making the index dirty
    vecs = np.random.randn(10, dim).astype(np.float32)
    client.add_vectors(vecs, ids=list(range(10)))
    assert client.total_vectors == 10
    assert not idx_path.exists()
    assert len(server.pool.indices) == 1

    # Call reset_all directly on pool and verify index is dropped and NOT saved
    server.pool.reset_all()
    assert len(server.pool.indices) == 0
    assert not idx_path.exists()

    # Re-add vectors to server
    client2, server2, idx_path2 = create_in_process_daemon_client(tmp_path, dim=dim)
    client2.add_vectors(vecs, ids=list(range(10)))
    assert len(server2.pool.indices) == 1

    # Shutdown server with save=False
    server2.shutdown_gracefully(save=False)
    assert len(server2.pool.indices) == 0
    assert not idx_path2.exists()

    client.close()
    client2.close()


def test_ipc_usearch_backend_selection(tmp_path: Path) -> None:
    """Verify that IPC client requesting backend='usearch' is properly initialized with USearch."""
    dim = 8
    server_sock, client_sock = socket.socketpair()

    app_cfg = AppConfig(
        storage=StorageConfig(
            data_dir=tmp_path,
            vector_engine=VectorEngineConfig(backend="auto"),
        )
    )
    server = VectorDaemonServer.__new__(VectorDaemonServer)
    server.config = app_cfg
    server.pool = IndexPoolManager(config=app_cfg.storage.vector_engine)
    server.start_time = time.monotonic()
    server.last_request_time = time.monotonic()
    server.idle_timeout_seconds = None
    server.auto_save_interval = 60
    server.heartbeat_interval = 2.0
    server.active_clients = 0
    server.clients_lock = threading.Lock()
    server._is_shutting_down = threading.Event()
    server.socket_path = tmp_path / "test_usearch.sock"

    def _srv():
        VectorDaemonRequestHandler(server_sock, ("", 0), server)

    server_thread = threading.Thread(
        target=_srv,
        daemon=True,
    )
    server_thread.start()

    idx_path = tmp_path / "usearch_test.index"
    usearch_cfg = VectorEngineConfig(backend="usearch")
    client = IPCVectorIndexClient(
        index_path=idx_path,
        dimension=dim,
        config=usearch_cfg,
        existing_socket=client_sock,
    )

    vecs = np.random.randn(5, dim).astype(np.float32)
    client.add_vectors(vecs, ids=[1, 2, 3, 4, 5])
    assert client.total_vectors == 5

    telemetry = server.get_telemetry_dict()
    assert len(telemetry["indices"]) == 1
    assert telemetry["indices"][0]["backend"] == "usearch"

    client.save()
    assert idx_path.exists()
    client.close()
    server.shutdown_gracefully(save=False)


def test_ipc_incompatible_backend_rejected(tmp_path: Path) -> None:
    """Verify that opening an index created with one backend using a different backend raises ValueError."""
    dim = 8
    idx_path = tmp_path / "torch_index.index"
    idx_path.write_bytes(b"dummy")
    meta_path = tmp_path / "torch_index.index.meta.json"
    meta_path.write_text(json.dumps({"backend": "torch", "dimension": dim}))

    server_sock, client_sock = socket.socketpair()
    app_cfg = AppConfig(storage=StorageConfig(data_dir=tmp_path))
    server = VectorDaemonServer.__new__(VectorDaemonServer)
    server.config = app_cfg
    server.pool = IndexPoolManager(config=app_cfg.storage.vector_engine)
    server.start_time = time.monotonic()
    server.last_request_time = time.monotonic()
    server.idle_timeout_seconds = None
    server.auto_save_interval = 60
    server.heartbeat_interval = 2.0
    server.active_clients = 0
    server.clients_lock = threading.Lock()
    server._is_shutting_down = threading.Event()
    server.socket_path = tmp_path / "test_reject.sock"

    def _srv():
        VectorDaemonRequestHandler(server_sock, ("", 0), server)

    server_thread = threading.Thread(target=_srv, daemon=True)
    server_thread.start()

    usearch_cfg = VectorEngineConfig(backend="usearch")
    with pytest.raises(ValueError, match="was serialized with backend 'torch'"):
        IPCVectorIndexClient(
            index_path=idx_path,
            dimension=dim,
            config=usearch_cfg,
            existing_socket=client_sock,
        )

    server.shutdown_gracefully(save=False)




