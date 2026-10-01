"""BigClust Clustering Subsystem for Reconciliator.

Provides continuous Minimum Sum-of-Squares Clustering (MSSC) via nonsmooth
Stochastic Limited Memory Bundle Algorithm (SLMBA) on Metal GPU.
"""

from reconciliator.clustering.bigclust.engine import (
    BigClustMetal,
    BigClustResult,
    IntermediateClusterSolution,
)
from reconciliator.clustering.bigclust.multiplex import BigClustMultiplexClusterer
from reconciliator.clustering.bigclust.slmba import MetalSLMBAOptimizer, SLMBAParameters
from reconciliator.clustering.bigclust.validity import (
    ClusterValidityMetrics,
    compute_validity_indices,
)

__all__ = [
    "BigClustMetal",
    "BigClustResult",
    "IntermediateClusterSolution",
    "BigClustMultiplexClusterer",
    "MetalSLMBAOptimizer",
    "SLMBAParameters",
    "ClusterValidityMetrics",
    "compute_validity_indices",
]
