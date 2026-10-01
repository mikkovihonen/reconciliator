"""GPU-accelerated Correlation Clustering (CC-Pivot) engine.

Supports parallel and serial pivot algorithms over positive similarity graphs
with strict negative edge veto constraints on Metal MPS, CUDA, and CPU.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Optional

import torch
import torch.nn.functional as F

from reconciliator.utils.device import clear_accelerator_memory, resolve_compute_device

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CorrelationClusterResult:
    """Result of correlation clustering."""

    assignments: torch.Tensor
    num_clusters: int
    elapsed_seconds: float


class CCPivotEngine:
    """GPU-accelerated Correlation Clustering CC-Pivot engine."""

    def __init__(
        self,
        positive_threshold: float = 0.70,
        max_parallel_pivots: int = 256,
        algorithm: str = "parallel_pivot",
        device: Optional[torch.device] = None,
    ) -> None:
        if max_parallel_pivots < 1:
            raise ValueError(f"max_parallel_pivots must be at least 1, got {max_parallel_pivots}")
        if algorithm not in ("parallel_pivot", "serial_pivot"):
            raise ValueError(
                f"Unsupported algorithm: '{algorithm}'. Supported algorithms: 'parallel_pivot', 'serial_pivot'"
            )
        self.positive_threshold = positive_threshold
        self.max_parallel_pivots = max_parallel_pivots
        self.algorithm = algorithm
        self.device = device or resolve_compute_device("auto")

    def fit(
        self,
        embeddings: torch.Tensor,
        negative_mask: Optional[torch.Tensor] = None,
    ) -> CorrelationClusterResult:
        """Partition embeddings using correlation clustering CC-Pivot.

        Args:
            embeddings: 2D Tensor of shape (N, D).
            negative_mask: Optional boolean Tensor of shape (N, N) where True
                represents a hard veto (cannot-link constraint).

        Returns:
            CorrelationClusterResult with cluster assignment tensor.
        """
        start_time = time.perf_counter()
        n = embeddings.size(0)

        if n == 0:
            return CorrelationClusterResult(
                assignments=torch.empty(0, dtype=torch.long),
                num_clusters=0,
                elapsed_seconds=time.perf_counter() - start_time,
            )

        if n == 1:
            return CorrelationClusterResult(
                assignments=torch.zeros(1, dtype=torch.long),
                num_clusters=1,
                elapsed_seconds=time.perf_counter() - start_time,
            )

        x = embeddings.to(self.device, dtype=torch.float32)
        x_norm = F.normalize(x, p=2, dim=1)
        sim_matrix = torch.mm(x_norm, x_norm.t())

        # Construct positive adjacency mask
        pos_mask = sim_matrix >= self.positive_threshold

        # Incorporate hard negative vetoes
        if negative_mask is not None:
            neg = negative_mask.to(self.device, dtype=torch.bool)
            pos_mask = pos_mask & (~neg)

        # Set diagonal to True (every node links to itself)
        diag_idx = torch.arange(n, device=self.device)
        pos_mask[diag_idx, diag_idx] = True

        assignments = self._run_pivot(pos_mask, n)
        elapsed = time.perf_counter() - start_time
        num_clusters = int(assignments.max().item()) + 1 if n > 0 else 0

        clear_accelerator_memory(self.device)
        return CorrelationClusterResult(
            assignments=assignments.cpu(),
            num_clusters=num_clusters,
            elapsed_seconds=elapsed,
        )

    def _run_pivot(self, pos_mask: torch.Tensor, n: int) -> torch.Tensor:
        """Run batched parallel or serial CC-Pivot."""
        active = torch.ones(n, dtype=torch.bool, device=self.device)
        assignments = torch.full((n,), -1, dtype=torch.long, device=self.device)
        current_cluster = 0

        while active.any():
            # Degree of active nodes within active subgraph
            active_adj = pos_mask & active.unsqueeze(0)
            degrees = active_adj.sum(dim=1)
            degrees = torch.where(active, degrees, torch.zeros_like(degrees))

            if self.algorithm == "serial_pivot":
                pivot = torch.argmax(degrees).item()
                members = active & pos_mask[pivot]
                members[pivot] = True
                assignments[members] = current_cluster
                active[members] = False
                current_cluster += 1
            else:
                # Parallel independent pivot selection
                active_indices = active.nonzero(as_tuple=False).squeeze(-1)
                if active_indices.numel() == 0:
                    break

                sorted_sub_indices = torch.argsort(degrees[active_indices], descending=True)
                sorted_candidates = active_indices[sorted_sub_indices].tolist()

                pivots: list[int] = []
                claimed_mask = torch.zeros(n, dtype=torch.bool, device=self.device)

                for cand in sorted_candidates:
                    if len(pivots) >= self.max_parallel_pivots:
                        break
                    if claimed_mask[cand]:
                        continue
                    pivots.append(cand)
                    # Mark neighbors of cand as claimed so no adjacent node is chosen as pivot
                    cand_neighbors = active & pos_mask[cand]
                    claimed_mask = claimed_mask | cand_neighbors

                # Assign clusters to each pivot in the batch
                for p in pivots:
                    if not active[p]:
                        continue
                    members = active & pos_mask[p]
                    members[p] = True
                    assignments[members] = current_cluster
                    active[members] = False
                    current_cluster += 1

        return assignments
