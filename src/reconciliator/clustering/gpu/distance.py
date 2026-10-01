"""GPU-accelerated chunked distance matrix and sparse graph construction."""

from __future__ import annotations

import logging
from typing import Optional

import torch
import torch.nn.functional as F

from reconciliator.utils.device import resolve_compute_device

logger = logging.getLogger(__name__)


def compute_pairwise_distances(
    embeddings: torch.Tensor,
    metric: str = "cosine",
    chunk_size: int = 4096,
    device: Optional[torch.device] = None,
) -> torch.Tensor:
    """Compute full pairwise distance matrix in memory-safe GPU chunks.

    Args:
        embeddings: 2D Tensor of shape (N, D).
        metric: Distance metric ('cosine' or 'euclidean').
        chunk_size: Row batch size for chunked evaluation.
        device: Target compute device.

    Returns:
        Pairwise distance matrix of shape (N, N) on the target device.
    """
    dev = device or resolve_compute_device("auto")
    x = embeddings.to(dev, dtype=torch.float32)
    n = x.size(0)

    dist_matrix = torch.empty((n, n), dtype=torch.float32, device=dev)

    if metric == "cosine":
        x_norm = F.normalize(x, p=2, dim=1)
        for i in range(0, n, chunk_size):
            chunk = x_norm[i : i + chunk_size]
            sim = torch.mm(chunk, x_norm.t())
            dist_matrix[i : i + chunk_size] = (1.0 - sim).clamp(min=0.0)
    elif metric == "euclidean":
        for i in range(0, n, chunk_size):
            chunk = x[i : i + chunk_size]
            dist_matrix[i : i + chunk_size] = torch.cdist(chunk, x, p=2.0)
    else:
        raise ValueError(f"Unsupported distance metric: '{metric}'")

    return dist_matrix


def compute_sparse_similarity_graph(
    embeddings: torch.Tensor,
    threshold: float = 0.70,
    top_k: Optional[int] = None,
    metric: str = "cosine",
    chunk_size: int = 4096,
    device: Optional[torch.device] = None,
) -> tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
    """Compute sparse similarity edges on GPU above threshold or up to top_k.

    Args:
        embeddings: 2D Tensor of shape (N, D).
        threshold: Minimum similarity threshold.
        top_k: Optional max neighbors per node.
        metric: Similarity metric ('cosine' or 'euclidean').
        chunk_size: Row batch size.
        device: Target compute device.

    Returns:
        Tuple of (row_indices, col_indices, weights) tensors on CPU.
    """
    dev = device or resolve_compute_device("auto")
    x = embeddings.to(dev, dtype=torch.float32)
    n = x.size(0)

    rows_list: list[torch.Tensor] = []
    cols_list: list[torch.Tensor] = []
    vals_list: list[torch.Tensor] = []

    if metric == "cosine":
        x_norm = F.normalize(x, p=2, dim=1)
        for i in range(0, n, chunk_size):
            chunk = x_norm[i : i + chunk_size]
            sims = torch.mm(chunk, x_norm.t())

            # Zero out self-similarities
            chunk_rows = sims.size(0)
            diag_idx = torch.arange(chunk_rows, device=dev)
            sims[diag_idx, i + diag_idx] = -float("inf")

            if top_k is not None and top_k < n:
                k = min(top_k, n - 1)
                top_vals, top_indices = torch.topk(sims, k=k, dim=1)
                mask = top_vals >= threshold
                valid_rows = (
                    torch.arange(i, i + chunk_rows, device=dev)
                    .unsqueeze(1)
                    .expand_as(top_vals)[mask]
                )
                valid_cols = top_indices[mask]
                valid_vals = top_vals[mask]
            else:
                mask = sims >= threshold
                nz = mask.nonzero(as_tuple=False)
                valid_rows = nz[:, 0] + i
                valid_cols = nz[:, 1]
                valid_vals = sims[mask]

            if valid_rows.numel() > 0:
                rows_list.append(valid_rows.cpu())
                cols_list.append(valid_cols.cpu())
                vals_list.append(valid_vals.cpu())

    elif metric == "euclidean":
        for i in range(0, n, chunk_size):
            chunk = x[i : i + chunk_size]
            dists = torch.cdist(chunk, x, p=2.0)
            sims = 1.0 / (1.0 + dists)

            chunk_rows = sims.size(0)
            diag_idx = torch.arange(chunk_rows, device=dev)
            sims[diag_idx, i + diag_idx] = -float("inf")

            if top_k is not None and top_k < n:
                k = min(top_k, n - 1)
                top_vals, top_indices = torch.topk(sims, k=k, dim=1)
                mask = top_vals >= threshold
                valid_rows = (
                    torch.arange(i, i + chunk_rows, device=dev)
                    .unsqueeze(1)
                    .expand_as(top_vals)[mask]
                )
                valid_cols = top_indices[mask]
                valid_vals = top_vals[mask]
            else:
                mask = sims >= threshold
                nz = mask.nonzero(as_tuple=False)
                valid_rows = nz[:, 0] + i
                valid_cols = nz[:, 1]
                valid_vals = sims[mask]

            if valid_rows.numel() > 0:
                rows_list.append(valid_rows.cpu())
                cols_list.append(valid_cols.cpu())
                vals_list.append(valid_vals.cpu())
    else:
        raise ValueError(f"Unsupported metric: '{metric}'")

    if not rows_list:
        return (
            torch.empty(0, dtype=torch.long),
            torch.empty(0, dtype=torch.long),
            torch.empty(0, dtype=torch.float32),
        )

    return (
        torch.cat(rows_list, dim=0),
        torch.cat(cols_list, dim=0),
        torch.cat(vals_list, dim=0),
    )

