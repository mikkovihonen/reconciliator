"""GPU-accelerated DBSCAN density-based clustering engine."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Optional

import numpy as np
import torch
import torch.nn.functional as F

from reconciliator.utils.device import clear_accelerator_memory, resolve_compute_device
from reconciliator.utils.union_find import UnionFind

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DBSCANResult:
    """Result of GPU density clustering."""

    assignments: np.ndarray  # Shape (N,), -1 for noise (if discarded) or cluster ID >= 0
    core_mask: np.ndarray    # Shape (N,), boolean mask of core points
    num_clusters: int
    num_noise: int
    elapsed_seconds: float


class GPUDBSCANEngine:
    """Tiled GPU-accelerated DBSCAN engine supporting Metal MPS, CUDA, and CPU."""

    def __init__(
        self,
        eps: float = 0.25,
        min_samples: int = 2,
        metric: str = "cosine",
        noise_strategy: str = "singleton",
        tile_size: int = 4096,
        device: Optional[torch.device] = None,
    ) -> None:
        if eps <= 0:
            raise ValueError(f"eps must be positive, got {eps}")
        if min_samples < 1:
            raise ValueError(f"min_samples must be at least 1, got {min_samples}")
        if tile_size < 1:
            raise ValueError(f"tile_size must be at least 1, got {tile_size}")
        self.eps = eps
        self.min_samples = min_samples
        self.metric = metric.lower()
        self.noise_strategy = noise_strategy.lower()
        self.tile_size = tile_size
        self.device = device or resolve_compute_device("auto")

    def fit(self, embeddings: torch.Tensor) -> DBSCANResult:
        """Run GPU density clustering over embeddings.

        Args:
            embeddings: 2D Tensor of shape (N, D).

        Returns:
            DBSCANResult with cluster assignments and noise points.
        """
        start_time = time.perf_counter()
        n = embeddings.size(0)

        if n == 0:
            return DBSCANResult(
                assignments=np.empty(0, dtype=np.int64),
                core_mask=np.empty(0, dtype=bool),
                num_clusters=0,
                num_noise=0,
                elapsed_seconds=time.perf_counter() - start_time,
            )

        if n == 1:
            is_core = self.min_samples <= 1
            label = 0 if (is_core or self.noise_strategy != "discard") else -1
            return DBSCANResult(
                assignments=np.array([label], dtype=np.int64),
                core_mask=np.array([is_core], dtype=bool),
                num_clusters=1 if label >= 0 else 0,
                num_noise=0 if label >= 0 else 1,
                elapsed_seconds=time.perf_counter() - start_time,
            )

        dev = self.device
        x = embeddings.to(dev, dtype=torch.float32)

        if self.metric == "cosine":
            x = F.normalize(x, p=2, dim=1)

        # 1. First pass: identify core points
        neighbor_counts = torch.zeros(n, dtype=torch.long, device=dev)
        all_neighbor_lists: list[list[int]] = [[] for _ in range(n)]

        for i in range(0, n, self.tile_size):
            chunk = x[i : i + self.tile_size]
            if self.metric == "cosine":
                sims = torch.mm(chunk, x.t())
                within_eps = sims >= (1.0 - self.eps)
            else:
                dists = torch.cdist(chunk, x, p=2.0)
                within_eps = dists <= self.eps

            neighbor_counts[i : i + self.tile_size] = within_eps.sum(dim=1)

            # Store neighbor indices for graph expansion
            nz = within_eps.nonzero(as_tuple=False).cpu().numpy()
            for row_idx, col_idx in nz:
                all_neighbor_lists[i + int(row_idx)].append(int(col_idx))

        is_core_tensor = neighbor_counts >= self.min_samples
        core_mask_np = is_core_tensor.cpu().numpy()

        # 2. Graph expansion via Union-Find
        uf = UnionFind[int](range(n))
        visited_border = np.zeros(n, dtype=bool)

        for p_idx in range(n):
            if not core_mask_np[p_idx]:
                continue
            neighbors = all_neighbor_lists[p_idx]
            for q_idx in neighbors:
                if q_idx == p_idx:
                    continue
                if core_mask_np[q_idx]:
                    uf.union(p_idx, q_idx)
                else:
                    # Border point connected to core point
                    if not visited_border[q_idx]:
                        uf.union(p_idx, q_idx)
                        visited_border[q_idx] = True

        # 3. Label assignments and noise strategy
        root_to_cluster: dict[int, int] = {}
        assignments = np.full(n, -1, dtype=np.int64)
        noise_count = 0

        for p_idx in range(n):
            if core_mask_np[p_idx] or visited_border[p_idx]:
                root = uf.find(p_idx)
                if root not in root_to_cluster:
                    root_to_cluster[root] = len(root_to_cluster)
                assignments[p_idx] = root_to_cluster[root]
            else:
                # Noise point
                noise_count += 1
                if self.noise_strategy == "singleton":
                    cluster_id = len(root_to_cluster)
                    root_to_cluster[p_idx + n] = cluster_id
                    assignments[p_idx] = cluster_id
                elif self.noise_strategy == "isolate":
                    # Assign to a dedicated isolate cluster or leave as negative
                    cluster_id = len(root_to_cluster)
                    root_to_cluster[p_idx + n] = cluster_id
                    assignments[p_idx] = cluster_id
                else:  # discard
                    assignments[p_idx] = -1

        num_clusters = len(root_to_cluster)
        elapsed = time.perf_counter() - start_time
        clear_accelerator_memory(dev)

        return DBSCANResult(
            assignments=assignments,
            core_mask=core_mask_np,
            num_clusters=num_clusters,
            num_noise=noise_count,
            elapsed_seconds=elapsed,
        )
