"""Abstract protocol and base definitions for vector indexing engines."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional, Protocol, runtime_checkable

import numpy as np


@runtime_checkable
class VectorIndex(Protocol):
    """Unified interface protocol for dense vector indexing engines."""

    index_path: Path
    meta_path: Path
    dimension: int
    metadata: dict[str, Any]

    @property
    def total_vectors(self) -> int:
        """Return current number of vectors in index."""
        ...

    def add_vectors(
        self,
        vectors: np.ndarray,
        ids: Optional[list[int] | np.ndarray] = None,
    ) -> list[int]:
        """Add batch of vectors with optional explicit integer IDs. Returns assigned IDs."""
        ...

    def remove_ids(self, ids: list[int] | np.ndarray) -> int:
        """Remove vectors with the given IDs from the index. Returns count of removed vectors."""
        ...

    def search(
        self,
        query_vectors: np.ndarray,
        top_k: int = 50,
    ) -> tuple[np.ndarray, np.ndarray]:
        """Search top-k nearest neighbors for query vectors.

        Returns:
            Tuple of (scores, indices) as numpy arrays.
        """
        ...

    def save(self, metadata: Optional[dict[str, Any]] = None) -> None:
        """Persist index binary and metadata JSON to disk."""
        ...

    def load(self) -> None:
        """Load index from disk and restore metadata."""
        ...

    def verify_metadata(
        self,
        expected_model: str,
        expected_dimension: int,
        expected_template_hash: Optional[str] = None,
        force_template: bool = False,
    ) -> None:
        """Verify that index matches expected model, dimension, and template hash."""
        ...
