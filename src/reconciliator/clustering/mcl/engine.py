"""GPU-accelerated Markov Cluster Algorithm (MCL) engine.

Simulates stochastic flow processes via matrix expansion, inflation,
and pruning on Metal MPS, CUDA, and CPU.
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
class MarkovClusterResult:
    """Result of Markov clustering."""

    assignments: np.ndarray  # Shape (N,), cluster ID >= 0
    num_clusters: int
    converged_iterations: int
    elapsed_seconds: float


class GPUMarkovClusterEngine:
    """GPU-accelerated Markov Cluster (MCL) engine."""

    def __init__(
        self,
        expansion_power: int = 2,
        inflation_power: float = 2.0,
        max_iterations: int = 30,
        convergence_tol: float = 1e-5,
        pruning_threshold: float = 1e-4,
        device: Optional[torch.device] = None,
    ) -> None:
        self.expansion_power = max(2, expansion_power)
        self.inflation_power = inflation_power
        self.max_iterations = max_iterations
        self.convergence_tol = convergence_tol
        self.pruning_threshold = pruning_threshold
        self.device = device or resolve_compute_device("auto")

    def fit(
        self,
        embeddings: Optional[torch.Tensor] = None,
        adjacency_matrix: Optional[torch.Tensor] = None,
    ) -> MarkovClusterResult:
        """Run MCL flow simulation to partition nodes.

        Args:
            embeddings: Optional 2D Tensor of shape (N, D).
            adjacency_matrix: Optional non-negative 2D Tensor of shape (N, N).

        Returns:
            MarkovClusterResult with cluster assignments.
        """
        start_time = time.perf_counter()

        if embeddings is not None:
            n = embeddings.size(0)
        elif adjacency_matrix is not None:
            n = adjacency_matrix.size(0)
        else:
            raise ValueError("Either embeddings or adjacency_matrix must be provided.")

        if n == 0:
            return MarkovClusterResult(
                assignments=np.empty(0, dtype=np.int64),
                num_clusters=0,
                converged_iterations=0,
                elapsed_seconds=time.perf_counter() - start_time,
            )

        if n == 1:
            return MarkovClusterResult(
                assignments=np.zeros(1, dtype=np.int64),
                num_clusters=1,
                converged_iterations=1,
                elapsed_seconds=time.perf_counter() - start_time,
            )

        dev = self.device

        # Build initial transition matrix
        if adjacency_matrix is not None:
            m = adjacency_matrix.to(dev, dtype=torch.float32)
        else:
            assert embeddings is not None
            x = embeddings.to(dev, dtype=torch.float32)
            x_norm = F.normalize(x, p=2, dim=1)
            sim = torch.mm(x_norm, x_norm.t())
            m = torch.clamp(sim, min=0.0)

        # Add self-loops to facilitate flow retention
        diag_idx = torch.arange(n, device=dev)
        m[diag_idx, diag_idx] = torch.maximum(m[diag_idx, diag_idx], torch.tensor(1.0, device=dev))

        # Initial column-normalization to form column-stochastic Markov matrix
        col_sums = m.sum(dim=0, keepdim=True).clamp(min=1e-12)
        m = m / col_sums

        converged_iter = self.max_iterations

        for it in range(1, self.max_iterations + 1):
            # 1. Expansion (matrix power)
            if self.expansion_power == 2:
                m_next = torch.mm(m, m)
            else:
                m_next = torch.matrix_power(m, self.expansion_power)

            # 2. Inflation (element-wise power + re-normalization)
            m_inf = torch.pow(m_next, self.inflation_power)
            col_sums = m_inf.sum(dim=0, keepdim=True).clamp(min=1e-12)
            m_inf = m_inf / col_sums

            # 3. Pruning (zero out values below threshold + re-normalize)
            if self.pruning_threshold > 0:
                m_inf = torch.where(m_inf < self.pruning_threshold, torch.zeros_like(m_inf), m_inf)
                col_sums = m_inf.sum(dim=0, keepdim=True).clamp(min=1e-12)
                m_inf = m_inf / col_sums

            diff = torch.norm(m_inf - m).item()
            m = m_inf

            if diff < self.convergence_tol:
                converged_iter = it
                break

        # 4. Cluster Extraction from Attractor Basins
        assignments = self._extract_clusters(m.cpu().numpy(), n)
        num_clusters = len(set(assignments))
        elapsed = time.perf_counter() - start_time

        clear_accelerator_memory(dev)

        return MarkovClusterResult(
            assignments=assignments,
            num_clusters=num_clusters,
            converged_iterations=converged_iter,
            elapsed_seconds=elapsed,
        )

    def _extract_clusters(self, m: np.ndarray, n: int) -> np.ndarray:
        """Extract connected attractor basins from converged stochastic matrix."""
        # Find primary attractor row for each column
        attractors = np.argmax(m, axis=0)

        # Build equivalence classes of attractors
        attractor_to_cluster: dict[int, int] = {}
        assignments = np.empty(n, dtype=np.int64)

        for col_idx in range(n):
            att = int(attractors[col_idx])
            if att not in attractor_to_cluster:
                attractor_to_cluster[att] = len(attractor_to_cluster)
            assignments[col_idx] = attractor_to_cluster[att]

        return assignments
