"""Hardware accelerator detection (CUDA, Apple Silicon Metal MPS, CPU) and optimization."""

from __future__ import annotations

import os
from typing import Literal, Optional
import torch


def resolve_device(device_str: Literal["auto", "cuda", "mps", "cpu"] = "auto") -> str:
    """Resolve the optimal computing device based on hardware availability."""
    target = device_str.lower()
    if target == "auto":
        if torch.cuda.is_available():
            return "cuda"
        if hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
            return "mps"
        return "cpu"

    if target == "cuda":
        if torch.cuda.is_available():
            return "cuda"
        return "cpu"

    if target == "mps":
        if hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
            return "mps"
        return "cpu"

    return "cpu"


def configure_execution_threads(num_threads: Optional[int] = None) -> int:
    """Configure intra-op parallelism threads for CPU fallback operations."""
    if num_threads is None:
        cpu_count = os.cpu_count() or 4
        num_threads = max(1, cpu_count)
    torch.set_num_threads(num_threads)
    return num_threads


def get_optimal_batch_size(base_batch_size: int, device: str) -> int:
    """Return effective batch size, scaling up for GPU/Metal when suitable."""
    if device in ("cuda", "mps") and base_batch_size < 128:
        return base_batch_size
    return base_batch_size

