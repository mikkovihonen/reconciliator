"""FAISS-backed vector indexing engine."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Optional

import faiss
import numpy as np


class FAISSIndexEngine:
    """Manages dense FAISS vector index (IndexFlatIP) with L2 normalization and metadata persistence."""

    def __init__(
        self,
        index_path: Path | str,
        dimension: int,
        threads: Optional[int] = None,
    ) -> None:
        self.index_path = Path(index_path)
        self.meta_path = Path(f"{index_path}.meta.json")
        self.dimension = dimension

        if threads is not None:
            faiss.omp_set_num_threads(threads)
        elif sys.platform == "darwin":
            # Avoid OpenMP thread collision with PyTorch on macOS by default
            try:
                faiss.omp_set_num_threads(1)
            except Exception:
                pass

        self.index_path.parent.mkdir(parents=True, exist_ok=True)
        self.metadata: dict[str, Any] = {
            "backend": "faiss",
            "dimension": dimension,
            "model_name": "",
            "template_hash": "",
        }

        self._base_index: Optional[faiss.Index] = None
        if self.index_path.exists():
            self.load()
        else:
            self._base_index = faiss.IndexFlatIP(dimension)
            self.index: faiss.Index = faiss.IndexIDMap2(self._base_index)

    @property
    def total_vectors(self) -> int:
        """Return current number of vectors in index."""
        return self.index.ntotal

    def remove_ids(self, ids: list[int] | np.ndarray) -> int:
        """Remove vectors with the given IDs from the index."""
        if len(ids) == 0 or self.index.ntotal == 0:
            return 0
        arr = np.ascontiguousarray(ids, dtype=np.int64)
        if arr.ndim != 1:
            arr = arr.flatten()
        return int(self.index.remove_ids(faiss.IDSelectorBatch(arr)))

    def add_vectors(
        self, vectors: np.ndarray, ids: Optional[list[int] | np.ndarray] = None
    ) -> list[int]:
        """Add batch of vectors to index with optional explicit integer IDs.

        If explicit IDs are provided:
          - Any pre-existing vectors with these IDs are removed first (idempotent update).
          - Vectors are added with these IDs using add_with_ids.
        If IDs are not provided:
          - Sequential IDs starting from self.index.ntotal are assigned.
        """
        if vectors.ndim == 1:
            vectors = np.expand_dims(vectors, axis=0)

        vectors = np.ascontiguousarray(vectors, dtype=np.float32)
        if vectors.shape[1] != self.dimension:
            raise ValueError(
                f"Vector dimension mismatch: expected {self.dimension}, got {vectors.shape[1]}"
            )

        # L2-normalize vectors for cosine similarity via Inner Product
        faiss.normalize_L2(vectors)

        if ids is not None:
            id_arr = np.ascontiguousarray(ids, dtype=np.int64)
            if id_arr.ndim != 1:
                id_arr = id_arr.flatten()
            if len(id_arr) != len(vectors):
                raise ValueError(
                    f"Number of IDs ({len(id_arr)}) must match number of vectors ({len(vectors)})"
                )
            self.remove_ids(id_arr)
            self.index.add_with_ids(vectors, id_arr)
            return [int(x) for x in id_arr]
        else:
            start_id = self.index.ntotal
            end_id = start_id + len(vectors)
            id_arr = np.arange(start_id, end_id, dtype=np.int64)
            self.index.add_with_ids(vectors, id_arr)
            return [int(x) for x in id_arr]

    def search(self, query_vectors: np.ndarray, top_k: int = 50) -> tuple[np.ndarray, np.ndarray]:
        """Search top-k nearest neighbors for query vectors."""
        if self.index.ntotal == 0:
            return np.empty((len(query_vectors), 0), dtype=np.float32), np.empty(
                (len(query_vectors), 0), dtype=np.int64
            )

        if query_vectors.ndim == 1:
            query_vectors = np.expand_dims(query_vectors, axis=0)

        query_vectors = np.ascontiguousarray(query_vectors, dtype=np.float32)
        if query_vectors.shape[1] != self.dimension:
            raise ValueError(
                f"Query vector dimension mismatch: expected {self.dimension}, got {query_vectors.shape[1]}"
            )

        faiss.normalize_L2(query_vectors)
        k = min(top_k, self.index.ntotal)
        scores, indices = self.index.search(query_vectors, k)
        return scores, indices

    def save(self, metadata: Optional[dict[str, Any]] = None) -> None:
        """Persist FAISS index binary and metadata JSON to disk."""
        self.index_path.parent.mkdir(parents=True, exist_ok=True)
        faiss.write_index(self.index, str(self.index_path))

        if metadata:
            self.metadata.update(metadata)
        self.metadata["backend"] = "faiss"
        self.metadata["dimension"] = self.dimension
        self.metadata["total_vectors"] = self.index.ntotal

        with self.meta_path.open("w", encoding="utf-8") as f:
            json.dump(self.metadata, f, indent=2)

    def load(self) -> None:
        """Load index from disk and restore metadata."""
        if not self.index_path.exists():
            raise FileNotFoundError(f"FAISS index file not found at: {self.index_path}")

        self._base_index = None
        loaded_index = faiss.read_index(str(self.index_path))
        if not isinstance(loaded_index, (faiss.IndexIDMap, faiss.IndexIDMap2)):
            self._base_index = loaded_index
            loaded_index = faiss.IndexIDMap2(self._base_index)
        self.index = loaded_index
        self.dimension = loaded_index.d

        if self.meta_path.exists():
            with self.meta_path.open("r", encoding="utf-8") as f:
                self.metadata = json.load(f)

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

