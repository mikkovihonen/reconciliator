"""Hardware accelerator resolution and memory management for PyTorch (Metal MPS, CUDA, CPU)."""

from __future__ import annotations

import gc
import logging
from typing import Optional

import torch

logger = logging.getLogger(__name__)


def is_gpu_available() -> bool:
    """Check if any hardware acceleration (CUDA or Apple Metal MPS) is available."""
    if torch.cuda.is_available():
        return True
    if hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
        return True
    return False


def resolve_compute_device(requested: str = "auto") -> torch.device:
    """Resolve compute device based on configuration request with robust fallback.

    Args:
        requested: Device name ('auto', 'cuda', 'mps', or 'cpu').

    Returns:
        Configured torch.device instance.
    """
    req = (requested or "auto").strip().lower()

    if req == "cuda":
        if torch.cuda.is_available():
            return torch.device("cuda")
        logger.warning("CUDA requested but not available; falling back to CPU.")
        return torch.device("cpu")

    if req == "mps":
        if hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
            return torch.device("mps")
        logger.warning("Metal MPS requested but not available; falling back to CPU.")
        return torch.device("cpu")

    if req == "cpu":
        return torch.device("cpu")

    # 'auto' mode: prefer CUDA, then Metal MPS, then CPU
    if torch.cuda.is_available():
        return torch.device("cuda")
    if hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
        return torch.device("mps")
    return torch.device("cpu")


def clear_accelerator_memory(device: Optional[torch.device] = None) -> None:
    """Release cached GPU/MPS memory back to the OS."""
    gc.collect()
    if device is not None:
        if device.type == "cuda" and torch.cuda.is_available():
            torch.cuda.empty_cache()
            return
        if device.type == "mps" and hasattr(torch, "mps") and hasattr(torch.mps, "empty_cache"):
            try:
                torch.mps.empty_cache()
            except Exception:
                pass
            return

    # If no specific device given or fallback, clear whatever is available
    if torch.cuda.is_available():
        torch.cuda.empty_cache()
    if hasattr(torch, "mps") and hasattr(torch.mps, "empty_cache"):
        try:
            torch.mps.empty_cache()
        except Exception:
            pass
