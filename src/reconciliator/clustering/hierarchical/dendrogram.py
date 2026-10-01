"""GPU-accelerated Hierarchical Agglomerative Clustering (HAC) dendrogram engine.

Constructs linkage matrices on Metal MPS, CUDA, and CPU using Lance-Williams
updates and provides multi-tier distance threshold cutting for WEMI entities.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Optional

import numpy as np
import torch

from reconciliator.utils.device import clear_accelerator_memory, resolve_compute_device
from reconciliator.clustering.gpu.distance import compute_pairwise_distances

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class HierarchicalTree:
    """Linkage tree and threshold cutter."""

    linkage_matrix: np.ndarray  # Shape (N-1, 4): [cluster_a, cluster_b, distance, sample_count]
    num_samples: int
    elapsed_seconds: float

    def cut_tree(self, distance_threshold: float) -> np.ndarray:
        """Cut dendrogram at distance threshold returning cluster assignments (0 to C-1).

        Args:
            distance_threshold: Maximum linkage distance to merge clusters.

        Returns:
            1D NumPy array of length num_samples with cluster labels.
        """
        n = self.num_samples
        if n == 0:
            return np.empty(0, dtype=np.int64)
        if n == 1 or len(self.linkage_matrix) == 0:
            return np.zeros(n, dtype=np.int64)

        # Disjoint set / Union-Find over 2*N-1 nodes
        parent = list(range(2 * n))

        def find(i: int) -> int:
            path = []
            while parent[i] != i:
                path.append(i)
                i = parent[i]
            for node in path:
                parent[node] = i
            return i

        def union(i: int, j: int, root: int) -> None:
            root_i = find(i)
            root_j = find(j)
            parent[root_i] = root
            parent[root_j] = root

        for step_idx, row in enumerate(self.linkage_matrix):
            c1, c2, dist, _ = row
            if dist > distance_threshold:
                break
            merged_id = n + step_idx
            union(int(c1), int(c2), merged_id)

        # Map distinct roots of original leaf nodes {0..n-1} to contiguous labels
        root_to_label: dict[int, int] = {}
        assignments = np.empty(n, dtype=np.int64)

        for leaf in range(n):
            root = find(leaf)
            if root not in root_to_label:
                root_to_label[root] = len(root_to_label)
            assignments[leaf] = root_to_label[root]

        return assignments


class GPUHierarchicalEngine:
    """Hierarchical agglomerative clustering engine on GPU/CPU."""

    def __init__(
        self,
        linkage: str = "average",
        metric: str = "cosine",
        device: Optional[torch.device] = None,
    ) -> None:
        self.linkage = linkage.lower()
        self.metric = metric.lower()
        if self.linkage == "ward" and self.metric != "euclidean":
            raise ValueError(
                f"Hierarchical clustering with linkage='ward' requires metric='euclidean', got '{self.metric}'"
            )
        self.device = device or resolve_compute_device("auto")

    def fit(self, embeddings: torch.Tensor) -> HierarchicalTree:
        """Build hierarchical linkage tree on device from embeddings.

        Args:
            embeddings: 2D Tensor of shape (N, D).

        Returns:
            HierarchicalTree with computed linkage matrix.
        """
        start_time = time.perf_counter()
        n = embeddings.size(0)

        if n <= 1:
            return HierarchicalTree(
                linkage_matrix=np.empty((0, 4), dtype=np.float64),
                num_samples=n,
                elapsed_seconds=time.perf_counter() - start_time,
            )

        dev = self.device
        # Compute initial N x N distance matrix
        init_dist = compute_pairwise_distances(embeddings, metric=self.metric, device=dev)

        # Allocate full (2N-1, 2N-1) matrix to hold cluster distances
        total_nodes = 2 * n - 1
        d_matrix = torch.full((total_nodes, total_nodes), float("inf"), dtype=torch.float32, device=dev)
        d_matrix[:n, :n] = init_dist

        # Set diagonal to infinity
        diag_idx = torch.arange(total_nodes, device=dev)
        d_matrix[diag_idx, diag_idx] = float("inf")

        cluster_sizes = torch.ones(total_nodes, dtype=torch.float32, device=dev)
        active = torch.zeros(total_nodes, dtype=torch.bool, device=dev)
        active[:n] = True

        linkage_records: list[list[float]] = []

        for step in range(n - 1):
            new_node = n + step
            active_indices = active.nonzero(as_tuple=False).squeeze(-1)

            # Submatrix of active distances
            sub_d = d_matrix[active_indices][:, active_indices]

            # Find minimum entry
            min_val = sub_d.min()
            min_pos = (sub_d == min_val).nonzero(as_tuple=False)[0]
            u_idx = active_indices[min_pos[0]].item()
            v_idx = active_indices[min_pos[1]].item()

            if u_idx > v_idx:
                u_idx, v_idx = v_idx, u_idx

            dist_uv = float(min_val.item())
            size_u = cluster_sizes[u_idx].item()
            size_v = cluster_sizes[v_idx].item()
            new_size = size_u + size_v
            cluster_sizes[new_node] = new_size

            linkage_records.append([float(u_idx), float(v_idx), dist_uv, new_size])

            # Update active set: remove u, v; add new_node
            active[u_idx] = False
            active[v_idx] = False

            remaining_active = active.nonzero(as_tuple=False).squeeze(-1)
            if remaining_active.numel() > 0:
                dist_u = d_matrix[u_idx, remaining_active]
                dist_v = d_matrix[v_idx, remaining_active]

                if self.linkage == "single":
                    new_dist = torch.minimum(dist_u, dist_v)
                elif self.linkage == "complete":
                    new_dist = torch.maximum(dist_u, dist_v)
                elif self.linkage == "ward":
                    # Lance-Williams Ward formula
                    s_m = cluster_sizes[remaining_active]
                    s_uv = size_u + size_v
                    denom = s_uv + s_m
                    d_sq = (
                        ((size_u + s_m) / denom) * (dist_u**2)
                        + ((size_v + s_m) / denom) * (dist_v**2)
                        - (s_m / denom) * (dist_uv**2)
                    )
                    new_dist = torch.sqrt(torch.clamp(d_sq, min=0.0))
                else:  # default: 'average' (UPGMA)
                    new_dist = (size_u * dist_u + size_v * dist_v) / (size_u + size_v)

                d_matrix[new_node, remaining_active] = new_dist
                d_matrix[remaining_active, new_node] = new_dist

            active[new_node] = True

        linkage_array = np.array(linkage_records, dtype=np.float64)
        elapsed = time.perf_counter() - start_time
        clear_accelerator_memory(dev)

        return HierarchicalTree(
            linkage_matrix=linkage_array,
            num_samples=n,
            elapsed_seconds=elapsed,
        )
