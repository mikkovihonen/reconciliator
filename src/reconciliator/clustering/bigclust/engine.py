"""BigClust Incremental Clustering Engine on Metal GPU.

Implements the Global Incremental Algorithm (GIA) for Minimum Sum-of-Squares
Clustering (MSSC) via nonsmooth Stochastic Limited Memory Bundle Algorithm (SLMBA).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional, Union

import numpy as np
import torch

from reconciliator.clustering.bigclust.slmba import MetalSLMBAOptimizer, SLMBAParameters
from reconciliator.clustering.bigclust.validity import (
    ClusterValidityMetrics,
    compute_validity_indices,
)

logger = logging.getLogger(__name__)


@dataclass
class IntermediateClusterSolution:
    """Clustering solution for intermediate cluster count l."""

    l: int
    centroids: torch.Tensor
    loss: float
    metrics: ClusterValidityMetrics


@dataclass
class BigClustResult:
    """Complete result of a BigClust execution."""

    optimal_k: int
    optimal_centroids: torch.Tensor
    optimal_metrics: ClusterValidityMetrics
    assignments: torch.Tensor
    intermediate_solutions: dict[int, IntermediateClusterSolution] = field(default_factory=dict)

    def to_numpy(self) -> tuple[np.ndarray, np.ndarray]:
        """Return (centroids, assignments) as NumPy arrays."""
        return (
            self.optimal_centroids.cpu().numpy(),
            self.assignments.cpu().numpy(),
        )


class BigClustMetal:
    """Continuous MSSC clustering engine running on Apple Metal (MPS) GPU.

    Solves the continuous minimum sum-of-squares clustering problem incrementally:
    k = 1 -> k = 2 -> ... -> k = k_max, solving the auxiliary 1-center problem
    at each stage before refining the joint configuration.
    """

    def __init__(
        self,
        k_max: int = 15,
        k_min: int = 2,
        batch_size: Optional[int] = 5000,
        max_candidate_points: int = 20,
        optimizer_params: Optional[SLMBAParameters] = None,
        device: Optional[torch.device] = None,
    ) -> None:
        self.k_max = k_max
        self.k_min = k_min
        self.batch_size = batch_size
        self.max_candidate_points = max_candidate_points

        if device is not None:
            self.device = device
        elif torch.backends.mps.is_available():
            self.device = torch.device("mps")
        elif torch.cuda.is_available():
            self.device = torch.device("cuda")
        else:
            self.device = torch.device("cpu")

        self.optimizer = MetalSLMBAOptimizer(params=optimizer_params, device=self.device)

    def fit(
        self,
        data: Union[torch.Tensor, np.ndarray],
        target_k: Optional[int] = None,
    ) -> BigClustResult:
        """Execute BigClust incremental clustering over dataset.

        Args:
            data: [N, d] float tensor or array of data points.
            target_k: Optional explicit cluster count. If None, k* is automatically
                      selected at minimum Davies-Bouldin index.

        Returns:
            BigClustResult with optimal centroids, assignments, and intermediate solutions.
        """
        if isinstance(data, np.ndarray):
            A = torch.from_numpy(data).float().to(self.device)
        else:
            A = data.float().to(self.device)

        N, d = A.shape
        if N == 0:
            raise ValueError("Cannot cluster empty dataset.")

        effective_k_max = min(self.k_max, N)
        if effective_k_max <= 1:
            # Single cluster
            c = A.mean(dim=0, keepdim=True)
            metrics = compute_validity_indices(A, c, device=self.device)
            return BigClustResult(
                optimal_k=1,
                optimal_centroids=c,
                optimal_metrics=metrics,
                assignments=torch.zeros(N, dtype=torch.long, device=self.device),
                intermediate_solutions={1: IntermediateClusterSolution(1, c, metrics.loss, metrics)},
            )

        intermediate: dict[int, IntermediateClusterSolution] = {}

        # -------------------------------------------------------------
        # Step 0: Global Centroid for l = 1
        # -------------------------------------------------------------
        c1 = A.mean(dim=0, keepdim=True)  # [1, d]
        diff1 = A - c1
        d_min = (diff1 * diff1).sum(dim=1)  # [N]
        loss1 = d_min.sum().item()
        metrics1 = compute_validity_indices(A, c1, device=self.device)

        curr_centroids = c1
        intermediate[1] = IntermediateClusterSolution(
            l=1,
            centroids=curr_centroids,
            loss=loss1,
            metrics=metrics1,
        )

        logger.debug("BigClust [l=1]: Loss=%.4f", loss1)

        # -------------------------------------------------------------
        # Incremental Global Optimization: l = 2 ... effective_k_max
        # -------------------------------------------------------------
        for l in range(2, effective_k_max + 1):
            # Step 1: Candidate starting points for 1-center auxiliary problem
            candidates = self._generate_candidate_starting_points(A, curr_centroids, d_min)

            # Step 2: Solve auxiliary problem for each candidate using SLMBA
            best_aux_center: Optional[torch.Tensor] = None
            best_aux_loss = float("inf")

            for cand in candidates:
                opt_cand, aux_loss = self.optimizer.optimize_auxiliary(
                    candidate_center=cand,
                    data=A,
                    d_min_prev=d_min,
                    batch_size=self.batch_size,
                )
                if aux_loss < best_aux_loss:
                    best_aux_loss = aux_loss
                    best_aux_center = opt_cand

            if best_aux_center is None:
                best_aux_center = candidates[0].unsqueeze(0)

            # Step 3: Initialize full l-cluster problem
            X_init = torch.cat([curr_centroids, best_aux_center], dim=0)

            # Step 4: Solve full l-cluster joint optimization using SLMBA
            curr_centroids, full_loss = self.optimizer.optimize_clustering(
                init_centers=X_init,
                data=A,
                batch_size=self.batch_size,
            )

            # Step 5: Update d_min across all N points and compute validity metrics
            norm_A = (A * A).sum(dim=1, keepdim=True)
            norm_X = (curr_centroids * curr_centroids).sum(dim=1, keepdim=True).T
            D = norm_A - 2.0 * torch.matmul(A, curr_centroids.T) + norm_X
            d_min, _ = torch.min(D, dim=1)

            metrics = compute_validity_indices(A, curr_centroids, device=self.device)
            intermediate[l] = IntermediateClusterSolution(
                l=l,
                centroids=curr_centroids.clone(),
                loss=metrics.loss,
                metrics=metrics,
            )

            logger.debug(
                "BigClust [l=%d]: Loss=%.4f, Davies-Bouldin=%.4f, Dunn=%.4f",
                l,
                metrics.loss,
                metrics.davies_bouldin,
                metrics.dunn_index,
            )

        # -------------------------------------------------------------
        # Determine Optimal k*
        # -------------------------------------------------------------
        if target_k is not None and target_k in intermediate:
            opt_k = target_k
        else:
            # Automatic selection: minimum Davies-Bouldin index for l in [k_min, effective_k_max]
            eligible_k = [k for k in range(self.k_min, effective_k_max + 1) if k in intermediate]
            if eligible_k:
                opt_k = min(eligible_k, key=lambda k: intermediate[k].metrics.davies_bouldin)
            else:
                opt_k = effective_k_max

        opt_sol = intermediate[opt_k]

        # Compute final assignments for optimal k
        final_norm_X = (opt_sol.centroids * opt_sol.centroids).sum(dim=1, keepdim=True).T
        final_D = (A * A).sum(dim=1, keepdim=True) - 2.0 * torch.matmul(A, opt_sol.centroids.T) + final_norm_X
        _, final_assignments = torch.min(final_D, dim=1)

        return BigClustResult(
            optimal_k=opt_k,
            optimal_centroids=opt_sol.centroids,
            optimal_metrics=opt_sol.metrics,
            assignments=final_assignments,
            intermediate_solutions=intermediate,
        )

    def _generate_candidate_starting_points(
        self, A: torch.Tensor, centroids: torch.Tensor, d_min: torch.Tensor
    ) -> list[torch.Tensor]:
        """Generate candidate starting points for the auxiliary 1-center problem (Step 2)."""
        N, d = A.shape
        mean_d = d_min.mean().item()

        # Points whose distance to existing centers exceeds average radius
        far_mask = d_min >= mean_d
        far_indices = torch.nonzero(far_mask).squeeze(-1)

        if len(far_indices) == 0:
            # Fallback to top furthest points
            _, far_indices = torch.topk(d_min, k=min(self.max_candidate_points, N))

        if len(far_indices) > self.max_candidate_points:
            # Subsample candidates with highest distance
            top_vals, top_idx = torch.topk(d_min[far_indices], k=self.max_candidate_points)
            candidate_points = A[far_indices[top_idx]]
        else:
            candidate_points = A[far_indices]

        # Deduplicate candidate centers that are too close to each other
        toler = 0.05 * mean_d
        unique_candidates: list[torch.Tensor] = []

        for pt in candidate_points:
            is_duplicate = False
            for existing in unique_candidates:
                dist = (pt - existing).pow(2).sum().item()
                if dist < toler:
                    is_duplicate = True
                    break
            if not is_duplicate:
                unique_candidates.append(pt)
            if len(unique_candidates) >= 10:
                break

        if not unique_candidates:
            unique_candidates = [candidate_points[0]]

        return unique_candidates
