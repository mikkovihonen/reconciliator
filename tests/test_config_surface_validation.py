"""Unit tests for configuration surface consolidation, bounds checks, and permutation validation."""

import pytest
import torch

from reconciliator.clustering.correlation.pivot import CCPivotEngine
from reconciliator.clustering.density.dbscan import GPUDBSCANEngine
from reconciliator.clustering.hierarchical.dendrogram import GPUHierarchicalEngine
from reconciliator.clustering.mcl.engine import GPUMarkovClusterEngine
from reconciliator.config.app import AppConfig, ReconcileConfig
from reconciliator.config.infrastructure import ComputeConfig
from reconciliator.config.ingestion import IngestFilterConfig, IngestionConfig, OAIDefaultsConfig, OAIIngestConfig
from reconciliator.config.matching import (
    AgentClusterConfig,
    AgentClusterScoringWeights,
    AggregateClusteringConfig,
    BigClustConfig,
    BipartiteCoClusterConfig,
    CascadeRouterConfig,
    CorrelationClusteringConfig,
    DBSCANClusterConfig,
    HierarchicalClusterConfig,
    HybridClusterConfig,
    MarkovClusterConfig,
    WEMIClusterConfig,
    WEMIScoringWeightsWithAuthor,
    WEMIScoringWeightsWithoutAuthor,
)
from reconciliator.normalizers.record import clean_title


def test_cascade_router_threshold_order_validation():
    # Valid default ordering
    cfg = CascadeRouterConfig(
        low_confidence_threshold=0.55,
        uncertainty_band_min=0.60,
        uncertainty_band_max=0.88,
        high_confidence_threshold=0.90,
    )
    assert cfg.low_confidence_threshold == 0.55

    # Inverted low vs high
    with pytest.raises(ValueError, match="low_confidence_threshold .* must be <="):
        CascadeRouterConfig(
            low_confidence_threshold=0.95,
            high_confidence_threshold=0.90,
        )

    # Inverted uncertainty band
    with pytest.raises(ValueError, match="uncertainty_band_min .* must be <="):
        CascadeRouterConfig(
            uncertainty_band_min=0.85,
            uncertainty_band_max=0.70,
        )

    # Uncertainty band min lower than low confidence threshold
    with pytest.raises(ValueError, match="uncertainty_band_min .* must be >="):
        CascadeRouterConfig(
            low_confidence_threshold=0.65,
            uncertainty_band_min=0.50,
        )

    # Uncertainty band max higher than high confidence threshold
    with pytest.raises(ValueError, match="uncertainty_band_max .* must be <="):
        CascadeRouterConfig(
            high_confidence_threshold=0.80,
            uncertainty_band_max=0.85,
        )


def test_correlation_clustering_validation():
    # Valid config
    cfg = CorrelationClusteringConfig(
        algorithm="parallel_pivot",
        positive_threshold=0.70,
        max_parallel_pivots=128,
        device=None,
    )
    assert cfg.device == "auto"

    # Unsupported qp_relaxation
    with pytest.raises(ValueError, match="qp_relaxation.*is not supported"):
        CorrelationClusteringConfig(algorithm="qp_relaxation")

    # Engine asserts max_parallel_pivots >= 1
    with pytest.raises(ValueError, match="max_parallel_pivots must be at least 1"):
        CCPivotEngine(max_parallel_pivots=0)

    # Engine asserts valid algorithm
    with pytest.raises(ValueError, match="Unsupported algorithm"):
        CCPivotEngine(algorithm="invalid_algo")


def test_hierarchical_clustering_validation():
    # Ward with cosine raises ValueError in both config and engine
    with pytest.raises(ValueError, match="linkage='ward' requires metric='euclidean'"):
        HierarchicalClusterConfig(linkage="ward", metric="cosine")

    with pytest.raises(ValueError, match="linkage='ward' requires metric='euclidean'"):
        GPUHierarchicalEngine(linkage="ward", metric="cosine")

    # Ward with euclidean is valid
    cfg = HierarchicalClusterConfig(linkage="ward", metric="euclidean")
    assert cfg.linkage == "ward"
    assert cfg.metric == "euclidean"

    # Work distance threshold must be >= expression distance threshold
    with pytest.raises(ValueError, match="work_distance_threshold .* must be >="):
        HierarchicalClusterConfig(
            work_distance_threshold=0.10,
            expression_distance_threshold=0.25,
        )


def test_bigclust_clustering_validation():
    # min_work_clusters > max_work_clusters raises ValueError
    with pytest.raises(ValueError, match="min_work_clusters .* cannot exceed"):
        BigClustConfig(min_work_clusters=100, max_work_clusters=50)

    # device null normalizes to auto
    cfg = BigClustConfig(device=None)
    assert cfg.device == "auto"


def test_hybrid_clustering_validation():
    # twilight_lower >= fine_threshold raises ValueError
    with pytest.raises(ValueError, match="twilight_lower .* must be strictly less than"):
        HybridClusterConfig(twilight_lower=0.75, fine_threshold=0.70)


def test_dbscan_clustering_validation():
    # Engine validates eps > 0, min_samples >= 1, tile_size >= 1
    with pytest.raises(ValueError, match="eps must be positive"):
        GPUDBSCANEngine(eps=0.0)

    with pytest.raises(ValueError, match="min_samples must be at least 1"):
        GPUDBSCANEngine(min_samples=0)

    with pytest.raises(ValueError, match="tile_size must be at least 1"):
        GPUDBSCANEngine(tile_size=0)


