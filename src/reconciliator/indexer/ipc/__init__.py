"""IPC package for Reconciliator singleton vector index daemon."""

from reconciliator.indexer.ipc.protocol import (
    get_default_lock_path,
    get_default_socket_path,
)

__all__ = [
    "get_default_lock_path",
    "get_default_socket_path",
]
