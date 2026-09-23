"""Compute configuration model."""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel


class ComputeConfig(BaseModel):
    """Hardware acceleration and compute parallelism configuration."""

    device: Literal["auto", "mps", "cuda", "cpu"] = "auto"
    threads: Optional[int] = None
