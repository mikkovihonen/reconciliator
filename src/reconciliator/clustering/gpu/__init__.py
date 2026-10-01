"""GPU hardware acceleration foundations for clustering engines."""

from reconciliator.clustering.gpu.device import (
    clear_accelerator_memory,
    is_gpu_available,
    resolve_compute_device,
)
from reconciliator.clustering.gpu.distance import (
    compute_pairwise_distances,
    compute_sparse_similarity_graph,
)

__all__ = [
    "clear_accelerator_memory",
    "compute_pairwise_distances",
    "compute_sparse_similarity_graph",
    "is_gpu_available",
    "resolve_compute_device",
]
