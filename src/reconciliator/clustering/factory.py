"""Factory for WEMI corpus clusterers."""

from __future__ import annotations

from typing import Any, Optional

from reconciliator.authority.lifecycle import ClusterIdentityManager
from reconciliator.clustering.graph import MultiplexGraphClusterer
from reconciliator.clustering.protocol import CorpusClustererProtocol
from reconciliator.config import WEMIClusterConfig
from reconciliator.database import WEMIRepository
from reconciliator.embedding.protocol import VectorEmbedder
from reconciliator.reconciliation import WorkMatcher


def create_corpus_clusterer(
    config: Optional[WEMIClusterConfig] = None,
    corpus_repo: Optional[WEMIRepository] = None,
    wemi_repo: Optional[WEMIRepository] = None,
    matcher: Optional[WorkMatcher] = None,
    embedder: Optional[VectorEmbedder] = None,
    corpus_origins: Optional[list[str]] = None,
    identity_manager: Optional[ClusterIdentityManager] = None,
    evaluator: Optional[Any] = None,
    rerank_scorer: Optional[Any] = None,
) -> CorpusClustererProtocol:
    """Create a CorpusClustererProtocol instance based on configuration method.

    Args:
        config: WEMIClusterConfig specifying clustering method and parameters.
        corpus_repo: Staged corpus repository.
        wemi_repo: Consolidated WEMI repository (defaults to corpus_repo).
        matcher: Optional WorkMatcher instance for authority CURIE hoisting.
        embedder: Optional VectorEmbedder for dense embeddings.
        corpus_origins: Optional list of source origins to filter.
        identity_manager: Optional ClusterIdentityManager for persistent cluster identities.
        evaluator: Optional CandidateEvaluator instance.
        rerank_scorer: Optional RerankScorer instance for neural boundary adjudication.

    Returns:
        CorpusClustererProtocol instance (BigClustMultiplexClusterer or MultiplexGraphClusterer).
    """
    if corpus_repo is None:
        raise ValueError("corpus_repo is required to construct a corpus clusterer.")

    cfg = config or WEMIClusterConfig()
    effective_wemi = wemi_repo or corpus_repo
    actual_embedder = embedder or (getattr(matcher, "embedder", None) if matcher else None)
    id_mgr = identity_manager or ClusterIdentityManager(corpus_repo)

    method = getattr(cfg, "method", "multiplex_graph")

    if method == "correlation_pivot":
        from reconciliator.clustering.correlation import CorrelationCorpusClusterer

        return CorrelationCorpusClusterer(
            corpus_repo=corpus_repo,
            wemi_repo=effective_wemi,
            embedder=actual_embedder,
            config=getattr(cfg, "correlation", None),
            identity_manager=id_mgr,
        )

    if method == "hierarchical_wemi":
        from reconciliator.clustering.hierarchical import HierarchicalWEMIClusterer

        return HierarchicalWEMIClusterer(
            corpus_repo=corpus_repo,
            wemi_repo=effective_wemi,
            embedder=actual_embedder,
            config=getattr(cfg, "hierarchical", None),
            identity_manager=id_mgr,
        )

    if method == "gpu_dbscan":
        from reconciliator.clustering.density import DensityCorpusClusterer

        return DensityCorpusClusterer(
            corpus_repo=corpus_repo,
            wemi_repo=effective_wemi,
            embedder=actual_embedder,
            config=getattr(cfg, "dbscan", None),
            identity_manager=id_mgr,
        )

    if method == "gpu_mcl":
        from reconciliator.clustering.mcl import MarkovCorpusClusterer

        return MarkovCorpusClusterer(
            corpus_repo=corpus_repo,
            wemi_repo=effective_wemi,
            embedder=actual_embedder,
            config=getattr(cfg, "mcl", None),
            identity_manager=id_mgr,
        )

    if method == "bipartite_coclust":
        from reconciliator.clustering.bipartite import BipartiteCoClusterer

        return BipartiteCoClusterer(
            corpus_repo=corpus_repo,
            wemi_repo=effective_wemi,
            embedder=actual_embedder,
            config=getattr(cfg, "bipartite", None),
            identity_manager=id_mgr,
        )

    if method == "bigclust":
        from reconciliator.clustering.bigclust import BigClustMultiplexClusterer

        bc_cfg = getattr(cfg, "bigclust", None)
        max_w = getattr(bc_cfg, "max_work_clusters", 50) if bc_cfg else 50
        max_e = getattr(bc_cfg, "max_expression_clusters_per_work", 10) if bc_cfg else 10
        return BigClustMultiplexClusterer(
            corpus_repo=corpus_repo,
            embedder=actual_embedder,
            max_work_clusters=max_w,
            max_expression_clusters_per_work=max_e,
            identity_manager=id_mgr,
        )

    if method in ("hybrid", "hybrid_bigclust_graph"):
        from reconciliator.clustering.hybrid import HybridBigClustGraphClusterer

        bc_cfg = getattr(cfg, "bigclust", None)
        hy_cfg = getattr(cfg, "hybrid", None)
        max_w = getattr(bc_cfg, "max_work_clusters", 50) if bc_cfg else 50
        min_w = getattr(bc_cfg, "min_work_clusters", 2) if bc_cfg else 2
        max_e = getattr(bc_cfg, "max_expression_clusters_per_work", 10) if bc_cfg else 10
        boundary_margin = getattr(hy_cfg, "boundary_margin", 0.15) if hy_cfg else 0.15
        fine_thresh = getattr(hy_cfg, "fine_threshold", 0.70) if hy_cfg else 0.70
        twilight_lower = getattr(hy_cfg, "twilight_lower", 0.55) if hy_cfg else 0.55
        neural_thresh = getattr(hy_cfg, "neural_boundary_threshold", 0.75) if hy_cfg else 0.75
        return HybridBigClustGraphClusterer(
            corpus_repo=corpus_repo,
            wemi_repo=effective_wemi,
            embedder=actual_embedder,
            matcher=matcher,
            config=cfg,
            corpus_origins=corpus_origins,
            identity_manager=id_mgr,
            max_work_clusters=max_w,
            min_work_clusters=min_w,
            max_expression_clusters_per_work=max_e,
            boundary_margin=boundary_margin,
            fine_threshold=fine_thresh,
            twilight_lower=twilight_lower,
            neural_boundary_threshold=neural_thresh,
            evaluator=evaluator or (getattr(matcher, "evaluator", None) if matcher else None),
            rerank_scorer=rerank_scorer,
        )

    return MultiplexGraphClusterer(
        corpus_repo=corpus_repo,
        wemi_repo=effective_wemi,
        matcher=matcher,
        config=cfg,
        corpus_origins=corpus_origins,
        identity_manager=id_mgr,
    )
