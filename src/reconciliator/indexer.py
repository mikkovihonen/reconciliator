"""FAISS vector index manager for dense semantic search with metadata validation."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional
import faiss
import numpy as np


class FAISSIndexManager:
    """Manages dense FAISS vector index (IndexFlatIP) with L2 normalization and metadata persistence."""

    def __init__(self, index_path: Path | str, dimension: int) -> None:
        self.index_path = Path(index_path)
        self.meta_path = Path(f"{index_path}.meta.json")
        self.dimension = dimension

        self.index_path.parent.mkdir(parents=True, exist_ok=True)
        self.metadata: dict[str, Any] = {
            "dimension": dimension,
            "model_name": "",
            "template_hash": "",
        }

        if self.index_path.exists():
            self.load()
        else:
            self.index: faiss.IndexFlatIP = faiss.IndexFlatIP(dimension)

    @property
    def total_vectors(self) -> int:
        """Return current number of vectors in index."""
        return self.index.ntotal

    def add_vectors(self, vectors: np.ndarray) -> list[int]:
        """Add batch of vectors to index, returning assigned FAISS integer IDs."""
        if vectors.ndim == 1:
            vectors = np.expand_dims(vectors, axis=0)

        vectors = vectors.astype(np.float32)
        if vectors.shape[1] != self.dimension:
            raise ValueError(
                f"Vector dimension mismatch: expected {self.dimension}, got {vectors.shape[1]}"
            )

        # L2-normalize vectors for cosine similarity via Inner Product
        faiss.normalize_L2(vectors)

        start_id = self.index.ntotal
        self.index.add(vectors)
        return list(range(start_id, self.index.ntotal))

    def search(
        self, query_vectors: np.ndarray, top_k: int = 50
    ) -> tuple[np.ndarray, np.ndarray]:
        """Search top-k nearest neighbors for query vectors."""
        if query_vectors.ndim == 1:
            query_vectors = np.expand_dims(query_vectors, axis=0)

        query_vectors = query_vectors.astype(np.float32)
        if query_vectors.shape[1] != self.dimension:
            raise ValueError(
                f"Query vector dimension mismatch: expected {self.dimension}, got {query_vectors.shape[1]}"
            )

        faiss.normalize_L2(query_vectors)
        k = min(top_k, max(1, self.index.ntotal))
        scores, indices = self.index.search(query_vectors, k)
        return scores, indices

    def save(self, metadata: Optional[dict[str, Any]] = None) -> None:
        """Persist FAISS index binary and metadata JSON to disk."""
        self.index_path.parent.mkdir(parents=True, exist_ok=True)
        faiss.write_index(self.index, str(self.index_path))

        if metadata:
            self.metadata.update(metadata)
        self.metadata["dimension"] = self.dimension
        self.metadata["total_vectors"] = self.index.ntotal

        with self.meta_path.open("w", encoding="utf-8") as f:
            json.dump(self.metadata, f, indent=2)

    def load(self) -> None:
        """Load index from disk and restore metadata."""
        if not self.index_path.exists():
            raise FileNotFoundError(f"FAISS index file not found at: {self.index_path}")

        loaded_index = faiss.read_index(str(self.index_path))
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

