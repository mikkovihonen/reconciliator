"""Hardware accelerator detection (CUDA, Apple Silicon Metal MPS, CPU) and memory management."""

from __future__ import annotations

import gc
import logging
import os
from typing import Optional, Union

import torch

logger = logging.getLogger(__name__)


def is_gpu_available() -> bool:
    """Check if any hardware acceleration (CUDA or Apple Metal MPS) is available."""
    if torch.cuda.is_available():
        return True
    if hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
        return True
    return False


def resolve_device(device_str: Optional[Union[str, torch.device]] = "auto") -> str:
    """Resolve compute device string based on hardware availability with robust fallback.

    Args:
        device_str: Device name ('auto', 'cuda', 'mps', or 'cpu') or existing torch.device.

    Returns:
        Canonical device string ('cuda', 'mps', or 'cpu').
    """
    if isinstance(device_str, torch.device):
        target = device_str.type.lower()
    elif isinstance(device_str, str):
        target = (device_str or "auto").strip().lower()
    else:
        target = "auto"

    if target == "cuda":
        if torch.cuda.is_available():
            return "cuda"
        logger.warning("CUDA requested but not available; falling back to CPU.")
        return "cpu"

    if target == "mps":
        if hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
            return "mps"
        logger.warning("Metal MPS requested but not available; falling back to CPU.")
        return "cpu"

    if target == "cpu":
        return "cpu"

    # 'auto' mode: prefer CUDA, then Metal MPS, then CPU
    if torch.cuda.is_available():
        return "cuda"
    if hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
        return "mps"
    return "cpu"


def resolve_compute_device(requested: Optional[Union[str, torch.device]] = "auto") -> torch.device:
    """Resolve compute device based on configuration request with robust fallback.

    Args:
        requested: Device name ('auto', 'cuda', 'mps', or 'cpu') or existing torch.device.

    Returns:
        Configured torch.device instance.
    """
    if isinstance(requested, torch.device):
        return requested
    return torch.device(resolve_device(requested))


def clear_accelerator_memory(device: Optional[Union[str, torch.device]] = None) -> None:
    """Release cached GPU/MPS memory back to the OS.

    Args:
        device: Optional target device as torch.device or string (e.g. 'cuda', 'mps').
    """
    gc.collect()
    dev_type: Optional[str] = None
    if isinstance(device, torch.device):
        dev_type = device.type.lower()
    elif isinstance(device, str):
        dev_type = device.strip().lower()

    if dev_type == "cuda" and torch.cuda.is_available():
        try:
            torch.cuda.empty_cache()
        except Exception:
            pass
        return

    if dev_type == "mps" and hasattr(torch, "mps") and hasattr(torch.mps, "empty_cache"):
        try:
            torch.mps.empty_cache()
        except Exception:
            pass
        return

    # If no specific device given or unrecognized, clear whatever is available
    if torch.cuda.is_available():
        try:
            torch.cuda.empty_cache()
        except Exception:
            pass
    if hasattr(torch, "mps") and hasattr(torch.mps, "empty_cache"):
        try:
            torch.mps.empty_cache()
        except Exception:
            pass


def configure_execution_threads(num_threads: Optional[int] = None) -> int:
    """Configure intra-op parallelism threads for CPU fallback operations.

    When ``num_threads`` is ``None`` the thread count is taken from
    ``os.cpu_count()``. Pass an explicit value (from ``EmbeddingConfig.num_cpu_threads``)
    to override.
    """
    if num_threads is None:
        num_threads = os.cpu_count() or 1
    torch.set_num_threads(num_threads)
    return num_threads


def get_optimal_batch_size(
    base_batch_size: int,
    device: str,
    *,
    cuda_batch_scale: int = 4,
    mps_batch_scale: int = 2,
) -> int:
    """Return batch size scaled for available hardware.

    Scale factors are configurable via ``EmbeddingConfig.cuda_batch_scale`` and
    ``EmbeddingConfig.mps_batch_scale`` so they can be tuned without touching
    source code.
    """
    if device == "cuda":
        return base_batch_size * cuda_batch_scale
    elif device == "mps":
        return base_batch_size * mps_batch_scale
    return base_batch_size


def configure_mixed_precision(device: str) -> bool:
    """Return whether automatic mixed precision (fp16) should be used.

    Enables fp16 on CUDA devices for faster inference with minimal accuracy loss.
    """
    if device == "cuda" and torch.cuda.is_available():
        logger.info("Enabling CUDA automatic mixed precision (fp16)")
        return True
    return False
