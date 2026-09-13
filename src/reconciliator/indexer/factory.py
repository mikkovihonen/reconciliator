"""Factory functions for instantiating vector indexing engines."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Optional

from reconciliator.device import resolve_device
from reconciliator.indexer.base import VectorIndex
from reconciliator.indexer.faiss_engine import FAISSIndexEngine
from reconciliator.indexer.torch_engine import TorchVectorEngine
from reconciliator.indexer.usearch_engine import USearchVectorEngine

if TYPE_CHECKING:
    from reconciliator.config import VectorEngineConfig

logger = logging.getLogger(__name__)


def create_vector_engine(
    index_path: Path | str,
    dimension: int,
    config: Optional[VectorEngineConfig] = None,
    **kwargs: Any,
) -> VectorIndex:
    """Create and return a configured VectorIndex instance.

    Args:
        index_path: Path to the serialized index file.
        dimension: Embedding vector dimension.
        config: VectorEngineConfig specification.
        **kwargs: Overrides for backend, device, threads.
    """
    mode = kwargs.get("mode")
    if mode is None:
        mode = config.mode if config is not None else "embedded"
    mode = str(mode).lower()

    if mode == "daemon":
        from reconciliator.indexer.ipc.client import IPCVectorIndexClient

        return IPCVectorIndexClient(
            index_path=index_path,
            dimension=dimension,
            config=config,
            **kwargs,
        )

    backend = kwargs.get("backend")
    device = kwargs.get("device")
    threads = kwargs.get("threads")

    torch_kwargs: dict[str, Any] = {}
    faiss_kwargs: dict[str, Any] = {}
    usearch_kwargs: dict[str, Any] = {}

    if config is not None:
        backend = backend or config.backend
        device = device or config.device
        threads = threads or config.threads
        if getattr(config, "torch", None) is not None:
            torch_kwargs.update(
                {
                    "dtype": config.torch.dtype,
                    "mmap": config.torch.mmap,
                    "chunk_size": config.torch.chunk_size,
                    "initial_capacity": config.torch.initial_capacity,
                }
            )
        if getattr(config, "faiss", None) is not None:
            faiss_kwargs.update(
                {
                    "index_type": config.faiss.index_type,
                    "mmap": config.faiss.mmap,
                    "nprobe": config.faiss.nprobe,
                    "nlist": config.faiss.nlist,
                }
            )
        if getattr(config, "usearch", None) is not None:
            usearch_kwargs.update(
                {
                    "metric": config.usearch.metric,
                    "dtype": config.usearch.dtype,
                    "connectivity": config.usearch.connectivity,
                    "expansion_add": config.usearch.expansion_add,
                    "expansion_search": config.usearch.expansion_search,
                    "mmap": config.usearch.mmap,
                }
            )

    # Allow direct kwargs overrides
    for k in ("dtype", "mmap", "chunk_size", "initial_capacity"):
        if k in kwargs:
            torch_kwargs[k] = kwargs[k]
    for k in ("index_type", "mmap", "nprobe", "nlist"):
        if k in kwargs:
            faiss_kwargs[k] = kwargs[k]
    for k in ("metric", "dtype", "connectivity", "expansion_add", "expansion_search", "mmap"):
        if k in kwargs:
            usearch_kwargs[k] = kwargs[k]

    backend = str(backend or "auto").lower()
    norm_dev = str(device or "auto").lower()
    valid_devices = ("auto", "mps", "cuda", "cpu")
    target_device: Literal["auto", "mps", "cuda", "cpu"] = (
        norm_dev if norm_dev in valid_devices else "auto"  # type: ignore[assignment]
    )

    if backend == "auto":
        resolved_dev = resolve_device(target_device)
        # Use native Torch engine if hardware GPU acceleration (MPS or CUDA) is available
        if resolved_dev in ("mps", "cuda"):
            dev_literal: Literal["mps", "cuda"] = "mps" if resolved_dev == "mps" else "cuda"
            logger.info("Auto-selecting TorchVectorEngine on %s", dev_literal)
            return TorchVectorEngine(
                index_path=index_path,
                dimension=dimension,
                device=dev_literal,
                **torch_kwargs,
            )
        else:
            logger.info("Auto-selecting FAISSIndexEngine on CPU")
            return FAISSIndexEngine(
                index_path=index_path,
                dimension=dimension,
                threads=threads,
                **faiss_kwargs,
            )

    if backend == "torch":
        return TorchVectorEngine(
            index_path=index_path,
            dimension=dimension,
            device=target_device,
            **torch_kwargs,
        )

    if backend == "faiss":
        return FAISSIndexEngine(
            index_path=index_path,
            dimension=dimension,
            threads=threads,
            **faiss_kwargs,
        )

    if backend == "usearch":
        return USearchVectorEngine(
            index_path=index_path,
            dimension=dimension,
            **usearch_kwargs,
        )

    raise ValueError(
        f"Unknown vector engine backend: '{backend}'. Expected 'auto', 'faiss', 'torch', or 'usearch'."
    )
