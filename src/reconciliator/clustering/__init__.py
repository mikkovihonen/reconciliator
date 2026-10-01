"""Clustering and co-reference resolution package for reconciliator."""

from __future__ import annotations

from reconciliator.clustering.agent import AgentClusterer
from reconciliator.clustering.bigclust import (
    BigClustMetal,
    BigClustMultiplexClusterer,
    BigClustResult,
)
from reconciliator.clustering.corpus import CorpusClusterer
from reconciliator.clustering.evaluator import BibliographicalEdgeEvaluator
from reconciliator.clustering.factory import create_corpus_clusterer
from reconciliator.clustering.graph import MultiplexGraphClusterer
from reconciliator.clustering.hybrid import HybridBigClustGraphClusterer
from reconciliator.clustering.protocol import BaseCorpusClusterer, CorpusClustererProtocol
from reconciliator.clustering.wemi_builder import (
    WEMIClusterBuilder,
    extract_expression_partition_keys,
    extract_manifestation_key,
    infer_rda_content_type,
    select_canonical_work_record,
)
from reconciliator.models import (
    AgentCluster,
    AgentClusterAttribute,
    AgentClusterMember,
    AgentClusterResult,
    ClusterEdge,
    CorpusLinkage,
    ExpressionCluster,
    ManifestationCluster,
    MultiplexClusterResult,
    SammelbandBinding,
    WorkCluster,
)

__all__ = [
    "AgentCluster",
    "AgentClusterAttribute",
    "AgentClusterMember",
    "AgentClusterResult",
    "AgentClusterer",
    "BaseCorpusClusterer",
    "BibliographicalEdgeEvaluator",
    "BigClustMetal",
    "BigClustMultiplexClusterer",
    "BigClustResult",
    "ClusterEdge",
    "CorpusClusterer",
    "CorpusClustererProtocol",
    "CorpusLinkage",
    "ExpressionCluster",
    "HybridBigClustGraphClusterer",
    "ManifestationCluster",
    "MultiplexClusterResult",
    "MultiplexGraphClusterer",
    "SammelbandBinding",
    "WEMIClusterBuilder",
    "WorkCluster",
    "create_corpus_clusterer",
    "extract_expression_partition_keys",
    "extract_manifestation_key",
    "infer_rda_content_type",
    "select_canonical_work_record",
]
