"""In-memory index pool management and lifecycle tracking for the Vector IPC Daemon."""

from __future__ import annotations

import json
import logging
import resource
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Optional

from reconciliator.config import VectorEngineConfig
from reconciliator.indexer.base import VectorIndex
from reconciliator.indexer.faiss_engine import FAISSIndexEngine
from reconciliator.indexer.torch_engine import TorchVectorEngine
from reconciliator.indexer.usearch_engine import USearchVectorEngine
from reconciliator.utils.device import resolve_device

logger = logging.getLogger("reconciliator.indexer.ipc.pool")


def get_process_rss_mb() -> float:
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
                    "mps"
                    if self.device_str == "mps"
                    else ("cuda" if self.device_str == "cuda" else "cpu")
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
