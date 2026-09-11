"""Factory functions for instantiating vector indexing engines."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Optional

from reconciliator.device import resolve_device
from reconciliator.indexer.base import VectorIndex
from reconciliator.indexer.faiss_engine import FAISSIndexEngine
from reconciliator.indexer.torch_engine import TorchVectorEngine

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
    backend = kwargs.get("backend")
    device = kwargs.get("device")
    threads = kwargs.get("threads")

    if config is not None:
        backend = backend or config.backend
        device = device or config.device
        threads = threads or config.threads

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
            )
        else:
            logger.info("Auto-selecting FAISSIndexEngine on CPU")
            return FAISSIndexEngine(
                index_path=index_path,
                dimension=dimension,
                threads=threads,
            )

    if backend == "torch":
        return TorchVectorEngine(
            index_path=index_path,
            dimension=dimension,
            device=target_device,
        )

    if backend == "faiss":
        return FAISSIndexEngine(
            index_path=index_path,
            dimension=dimension,
            threads=threads,
        )

    raise ValueError(f"Unknown vector engine backend: '{backend}'. Expected 'auto', 'faiss', or 'torch'.")
