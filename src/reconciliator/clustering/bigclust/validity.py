"""Cluster validity metrics on Metal GPU.

Computes Davies-Bouldin and Dunn validity indices to automatically
determine optimal cluster count k* in continuous metric space.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import torch


@dataclass
class ClusterValidityMetrics:
    """Cluster validity indices for a k-partition solution."""

    k: int
    loss: float
    davies_bouldin: float
    dunn_index: float


def compute_validity_indices(
    data: torch.Tensor,
    centroids: torch.Tensor,
    device: Optional[torch.device] = None,
) -> ClusterValidityMetrics:
    """Compute Davies-Bouldin and Dunn indices for centroids on data.

    Args:
        data: [N, d] dataset tensor.
        centroids: [k, d] cluster centroids tensor.
        device: Torch device (MPS / CUDA / CPU).

    Returns:
        ClusterValidityMetrics containing k, loss, Davies-Bouldin, and Dunn values.
    """
    dev = device or centroids.device
    A = data.to(dev).float()
    X = centroids.to(dev).float()

    N, d = A.shape
    k = X.shape[0]

    if k <= 1:
        # Trivial single-cluster solution
        diff = A - X[0]
        loss = (diff * diff).sum().item()
        return ClusterValidityMetrics(k=1, loss=loss, davies_bouldin=float("inf"), dunn_index=0.0)

    # 1. Compute cluster assignments and distances
    norm_A = (A * A).sum(dim=1, keepdim=True)
    norm_X = (X * X).sum(dim=1, keepdim=True).T
    sq_dist = norm_A - 2.0 * torch.matmul(A, X.T) + norm_X
    sq_dist = torch.clamp(sq_dist, min=0.0)
    euc_dist = torch.sqrt(sq_dist)

    min_sq_dist, assignments = torch.min(sq_dist, dim=1)
    loss = min_sq_dist.sum().item()

    # 2. Compute average and maximum cluster radius (fully vectorized without python loops)
    dists_assigned = torch.sqrt(min_sq_dist)
    counts = torch.zeros(k, dtype=torch.float32, device=dev).scatter_add(
        0, assignments, torch.ones_like(dists_assigned)
    )
    sum_radius = torch.zeros(k, dtype=torch.float32, device=dev).scatter_add(
        0, assignments, dists_assigned
    )
    avg_radius = torch.where(counts > 0, sum_radius / counts, torch.zeros_like(sum_radius))

    max_radius = torch.zeros(k, dtype=torch.float32, device=dev).scatter_reduce(
        0, assignments, dists_assigned, reduce="amax", include_self=False
    )
    max_radius = torch.where(counts > 0, max_radius, torch.zeros_like(max_radius))

    # 3. Compute inter-centroid Euclidean distances
    cent_norm = (X * X).sum(dim=1, keepdim=True)
    cent_sq_dist = cent_norm - 2.0 * torch.matmul(X, X.T) + cent_norm.T
    cent_sq_dist = torch.clamp(cent_sq_dist, min=0.0)
    cent_dist = torch.sqrt(cent_sq_dist)

    # Mask diagonal
    eye_mask = torch.eye(k, dtype=torch.bool, device=dev)
    cent_dist_no_diag = cent_dist.masked_fill(eye_mask, 1e9)

    # 4. Davies-Bouldin Index:
    # R_ij = (avg_radius[i] + avg_radius[j]) / dist(c_i, c_j)
    # D_i = max_{j != i} R_ij
    # DB = mean(D_i)
    radius_sum = avg_radius.unsqueeze(1) + avg_radius.unsqueeze(0)  # [k, k]
    r_matrix = radius_sum / (cent_dist_no_diag + 1e-12)
    r_matrix.masked_fill_(eye_mask, -1.0)
    max_r_per_cluster, _ = torch.max(r_matrix, dim=1)
    db_index = max_r_per_cluster.mean().item()

    # 5. Dunn Index:
    # min_{i != j} dist(c_i, c_j) / max_p (max_radius[p])
    min_inter_cluster_dist = cent_dist_no_diag.min().item()
    max_cluster_diameter = max_radius.max().item()
    dunn_index = (
        min_inter_cluster_dist / max(max_cluster_diameter, 1e-12)
        if max_cluster_diameter > 0
        else 0.0
    )

    return ClusterValidityMetrics(
        k=k,
        loss=loss,
        davies_bouldin=db_index,
        dunn_index=dunn_index,
    )
