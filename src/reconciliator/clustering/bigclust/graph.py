"""Sparse similarity graph abstractions for GPU-accelerated entity clustering."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Generic, Optional, TypeVar

import torch

TId = TypeVar("TId")


@dataclass
class SparseSimilarityGraph(Generic[TId]):
    """Represents a sparse similarity graph constructed from pairwise entity scoring."""

    nodes: list[TId]
    node_to_idx: dict[TId, int]
    edge_index: torch.Tensor  # shape (2, E), dtype torch.long
    edge_weight: torch.Tensor  # shape (E,), dtype torch.float32

    def to_device(self, device: torch.device | str) -> SparseSimilarityGraph[TId]:
        """Move graph tensors to target device (e.g. 'mps' or 'cuda')."""
        return SparseSimilarityGraph(
            nodes=self.nodes,
            node_to_idx=self.node_to_idx,
            edge_index=self.edge_index.to(device),
            edge_weight=self.edge_weight.to(device),
        )

    def to_sparse_coo(self) -> torch.Tensor:
        """Convert to torch.sparse_coo_tensor of shape (N, N)."""
        n = len(self.nodes)
        return torch.sparse_coo_tensor(
            self.edge_index, self.edge_weight, size=(n, n)
        ).coalesce()


def build_sparse_similarity_graph(
    nodes: Sequence[TId],
    edges: Iterable[tuple[TId, TId, float]],
    min_weight: float = 0.0,
    device: Optional[torch.device | str] = None,
) -> SparseSimilarityGraph[TId]:
    """Construct a SparseSimilarityGraph from generic nodes and weighted pairwise edges.

    Supports bidirectional (symmetric) edge creation for both WEMI records and Agent entities.
    """
    node_list = list(nodes)
    node_to_idx: dict[TId, int] = {node: i for i, node in enumerate(node_list)}

    src_indices: list[int] = []
    dst_indices: list[int] = []
    weights: list[float] = []

    for u, v, weight in edges:
        if weight < min_weight:
            continue
        idx_u = node_to_idx.get(u)
        idx_v = node_to_idx.get(v)
        if idx_u is not None and idx_v is not None and idx_u != idx_v:
            # Symmetrize edge (u -> v and v -> u)
            src_indices.extend([idx_u, idx_v])
            dst_indices.extend([idx_v, idx_u])
            weights.extend([weight, weight])

    if src_indices:
        edge_index = torch.tensor([src_indices, dst_indices], dtype=torch.long)
        edge_weight = torch.tensor(weights, dtype=torch.float32)
    else:
        edge_index = torch.empty((2, 0), dtype=torch.long)
        edge_weight = torch.empty((0,), dtype=torch.float32)

    if device is not None:
        edge_index = edge_index.to(device)
        edge_weight = edge_weight.to(device)

    return SparseSimilarityGraph(
        nodes=node_list,
        node_to_idx=node_to_idx,
        edge_index=edge_index,
        edge_weight=edge_weight,
    )
