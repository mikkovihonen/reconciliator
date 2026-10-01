"""IPC package for Reconciliator singleton vector index daemon."""

from reconciliator.indexer.ipc.daemon import main, run_daemon
from reconciliator.indexer.ipc.handler import VectorDaemonRequestHandler
from reconciliator.indexer.ipc.pool import (
    IndexPoolManager,
    ManagedIndex,
    get_process_rss_mb,
)
from reconciliator.indexer.ipc.protocol import (
    get_default_lock_path,
    get_default_socket_path,
)
from reconciliator.indexer.ipc.server import VectorDaemonServer

__all__ = [
    "IndexPoolManager",
    "ManagedIndex",
    "VectorDaemonRequestHandler",
    "VectorDaemonServer",
    "get_default_lock_path",
    "get_default_socket_path",
    "get_process_rss_mb",
    "main",
    "run_daemon",
]
