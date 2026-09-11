"""Native PyTorch vector indexing engine using safetensors persistence."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal, Optional

import numpy as np
import safetensors.torch
import torch

from reconciliator.device import resolve_device


class TorchVectorEngine:
    """Manages dense vector index using PyTorch tensors with hardware acceleration (MPS/CUDA/CPU)."""

    def __init__(
        self,
        index_path: Path | str,
        dimension: int,
        device: Literal["auto", "mps", "cuda", "cpu"] = "auto",
    ) -> None:
        self.index_path = Path(index_path)
        self.meta_path = Path(f"{index_path}.meta.json")
        self.dimension = dimension
        self.device_str = resolve_device(device)
        self.device = torch.device(self.device_str)

        self.index_path.parent.mkdir(parents=True, exist_ok=True)
        self.metadata: dict[str, Any] = {
            "backend": "torch",
            "device": self.device_str,
            "dimension": dimension,
            "model_name": "",
            "template_hash": "",
        }

        self.vectors: Optional[torch.Tensor] = None
        self.ids: Optional[torch.Tensor] = None

        if self.index_path.exists():
            self.load()

    @property
    def total_vectors(self) -> int:
        """Return current number of vectors in index."""
        if self.vectors is None:
            return 0
        return int(self.vectors.shape[0])

    def remove_ids(self, ids: list[int] | np.ndarray) -> int:
        """Remove vectors with the given IDs from the index."""
        if self.total_vectors == 0 or len(ids) == 0:
            return 0

        target_ids = torch.as_tensor(ids, dtype=torch.int64, device=self.device)
        if target_ids.ndim != 1:
            target_ids = target_ids.flatten()

        assert self.ids is not None
        assert self.vectors is not None

        mask = torch.isin(self.ids, target_ids, invert=True)
        num_removed = int((~mask).sum().item())
        if num_removed > 0:
            self.vectors = self.vectors[mask]
            self.ids = self.ids[mask]
        return num_removed

    def add_vectors(
        self, vectors: np.ndarray, ids: Optional[list[int] | np.ndarray] = None
    ) -> list[int]:
        """Add batch of vectors to index with optional explicit integer IDs."""
        if vectors.ndim == 1:
            vectors = np.expand_dims(vectors, axis=0)

        if vectors.shape[1] != self.dimension:
            raise ValueError(
                f"Vector dimension mismatch: expected {self.dimension}, got {vectors.shape[1]}"
            )

        new_vecs = torch.as_tensor(vectors, dtype=torch.float32, device=self.device)
        # L2-normalize vectors for cosine similarity via inner product
        new_vecs = torch.nn.functional.normalize(new_vecs, p=2.0, dim=-1)

        if ids is not None:
            id_arr = np.ascontiguousarray(ids, dtype=np.int64)
            if id_arr.ndim != 1:
                id_arr = id_arr.flatten()
            if len(id_arr) != len(vectors):
                raise ValueError(
                    f"Number of IDs ({len(id_arr)}) must match number of vectors ({len(vectors)})"
                )
            self.remove_ids(id_arr)
            new_ids = torch.as_tensor(id_arr, dtype=torch.int64, device=self.device)
        else:
            start_id = self.total_vectors
            new_ids = torch.arange(
                start_id, start_id + len(vectors), dtype=torch.int64, device=self.device
            )

        if self.vectors is None or self.vectors.shape[0] == 0:
            self.vectors = new_vecs
            self.ids = new_ids
        else:
            assert self.ids is not None
            self.vectors = torch.cat([self.vectors, new_vecs], dim=0)
            self.ids = torch.cat([self.ids, new_ids], dim=0)

        return [int(x) for x in new_ids.cpu().tolist()]

    def search(self, query_vectors: np.ndarray, top_k: int = 50) -> tuple[np.ndarray, np.ndarray]:
        """Search top-k nearest neighbors for query vectors."""
        if self.total_vectors == 0:
            return np.empty((len(query_vectors), 0), dtype=np.float32), np.empty(
                (len(query_vectors), 0), dtype=np.int64
            )

        if query_vectors.ndim == 1:
            query_vectors = np.expand_dims(query_vectors, axis=0)

        if query_vectors.shape[1] != self.dimension:
            raise ValueError(
                f"Query vector dimension mismatch: expected {self.dimension}, got {query_vectors.shape[1]}"
            )

        q = torch.as_tensor(query_vectors, dtype=torch.float32, device=self.device)
        q = torch.nn.functional.normalize(q, p=2.0, dim=-1)

        assert self.vectors is not None
        assert self.ids is not None

        k = min(top_k, self.total_vectors)
        # Inner product on normalized vectors = cosine similarity
        scores = torch.mm(q, self.vectors.T)
        top_scores, top_indices = torch.topk(scores, k=k, dim=-1)
        top_ids = self.ids[top_indices]

        return (
            top_scores.cpu().numpy().astype(np.float32),
            top_ids.cpu().numpy().astype(np.int64),
        )

    def save(self, metadata: Optional[dict[str, Any]] = None) -> None:
        """Persist vectors and IDs to safetensors and metadata to JSON."""
        self.index_path.parent.mkdir(parents=True, exist_ok=True)

        if self.vectors is None or self.vectors.shape[0] == 0:
            v_save = torch.empty((0, self.dimension), dtype=torch.float32)
            id_save = torch.empty((0,), dtype=torch.int64)
        else:
            assert self.ids is not None
            v_save = self.vectors.contiguous().cpu()
            id_save = self.ids.contiguous().cpu()

        safetensors.torch.save_file(
            {"vectors": v_save, "ids": id_save},
            str(self.index_path),
        )

        if metadata:
            self.metadata.update(metadata)
        self.metadata["backend"] = "torch"
        self.metadata["device"] = self.device_str
        self.metadata["dimension"] = self.dimension
        self.metadata["total_vectors"] = self.total_vectors

        with self.meta_path.open("w", encoding="utf-8") as f:
            json.dump(self.metadata, f, indent=2)

    def load(self) -> None:
        """Load tensors from safetensors and restore metadata."""
        if not self.index_path.exists():
            raise FileNotFoundError(f"Vector index file not found at: {self.index_path}")

        tensors = safetensors.torch.load_file(str(self.index_path))
        self.vectors = tensors["vectors"].to(self.device)
        self.ids = tensors["ids"].to(self.device)
        if self.vectors.shape[0] > 0:
            self.dimension = int(self.vectors.shape[1])

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

