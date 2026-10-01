"""Native PyTorch vector indexing engine using safetensors persistence."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal, Optional

import numpy as np
import safetensors
import safetensors.torch
import torch

from reconciliator.utils.device import clear_accelerator_memory, resolve_device


class TorchVectorEngine:
    """Manages dense vector index using PyTorch tensors with hardware acceleration (MPS/CUDA/CPU).

    Vectors are accumulated and stored in host RAM with geometric capacity pre-allocation to prevent
    GPU driver memory fragmentation and out-of-memory kernel panics. Nearest neighbor search dispatches
    to the configured hardware accelerator (MPS/CUDA/CPU) with chunking and cache cleanup.
    """

    def __init__(
        self,
        index_path: Path | str,
        dimension: int,
        device: Literal["auto", "mps", "cuda", "cpu"] = "auto",
        initial_capacity: int = 10000,
        dtype: Literal["float32", "float16", "bfloat16"] | torch.dtype = "float32",
        mmap: bool = False,
        chunk_size: int = 50000,
    ) -> None:
        self.index_path = Path(index_path)
        self.meta_path = Path(f"{index_path}.meta.json")
        self.dimension = dimension
        self.device_str = resolve_device(device)
        self.device = torch.device(self.device_str)
        self.initial_capacity = max(1000, initial_capacity)
        self.mmap = mmap
        self.chunk_size = max(1000, chunk_size)

        if isinstance(dtype, str):
            dtype_map = {
                "float32": torch.float32,
                "float16": torch.float16,
                "bfloat16": torch.bfloat16,
            }
            self.torch_dtype = dtype_map.get(dtype.lower(), torch.float32)
            self.dtype_str = dtype.lower()
        elif isinstance(dtype, torch.dtype):
            self.torch_dtype = dtype
            self.dtype_str = str(dtype).split(".")[-1]
        else:
            self.torch_dtype = torch.float32
            self.dtype_str = "float32"

        self.index_path.parent.mkdir(parents=True, exist_ok=True)
        self.metadata: dict[str, Any] = {
            "backend": "torch",
            "device": self.device_str,
            "dimension": dimension,
            "dtype": self.dtype_str,
            "model_name": "",
            "template_hash": "",
        }

        self._capacity: int = 0
        self._size: int = 0
        self._vectors: Optional[torch.Tensor] = None
        self._ids: Optional[torch.Tensor] = None
        self._id_to_idx: dict[int, int] = {}

        if self.index_path.exists():
            self.load()
        else:
            self._ensure_capacity(self.initial_capacity)

    @property
    def total_vectors(self) -> int:
        """Return current number of vectors in index."""
        return self._size

    @property
    def vectors(self) -> Optional[torch.Tensor]:
        """Return active slice of vectors for inspection / backward compatibility."""
        if self._vectors is None or self._size == 0:
            return torch.empty((0, self.dimension), dtype=self.torch_dtype, device="cpu")
        return self._vectors[: self._size]

    @property
    def ids(self) -> Optional[torch.Tensor]:
        """Return active slice of IDs for inspection / backward compatibility."""
        if self._ids is None or self._size == 0:
            return torch.empty((0,), dtype=torch.int64, device="cpu")
        return self._ids[: self._size]

    def _ensure_capacity(self, needed: int) -> None:
        """Pre-allocate contiguous host memory geometrically to eliminate frequent reallocations."""
        if self._vectors is None:
            new_cap = max(self.initial_capacity, needed)
            self._vectors = torch.empty(
                (new_cap, self.dimension), dtype=self.torch_dtype, device="cpu"
            )
            self._ids = torch.empty((new_cap,), dtype=torch.int64, device="cpu")
            self._capacity = new_cap
            self._size = 0
            return

        if needed <= self._capacity:
            return

        # Double capacity geometrically
        new_cap = max(self._capacity * 2, needed)
        new_vecs = torch.empty((new_cap, self.dimension), dtype=self.torch_dtype, device="cpu")
        new_ids = torch.empty((new_cap,), dtype=torch.int64, device="cpu")

        if self._size > 0:
            assert self._vectors is not None
            assert self._ids is not None
            new_vecs[: self._size] = self._vectors[: self._size]
            new_ids[: self._size] = self._ids[: self._size]

        self._vectors = new_vecs
        self._ids = new_ids
        self._capacity = new_cap

    def remove_ids(self, ids: list[int] | np.ndarray) -> int:
        """Remove vectors with the given IDs from the index."""
        if self._size == 0 or len(ids) == 0:
            return 0

        target_list = ids if isinstance(ids, list) else ids.tolist()
        to_remove = set(target_list).intersection(self._id_to_idx.keys())
        if not to_remove:
            return 0

        assert self._ids is not None
        assert self._vectors is not None

        active_ids = self._ids[: self._size]
        target_tensor = torch.as_tensor(list(to_remove), dtype=torch.int64, device="cpu")
        mask = torch.isin(active_ids, target_tensor, invert=True)

        kept_vecs = self._vectors[: self._size][mask]
        kept_ids = active_ids[mask]
        new_size = int(kept_ids.shape[0])

        self._vectors[:new_size] = kept_vecs
        self._ids[:new_size] = kept_ids
        self._size = new_size

        # Re-index remaining IDs
        self._id_to_idx = {int(rid): i for i, rid in enumerate(kept_ids.tolist())}
        return len(to_remove)

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

        # Normalize on CPU
        new_vecs = torch.as_tensor(vectors, dtype=self.torch_dtype, device="cpu")
        new_vecs = torch.nn.functional.normalize(new_vecs, p=2.0, dim=-1)

        if ids is not None:
            id_arr = np.ascontiguousarray(ids, dtype=np.int64)
            if id_arr.ndim != 1:
                id_arr = id_arr.flatten()
            if len(id_arr) != len(vectors):
                raise ValueError(
                    f"Number of IDs ({len(id_arr)}) must match number of vectors ({len(vectors)})"
                )
            new_ids_list = [int(x) for x in id_arr]
            # O(1) duplicate removal if any ID already exists in the index
            duplicates = [x for x in new_ids_list if x in self._id_to_idx]
            if duplicates:
                self.remove_ids(duplicates)
        else:
            start_id = self._size
            new_ids_list = list(range(start_id, start_id + len(vectors)))

        n = len(new_vecs)
        self._ensure_capacity(self._size + n)
        assert self._vectors is not None
        assert self._ids is not None

        new_ids_tensor = torch.as_tensor(new_ids_list, dtype=torch.int64, device="cpu")
        self._vectors[self._size : self._size + n] = new_vecs
        self._ids[self._size : self._size + n] = new_ids_tensor

        for offset, rid in enumerate(new_ids_list):
            self._id_to_idx[rid] = self._size + offset

        self._size += n
        return new_ids_list

    def search(self, query_vectors: np.ndarray, top_k: int = 50) -> tuple[np.ndarray, np.ndarray]:
        """Search top-k nearest neighbors for query vectors with chunking and accelerator cache cleanup."""
        if self._size == 0:
            return np.empty((len(query_vectors), 0), dtype=np.float32), np.empty(
                (len(query_vectors), 0), dtype=np.int64
            )

        if query_vectors.ndim == 1:
            query_vectors = np.expand_dims(query_vectors, axis=0)

        if query_vectors.shape[1] != self.dimension:
            raise ValueError(
                f"Query vector dimension mismatch: expected {self.dimension}, got {query_vectors.shape[1]}"
            )

        assert self._vectors is not None
        assert self._ids is not None

        active_vectors = self._vectors[: self._size]
        active_ids = self._ids[: self._size]
        k = min(top_k, self._size)

        # On CPU: run direct matrix multiplication with Accelerate BLAS
        if self.device.type == "cpu":
            q = torch.as_tensor(query_vectors, dtype=self.torch_dtype, device="cpu")
            q = torch.nn.functional.normalize(q, p=2.0, dim=-1)
            scores = torch.mm(q, active_vectors.T)
            top_scores, top_indices = torch.topk(scores, k=k, dim=-1)
            top_ids = active_ids[top_indices]
            return (
                top_scores.float().numpy().astype(np.float32),
                top_ids.numpy().astype(np.int64),
            )

        # On accelerator (MPS or CUDA): run with bounded memory chunking
        try:
            q = torch.as_tensor(query_vectors, dtype=self.torch_dtype, device=self.device)
            q = torch.nn.functional.normalize(q, p=2.0, dim=-1)

            chunk_size = self.chunk_size
            if self._size <= chunk_size:
                v_dev = active_vectors.to(self.device)
                scores = torch.mm(q, v_dev.T)
                top_scores, top_indices = torch.topk(scores, k=k, dim=-1)
                top_ids = active_ids[top_indices.cpu()]
                res_scores = top_scores.cpu().float().numpy().astype(np.float32)
                res_ids = top_ids.numpy().astype(np.int64)
                del v_dev, scores
            else:
                all_top_scores = []
                all_top_indices = []
                for c_start in range(0, self._size, chunk_size):
                    c_end = min(c_start + chunk_size, self._size)
                    chunk = active_vectors[c_start:c_end].to(self.device)
                    c_scores = torch.mm(q, chunk.T)
                    c_top_scores, c_top_idx = torch.topk(c_scores, k=min(k, chunk.shape[0]), dim=-1)
                    all_top_scores.append(c_top_scores.cpu())
                    all_top_indices.append((c_top_idx + c_start).cpu())
                    del chunk, c_scores

                cat_scores = torch.cat(all_top_scores, dim=-1)
                cat_indices = torch.cat(all_top_indices, dim=-1)
                final_scores, final_idx = torch.topk(cat_scores, k=k, dim=-1)
                top_idx_merged = torch.gather(cat_indices, 1, final_idx)
                top_ids = active_ids[top_idx_merged]
                res_scores = final_scores.float().numpy().astype(np.float32)
                res_ids = top_ids.numpy().astype(np.int64)
        finally:
            clear_accelerator_memory(self.device)

        return res_scores, res_ids

    def save(self, metadata: Optional[dict[str, Any]] = None) -> None:
        """Persist vectors and IDs to safetensors directly from host RAM."""
        self.index_path.parent.mkdir(parents=True, exist_ok=True)

        if self._vectors is None or self._size == 0:
            v_save = torch.empty((0, self.dimension), dtype=self.torch_dtype)
            id_save = torch.empty((0,), dtype=torch.int64)
        else:
            assert self._ids is not None
            v_save = self._vectors[: self._size].contiguous().to(self.torch_dtype)
            id_save = self._ids[: self._size].contiguous()

        safetensors.torch.save_file(
            {"vectors": v_save, "ids": id_save},
            str(self.index_path),
        )

        if metadata:
            self.metadata.update(metadata)
        self.metadata["backend"] = "torch"
        self.metadata["device"] = self.device_str
        self.metadata["dimension"] = self.dimension
        self.metadata["dtype"] = self.dtype_str
        self.metadata["total_vectors"] = self.total_vectors

        with self.meta_path.open("w", encoding="utf-8") as f:
            json.dump(self.metadata, f, indent=2)

    def load(self) -> None:
        """Load tensors from safetensors into host memory or mmap and restore metadata."""
        if not self.index_path.exists():
            raise FileNotFoundError(f"Vector index file not found at: {self.index_path}")

        if self.mmap:
            with safetensors.safe_open(str(self.index_path), framework="pt", device="cpu") as f:
                loaded_vecs = f.get_tensor("vectors")
                loaded_ids = f.get_tensor("ids")

            if loaded_vecs.dtype != self.torch_dtype:
                loaded_vecs = loaded_vecs.to(self.torch_dtype)

            n = int(loaded_vecs.shape[0])
            if n > 0:
                self.dimension = int(loaded_vecs.shape[1])
            self._size = n
            self._capacity = n
            self._vectors = loaded_vecs
            self._ids = loaded_ids
            self._id_to_idx = {int(rid): i for i, rid in enumerate(loaded_ids.tolist())}
        else:
            tensors = safetensors.torch.load_file(str(self.index_path), device="cpu")
            loaded_vecs = tensors["vectors"]
            loaded_ids = tensors["ids"]

            if loaded_vecs.dtype != self.torch_dtype:
                loaded_vecs = loaded_vecs.to(self.torch_dtype)

            n = int(loaded_vecs.shape[0])
            if n > 0:
                self.dimension = int(loaded_vecs.shape[1])

            self._capacity = max(self.initial_capacity, n * 2 if n > 0 else self.initial_capacity)
            self._vectors = torch.empty(
                (self._capacity, self.dimension), dtype=self.torch_dtype, device="cpu"
            )
            self._ids = torch.empty((self._capacity,), dtype=torch.int64, device="cpu")

            if n > 0:
                self._vectors[:n] = loaded_vecs
                self._ids[:n] = loaded_ids
            self._size = n
            self._id_to_idx = {int(rid): i for i, rid in enumerate(loaded_ids.tolist())}

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