def test_markov_clustering_validation():
    # Engine validates powers and thresholds
    with pytest.raises(ValueError, match="expansion_power must be >= 2"):
        GPUMarkovClusterEngine(expansion_power=1)

    with pytest.raises(ValueError, match="inflation_power must be > 1.0"):
        GPUMarkovClusterEngine(inflation_power=1.0)

    with pytest.raises(ValueError, match="pruning_threshold must be in"):
        GPUMarkovClusterEngine(pruning_threshold=1.0)


def test_scoring_weights_validation():
    # WEMI with author sum != 1.0
    with pytest.raises(ValueError, match="weights must sum to 1.0"):
        WEMIScoringWeightsWithAuthor(title=0.5, author=0.5, year=0.1, type=0.1)

    # WEMI without author sum != 1.0
    with pytest.raises(ValueError, match="weights must sum to 1.0"):
        WEMIScoringWeightsWithoutAuthor(title=0.6, year=0.1, type=0.1)

    # Agent cluster weights sum != 1.0
    with pytest.raises(ValueError, match="weights must sum to 1.0"):
        AgentClusterScoringWeights(name_weight=0.9, dates_weight=0.2)


def test_aggregate_clustering_thresholds():
    with pytest.raises(ValueError, match="jaccard_veto_threshold .* cannot exceed"):
        AggregateClusteringConfig(jaccard_merge_threshold=0.5, jaccard_veto_threshold=0.8)


def test_ingest_filter_year_range_validation():
    # from_year > until_year raises ValueError
    with pytest.raises(ValueError, match="from_year .* must be less than or equal to until_year"):
        IngestFilterConfig(from_year=2020, until_year=2010)

    # from_year <= until_year is valid
    cfg = IngestFilterConfig(from_year=1990, until_year=2020)
    assert cfg.from_year == 1990
    assert cfg.until_year == 2020


def test_ingestion_oai_defaults_inheritance():
    defaults = OAIDefaultsConfig(workers=8, slice_days=14, batch_size=2000)
    ingestion = IngestionConfig(
        oai_defaults=defaults,
        oai={
            "source_a": OAIIngestConfig(),
            "source_b": OAIIngestConfig(workers=2),  # explicitly specified
        },
    )
    # source_a inherits from oai_defaults
    assert ingestion.oai["source_a"].workers == 8
    assert ingestion.oai["source_a"].slice_days == 14
    assert ingestion.oai["source_a"].batch_size == 2000

    # source_b keeps explicitly set workers, inherits slice_days and batch_size
    assert ingestion.oai["source_b"].workers == 2
    assert ingestion.oai["source_b"].slice_days == 14
    assert ingestion.oai["source_b"].batch_size == 2000


def test_clean_title_iso_639_mapping():
    # Finnish title starting with Swedish article 'En'
    # When lang="fin", Swedish articles shouldn't be stripped
    title = "Enkelit ja demonit"
    cleaned = clean_title(title, lang="fin")
    assert cleaned == "Enkelit ja demonit"

    # When lang="fi", Swedish articles shouldn't be stripped
    cleaned = clean_title(title, lang="fi")
    assert cleaned == "Enkelit ja demonit"

    # When lang="swe", Swedish article 'En ' is stripped
    swedish_title = "En bok om kärlek"
    cleaned_swe = clean_title(swedish_title, lang="swe")
    assert cleaned_swe == "bok om kärlek"

    # When lang="sv", Swedish article 'En ' is stripped
    cleaned_sv = clean_title(swedish_title, lang="sv")
    assert cleaned_sv == "bok om kärlek"

    # When lang is unspecified (None), legacy fallback checks all articles
    cleaned_fallback = clean_title("The Great Gatsby", lang=None)
    assert cleaned_fallback == "Great Gatsby"


def test_reconcile_config_threshold_scaling_and_bounds():
    # 0.0-1.0 scale auto-scales to 0-100 scale for rapidfuzz
    from pathlib import Path
    cfg = ReconcileConfig(
        target_file=Path("dummy.xml"),
        agent_aligner_threshold=0.85,
        qualifier_similarity_threshold=0.80,
        distribution_match_threshold=0.80,
        surname_similarity_threshold=0.85,
    )
    assert cfg.agent_aligner_threshold == 85.0
    assert cfg.qualifier_similarity_threshold == 80.0
    assert cfg.distribution_match_threshold == 80.0
    assert cfg.surname_similarity_threshold == 85.0

    # Values > 100.0 or < 0.0 raise ValueError
    with pytest.raises(ValueError, match="agent_aligner_threshold must be in"):
        ReconcileConfig(target_file=Path("dummy.xml"), agent_aligner_threshold=105.0)


def test_device_propagation_to_clustering_hierarchy():
    from pathlib import Path
    app_cfg = AppConfig(
        compute=ComputeConfig(device="cuda"),
        reconcile=ReconcileConfig(
            target_file=Path("dummy.xml"),
            cluster=WEMIClusterConfig(
                device="auto",
                correlation=CorrelationClusteringConfig(device="auto"),
                hierarchical=HierarchicalClusterConfig(device="auto"),
                dbscan=DBSCANClusterConfig(device="auto"),
                mcl=MarkovClusterConfig(device="auto"),
                bipartite=BipartiteCoClusterConfig(device="auto"),
                bigclust=BigClustConfig(device="auto"),
            ),
        ),
    )
    # Propagated to cluster and sub-clusterers
    assert app_cfg.reconcile.cluster.device == "cuda"
    assert app_cfg.reconcile.cluster.correlation.device == "cuda"
    assert app_cfg.reconcile.cluster.hierarchical.device == "cuda"
    assert app_cfg.reconcile.cluster.dbscan.device == "cuda"
    assert app_cfg.reconcile.cluster.mcl.device == "cuda"
    assert app_cfg.reconcile.cluster.bipartite.device == "cuda"
    assert app_cfg.reconcile.cluster.bigclust.device == "cuda"
