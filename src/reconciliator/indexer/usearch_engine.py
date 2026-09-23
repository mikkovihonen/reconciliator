"""USearch-backed vector indexing engine with zero-copy mmap and native quantization."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal, Optional

import numpy as np
from usearch.index import Index


class USearchVectorEngine:
    """Manages dense vector index using Unum USearch (HNSW) with zero-copy mmap and quantization.

    Supports native multi-precision quantization (f32, f16, i8, b1), fast multi-threaded search
    with GIL release, native 64-bit integer IDs, and zero-RAM disk mapping via view=True.
    """

    def __init__(
        self,
        index_path: Path | str,
        dimension: int,
        metric: Literal["cos", "ip", "l2"] = "cos",
        dtype: Literal["f32", "f16", "i8", "b1"] = "f16",
        connectivity: int = 16,
        expansion_add: int = 128,
        expansion_search: int = 64,
        mmap: bool = False,
    ) -> None:
        self.index_path = Path(index_path)
        self.meta_path = Path(f"{index_path}.meta.json")
        self.dimension = dimension
        self.metric_str = (metric or "cos").lower()
        self.dtype_str = (dtype or "f16").lower()
        self.connectivity = connectivity
        self.expansion_add = expansion_add
        self.expansion_search = expansion_search
        self.mmap = mmap

        self._is_view: bool = False
        self._next_id: int = 0

        self.index_path.parent.mkdir(parents=True, exist_ok=True)
        self.metadata: dict[str, Any] = {
            "backend": "usearch",
            "dimension": dimension,
            "metric": self.metric_str,
            "dtype": self.dtype_str,
            "connectivity": self.connectivity,
            "expansion_add": self.expansion_add,
            "expansion_search": self.expansion_search,
            "model_name": "",
            "template_hash": "",
            "next_id": 0,
        }

        if self.index_path.exists():
            self.load()
        else:
            self._init_empty_index()

    def _init_empty_index(self) -> None:
        """Instantiate a new empty mutable USearch index."""
        self.index: Index = Index(
            ndim=self.dimension,
            metric=self.metric_str,
            dtype=self.dtype_str,
            connectivity=self.connectivity,
            expansion_add=self.expansion_add,
            expansion_search=self.expansion_search,
        )
        self._is_view = False

    @property
    def total_vectors(self) -> int:
        """Return current number of vectors in index."""
        return len(self.index)

    @property
    def ids(self) -> np.ndarray:
        """Return array of active integer IDs in the index."""
        if len(self.index) == 0:
            return np.empty((0,), dtype=np.int64)
        return np.fromiter(self.index.keys, dtype=np.int64)

    def remove_ids(self, ids: list[int] | np.ndarray) -> int:
        """Remove vectors with the given IDs from the index."""
        if len(ids) == 0 or len(self.index) == 0:
            return 0

        if self._is_view and self.index_path.exists():
            self.load(view=False)

        arr = np.ascontiguousarray(ids, dtype=np.int64)
        if arr.ndim != 1:
            arr = arr.flatten()

        removed = 0
        for rid in arr:
            k = int(rid)
            if k in self.index:
                self.index.remove(k)
                removed += 1
        return removed

    def add_vectors(
        self, vectors: np.ndarray, ids: Optional[list[int] | np.ndarray] = None
    ) -> list[int]:
        """Add batch of vectors to index with optional explicit integer IDs.

        If explicit IDs are provided:
          - Any pre-existing vectors with these IDs are removed first (idempotent update).
          - Vectors are added with these IDs using index.add.
        If IDs are not provided:
          - Sequential IDs starting from self._next_id are assigned.
        """
        if self._is_view and self.index_path.exists():
            self.load(view=False)

        if vectors.ndim == 1:
            vectors = np.expand_dims(vectors, axis=0)

        vectors = np.ascontiguousarray(vectors, dtype=np.float32)
        if not np.all(np.isfinite(vectors)):
            import logging

            from reconciliator.logging import log_validation

            log_validation(
                logging.getLogger("reconciliator.indexer.usearch"),
                action="sanitize_vectors",
                entity="usearch_index",
                reason=f"Found non-finite values (NaN/Inf) in input vector matrix of shape {vectors.shape}; replacing with zeros",
                level="WARNING",
            )
            vectors = np.nan_to_num(vectors, nan=0.0, posinf=0.0, neginf=0.0)

        if vectors.shape[1] != self.dimension:
            raise ValueError(
                f"Vector dimension mismatch: expected {self.dimension}, got {vectors.shape[1]}"
            )

        # Normalize L2 for inner product / cosine similarity
        if self.metric_str in ("cos", "ip"):
            norms = np.linalg.norm(vectors, axis=1, keepdims=True)
            norms[norms == 0] = 1.0
            vectors = vectors / norms

        if ids is not None:
            id_arr = np.ascontiguousarray(ids, dtype=np.int64)
            if id_arr.ndim != 1:
                id_arr = id_arr.flatten()
            if len(id_arr) != len(vectors):
                raise ValueError(
                    f"Number of IDs ({len(id_arr)}) must match number of vectors ({len(vectors)})"
                )
            assigned = [int(x) for x in id_arr]
        else:
            start_id = self._next_id
            end_id = start_id + len(vectors)
            id_arr = np.arange(start_id, end_id, dtype=np.int64)
            assigned = [int(x) for x in id_arr]

        # Ensure idempotency: remove pre-existing keys before adding
        for rid in id_arr:
            k = int(rid)
            if k in self.index:
                self.index.remove(k)

        self.index.add(id_arr, vectors)

        if len(id_arr) > 0:
            self._next_id = max(self._next_id, int(np.max(id_arr)) + 1)

        return assigned

    def search(self, query_vectors: np.ndarray, top_k: int = 50) -> tuple[np.ndarray, np.ndarray]:
        """Search top-k nearest neighbors for query vectors."""
        if len(self.index) == 0:
            return (
                np.empty((len(query_vectors), 0), dtype=np.float32),
                np.empty((len(query_vectors), 0), dtype=np.int64),
            )

        if query_vectors.ndim == 1:
            query_vectors = np.expand_dims(query_vectors, axis=0)

        query_vectors = np.ascontiguousarray(query_vectors, dtype=np.float32)
        if query_vectors.shape[1] != self.dimension:
            raise ValueError(
                f"Query vector dimension mismatch: expected {self.dimension}, got {query_vectors.shape[1]}"
            )

        # Normalize query vectors for cosine similarity
        if self.metric_str in ("cos", "ip"):
            norms = np.linalg.norm(query_vectors, axis=1, keepdims=True)
            norms[norms == 0] = 1.0
            query_vectors = query_vectors / norms

        k = min(top_k, len(self.index))
        matches = self.index.search(query_vectors, count=k)

        if matches.keys.ndim == 1:
            keys = np.expand_dims(matches.keys, axis=0)
            distances = np.expand_dims(matches.distances, axis=0)
        else:
            keys = matches.keys
            distances = matches.distances

        # Convert cosine distance (1 - cos) back to cosine similarity score (cos)
        if self.metric_str in ("cos", "ip"):
            scores = 1.0 - distances
        else:
            scores = -distances

        scores_out = np.ascontiguousarray(scores[:, :k], dtype=np.float32)
        indices_out = np.ascontiguousarray(keys[:, :k], dtype=np.int64)

        return scores_out, indices_out

    def save(self, metadata: Optional[dict[str, Any]] = None) -> None:
        """Persist USearch index binary and metadata JSON to disk."""
        self.index_path.parent.mkdir(parents=True, exist_ok=True)
        self.index.save(str(self.index_path))

        if metadata:
            self.metadata.update(metadata)
        self.metadata["backend"] = "usearch"
        self.metadata["dimension"] = self.dimension
        self.metadata["metric"] = self.metric_str
        self.metadata["dtype"] = self.dtype_str
        self.metadata["total_vectors"] = len(self.index)
        self.metadata["next_id"] = self._next_id

        with self.meta_path.open("w", encoding="utf-8") as f:
            json.dump(self.metadata, f, indent=2)

    def load(self, view: Optional[bool] = None) -> None:
        """Load index from disk and restore metadata."""
        if not self.index_path.exists():
            raise FileNotFoundError(f"USearch index file not found at: {self.index_path}")

        use_view = self.mmap if view is None else view
        restored = Index.restore(str(self.index_path), view=use_view)
        if restored is None:
            raise RuntimeError(f"Failed to restore USearch index from {self.index_path}")
        self.index = restored
        self._is_view = use_view

        self.dimension = self.index.ndim

        if self.meta_path.exists():
            with self.meta_path.open("r", encoding="utf-8") as f:
                self.metadata = json.load(f)
                if "metric" in self.metadata:
                    self.metric_str = self.metadata["metric"]
                if "dtype" in self.metadata:
                    self.dtype_str = self.metadata["dtype"]
                if "next_id" in self.metadata:
                    self._next_id = int(self.metadata["next_id"])
                elif len(self.index) > 0:
                    try:
                        self._next_id = (
                            int(np.max(np.fromiter(self.index.keys, dtype=np.int64))) + 1
                        )
                    except Exception:
                        self._next_id = len(self.index)

    def verify_metadata(
        self,
        expected_model: str,
        expected_dimension: int,
        expected_template_hash: Optional[str] = None,
        force_template: bool = False,
    ) -> None:
        """Verify that index matches expected model, dimension, and template hash."""
        if self.dimension != expected_dimension:
            raise ValueError(
                f"Index vector dimension mismatch: index has {self.dimension}, expected {expected_dimension}"
            )

        stored_model = self.metadata.get("model_name")
        if stored_model and expected_model and stored_model != expected_model:
            raise ValueError(
                f"Index model mismatch: indexed with '{stored_model}', current config specifies '{expected_model}'"
            )

        stored_hash = self.metadata.get("template_hash")
        if stored_hash and expected_template_hash and stored_hash != expected_template_hash:
            if not force_template:
                raise ValueError(
                    f"Index template hash mismatch: index has '{stored_hash[:8]}...', "
                    f"current template has '{expected_template_hash[:8]}...'. "
                    f"Set embedding.force_template = true to override."
                )
