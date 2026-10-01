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
    cluster_device_str = getattr(cfg, "device", "auto")

    if method in ("correlation_pivot", "correlation"):
        from reconciliator.clustering.correlation import CorrelationCorpusClusterer
        from reconciliator.utils.device import resolve_compute_device

        corr_cfg = getattr(cfg, "correlation", None)
        target_dev = resolve_compute_device(getattr(corr_cfg, "device", cluster_device_str) if corr_cfg and getattr(corr_cfg, "device", "auto") != "auto" else cluster_device_str)
        return CorrelationCorpusClusterer(
            corpus_repo=corpus_repo,
            wemi_repo=effective_wemi,
            embedder=actual_embedder,
            config=corr_cfg,
            identity_manager=id_mgr,
            device=target_dev,
        )

    if method in ("hierarchical_wemi", "hierarchical"):
        from reconciliator.clustering.hierarchical import HierarchicalWEMIClusterer
        from reconciliator.utils.device import resolve_compute_device

        h_cfg = getattr(cfg, "hierarchical", None)
        target_dev = resolve_compute_device(getattr(h_cfg, "device", cluster_device_str) if h_cfg and getattr(h_cfg, "device", "auto") != "auto" else cluster_device_str)
        return HierarchicalWEMIClusterer(
            corpus_repo=corpus_repo,
            wemi_repo=effective_wemi,
            embedder=actual_embedder,
            config=h_cfg,
            identity_manager=id_mgr,
            device=target_dev,
        )

    if method in ("gpu_dbscan", "dbscan"):
        from reconciliator.clustering.density import DensityCorpusClusterer
        from reconciliator.utils.device import resolve_compute_device

        db_cfg = getattr(cfg, "dbscan", None)
        target_dev = resolve_compute_device(getattr(db_cfg, "device", cluster_device_str) if db_cfg and getattr(db_cfg, "device", "auto") != "auto" else cluster_device_str)
        return DensityCorpusClusterer(
            corpus_repo=corpus_repo,
            wemi_repo=effective_wemi,
            embedder=actual_embedder,
            config=db_cfg,
            identity_manager=id_mgr,
            device=target_dev,
        )

    if method in ("gpu_mcl", "mcl"):
        from reconciliator.clustering.mcl import MarkovCorpusClusterer
        from reconciliator.utils.device import resolve_compute_device

        mcl_cfg = getattr(cfg, "mcl", None)
        target_dev = resolve_compute_device(getattr(mcl_cfg, "device", cluster_device_str) if mcl_cfg and getattr(mcl_cfg, "device", "auto") != "auto" else cluster_device_str)
        return MarkovCorpusClusterer(
            corpus_repo=corpus_repo,
            wemi_repo=effective_wemi,
            embedder=actual_embedder,
            config=mcl_cfg,
            identity_manager=id_mgr,
            device=target_dev,
        )

    if method in ("bipartite_coclust", "bipartite"):
        from reconciliator.clustering.bipartite import BipartiteCoClusterer
        from reconciliator.utils.device import resolve_compute_device

        bip_cfg = getattr(cfg, "bipartite", None)
        target_dev = resolve_compute_device(getattr(bip_cfg, "device", cluster_device_str) if bip_cfg and getattr(bip_cfg, "device", "auto") != "auto" else cluster_device_str)
        return BipartiteCoClusterer(
            corpus_repo=corpus_repo,
            wemi_repo=effective_wemi,
            embedder=actual_embedder,
            config=bip_cfg,
            identity_manager=id_mgr,
            device=target_dev,
        )

    if method == "bigclust":
        from reconciliator.clustering.bigclust import BigClustMultiplexClusterer
        from reconciliator.utils.device import resolve_compute_device

        bc_cfg = getattr(cfg, "bigclust", None)
        max_w = getattr(bc_cfg, "max_work_clusters", 50) if bc_cfg else 50
        max_e = getattr(bc_cfg, "max_expression_clusters_per_work", 10) if bc_cfg else 10
        sim_thresh = getattr(cfg, "similarity_threshold", 0.70)
        target_dev = resolve_compute_device(getattr(bc_cfg, "device", cluster_device_str) if bc_cfg and getattr(bc_cfg, "device", "auto") != "auto" else cluster_device_str)
        return BigClustMultiplexClusterer(
            corpus_repo=corpus_repo,
            embedder=actual_embedder,
            max_work_clusters=max_w,
            max_expression_clusters_per_work=max_e,
            similarity_threshold=sim_thresh,
            identity_manager=id_mgr,
            device=target_dev,
        )

    if method in ("hybrid", "hybrid_bigclust_graph"):
        from reconciliator.clustering.hybrid import HybridBigClustGraphClusterer
        from reconciliator.utils.device import resolve_compute_device

        bc_cfg = getattr(cfg, "bigclust", None)
        hy_cfg = getattr(cfg, "hybrid", None)
        max_w = getattr(bc_cfg, "max_work_clusters", 50) if bc_cfg else 50
        min_w = getattr(bc_cfg, "min_work_clusters", 2) if bc_cfg else 2
        max_e = getattr(bc_cfg, "max_expression_clusters_per_work", 10) if bc_cfg else 10
        boundary_margin = getattr(hy_cfg, "boundary_margin", 0.15) if hy_cfg else 0.15
        fine_thresh = getattr(hy_cfg, "fine_threshold", 0.70) if hy_cfg else 0.70
        twilight_lower = getattr(hy_cfg, "twilight_lower", 0.55) if hy_cfg else 0.55
        neural_thresh = getattr(hy_cfg, "neural_boundary_threshold", 0.75) if hy_cfg else 0.75
        target_dev = resolve_compute_device(getattr(bc_cfg, "device", cluster_device_str) if bc_cfg and getattr(bc_cfg, "device", "auto") != "auto" else cluster_device_str)
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
            device=str(target_dev),
        )

    return MultiplexGraphClusterer(
        corpus_repo=corpus_repo,
        wemi_repo=effective_wemi,
        matcher=matcher,
        config=cfg,
        corpus_origins=corpus_origins,
        identity_manager=id_mgr,
    )
