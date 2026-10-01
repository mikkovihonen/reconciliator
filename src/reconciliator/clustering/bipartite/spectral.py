"""GPU-accelerated Bipartite Spectral Co-Clustering engine.

Jointly clusters two interacting entity domains (Works and Agents) via
truncated singular value decomposition (SVD) of the normalized bipartite
incidence matrix on Metal MPS, CUDA, and CPU.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Optional

import numpy as np
import torch
import torch.nn.functional as F

from reconciliator.clustering.gpu.device import clear_accelerator_memory, resolve_compute_device

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BipartiteCoClusterResult:
    """Result of joint bipartite co-clustering."""

    work_assignments: np.ndarray
    agent_assignments: np.ndarray
    num_work_clusters: int
    num_agent_clusters: int
    elapsed_seconds: float


class GPUBipartiteSpectralEngine:
    """GPU-accelerated Bipartite Spectral Co-clustering engine."""

    def __init__(
        self,
        latent_dimensions: int = 32,
        work_k: int = 50,
        agent_k: int = 50,
        iterations: int = 100,
        device: Optional[torch.device] = None,
    ) -> None:
        self.latent_dimensions = latent_dimensions
        self.work_k = work_k
        self.agent_k = agent_k
        self.iterations = iterations
        self.device = device or resolve_compute_device("auto")

    def fit(self, bipartite_incidence: torch.Tensor) -> BipartiteCoClusterResult:
        """Run spectral bipartite co-clustering over Work x Agent incidence matrix.

        Args:
            bipartite_incidence: 2D Tensor of shape (N_works, N_agents) where values
                represent association strengths (e.g. authorship, contribution).

        Returns:
            BipartiteCoClusterResult with assignments for both Works and Agents.
        """
        start_time = time.perf_counter()
        dev = self.device
        b = bipartite_incidence.to(dev, dtype=torch.float32)
        n_works, n_agents = b.shape

        if n_works == 0:
            return BipartiteCoClusterResult(
                work_assignments=np.empty(0, dtype=np.int64),
                agent_assignments=np.empty(n_agents, dtype=np.int64),
                num_work_clusters=0,
                num_agent_clusters=0,
                elapsed_seconds=time.perf_counter() - start_time,
            )

        if n_agents == 0:
            return BipartiteCoClusterResult(
                work_assignments=np.zeros(n_works, dtype=np.int64),
                agent_assignments=np.empty(0, dtype=np.int64),
                num_work_clusters=1,
                num_agent_clusters=0,
                elapsed_seconds=time.perf_counter() - start_time,
            )

        # Degrees
        d1 = b.sum(dim=1).clamp(min=1e-12)
        d2 = b.sum(dim=0).clamp(min=1e-12)

        d1_inv_sqrt = torch.pow(d1, -0.5)
        d2_inv_sqrt = torch.pow(d2, -0.5)

        # Normalized bipartite matrix An = D1^{-1/2} * B * D2^{-1/2}
        a_norm = d1_inv_sqrt.unsqueeze(1) * b * d2_inv_sqrt.unsqueeze(0)

        # Truncated SVD
        k_dim = min(self.latent_dimensions, n_works, n_agents)
        k_dim = max(1, k_dim)

        u, _, vh = torch.linalg.svd(a_norm, full_matrices=False)
        u_k = u[:, :k_dim]
        v_k = vh.t()[:, :k_dim]

        # Rescaled spectral embeddings
        z1 = F.normalize(d1_inv_sqrt.unsqueeze(1) * u_k, p=2, dim=1)
        z2 = F.normalize(d2_inv_sqrt.unsqueeze(1) * v_k, p=2, dim=1)

        # Partition Works and Agents via K-means on spectral coordinates
        k_w = min(self.work_k, n_works)
        k_a = min(self.agent_k, n_agents)

        w_assigns = self._kmeans(z1, k=k_w, max_iter=self.iterations)
        a_assigns = self._kmeans(z2, k=k_a, max_iter=self.iterations)

        elapsed = time.perf_counter() - start_time
        clear_accelerator_memory(dev)

        return BipartiteCoClusterResult(
            work_assignments=w_assigns.cpu().numpy(),
            agent_assignments=a_assigns.cpu().numpy(),
            num_work_clusters=len(set(w_assigns.tolist())),
            num_agent_clusters=len(set(a_assigns.tolist())),
            elapsed_seconds=elapsed,
        )

    def _kmeans(self, x: torch.Tensor, k: int, max_iter: int = 100) -> torch.Tensor:
        """GPU K-Means clustering implementation."""
        n = x.size(0)
        if n <= k:
            return torch.arange(n, device=x.device, dtype=torch.long)
        if k <= 1:
            return torch.zeros(n, device=x.device, dtype=torch.long)

        # Initialize centroids with deterministic stride
        stride = max(1, n // k)
        init_indices = torch.arange(0, n, stride, device=x.device)[:k]
        centroids = x[init_indices].clone()

        labels = torch.zeros(n, dtype=torch.long, device=x.device)

        for _ in range(max_iter):
            # Compute distances: (N, K)
            dists = torch.cdist(x, centroids, p=2.0)
            new_labels = torch.argmin(dists, dim=1)

            if torch.equal(new_labels, labels):
                break
            labels = new_labels

            # Recompute centroids
            for c in range(k):
                mask = labels == c
                if mask.any():
                    centroids[c] = x[mask].mean(dim=0)

        return labels
