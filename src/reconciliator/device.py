"""Hardware accelerator detection (CUDA, Apple Silicon Metal MPS, CPU) and optimization."""

from __future__ import annotations

import logging
import os
from typing import Literal, Optional

import torch

logger = logging.getLogger(__name__)


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
    """Return batch size scaled for available hardware.

    Scales up for GPU/Metal when suitable to exploit hardware parallelism.
    """
    if device == "cuda":
        return base_batch_size * 4
    elif device == "mps":
        return base_batch_size * 2
    return base_batch_size


def configure_mixed_precision(device: str) -> bool:
    """Return whether automatic mixed precision (fp16) should be used.

    Enables fp16 on CUDA devices for faster inference with minimal accuracy loss.
    """
    if device == "cuda" and torch.cuda.is_available():
        logger.info("Enabling CUDA automatic mixed precision (fp16)")
        return True
    return False
