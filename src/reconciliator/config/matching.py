"""Matching, WEMI clustering, agent clustering, consolidation, reranking, and funnels models."""

from __future__ import annotations

from typing import Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator

from reconciliator.config.defaults import (
    get_bundled_agent_clustering_defaults,
    get_bundled_consolidation_defaults,
    get_bundled_funnels_defaults,
    get_bundled_llm_defaults,
    get_bundled_reconcile_defaults,
)
from reconciliator.funnels.spec import FunnelSpec as FunnelSpec

_RECONCILE_DEFAULTS = get_bundled_reconcile_defaults()
_CLUSTER_DEFAULTS = dict(_RECONCILE_DEFAULTS.get("cluster") or {})
_RERANK_DEFAULTS = dict(_RECONCILE_DEFAULTS.get("rerank") or {})
_CASCADE_DEFAULTS = dict(_RERANK_DEFAULTS.get("cascade") or {})
_SHORT_CIRCUIT_DEFAULTS = dict(_RERANK_DEFAULTS.get("short_circuit") or {})
_FTS_FALLBACK_DEFAULTS = dict(_RECONCILE_DEFAULTS.get("fts_fallback") or {})
_AGENT_CLUSTERING_DEFAULTS = get_bundled_agent_clustering_defaults()
_CONSOLIDATION_DEFAULTS = get_bundled_consolidation_defaults()
_FUNNELS_DEFAULTS = get_bundled_funnels_defaults()
_LLM_DEFAULTS = get_bundled_llm_defaults()

RerankEngineType = Literal["cascade", "llm_judge", "bm25", "cross_encoder", "fuzzy", "noop"]


class WorkConsolidationConfig(BaseModel):
    """Configuration for cross-source work identity consolidation."""

    enabled: bool = Field(
        default_factory=lambda: bool(_CONSOLIDATION_DEFAULTS.get("enabled", True))
    )
    use_wikidata_qid: bool = Field(
        default_factory=lambda: bool(_CONSOLIDATION_DEFAULTS.get("use_wikidata_qid", True))
    )
    use_shared_identifiers: bool = Field(
        default_factory=lambda: bool(_CONSOLIDATION_DEFAULTS.get("use_shared_identifiers", True))
    )
    use_metadata: bool = Field(
        default_factory=lambda: bool(_CONSOLIDATION_DEFAULTS.get("use_metadata", True))
    )
    strong_identifier_schemes: list[str] = Field(
        default_factory=lambda: list(_CONSOLIDATION_DEFAULTS.get("strong_identifier_schemes") or [])
    )
    metadata_threshold: float = Field(
        default_factory=lambda: float(_CONSOLIDATION_DEFAULTS.get("metadata_threshold", 0.93))
    )
    include_same_source: bool = Field(
        default_factory=lambda: bool(_CONSOLIDATION_DEFAULTS.get("include_same_source", False))
    )
    canonical_source_priority: list[str] = Field(
        default_factory=lambda: list(_CONSOLIDATION_DEFAULTS.get("canonical_source_priority") or [])
    )


class ReconcileFTSFallbackConfig(BaseModel):
    """Configuration for WorkMatcher PostgreSQL full-text search fallback during reconciliation."""

    enabled: bool = Field(
        default_factory=lambda: bool(_FTS_FALLBACK_DEFAULTS.get("enabled", False))
    )
    limit: int = Field(default_factory=lambda: int(_FTS_FALLBACK_DEFAULTS.get("limit", 5)))
    confidence_score: float = Field(
        default_factory=lambda: float(_FTS_FALLBACK_DEFAULTS.get("confidence_score", 0.80))
    )

    @model_validator(mode="after")
    def validate_params(self) -> ReconcileFTSFallbackConfig:
        if not (0.0 <= self.confidence_score <= 1.0):
            raise ValueError(f"confidence_score must be in [0.0, 1.0], got {self.confidence_score}")
        if self.limit <= 0:
            raise ValueError(f"limit must be a positive integer, got {self.limit}")
        return self


class MultiplexClusterThresholds(BaseModel):
    """Layer-specific cosine similarity thresholds for multiplex graph clustering."""

    work: float = 0.85
    expression: float = 0.88
    manifestation: float = 0.92

    @model_validator(mode="after")
    def validate_thresholds(self) -> MultiplexClusterThresholds:
        for layer, val in (
            ("work", self.work),
            ("expression", self.expression),
            ("manifestation", self.manifestation),
        ):
            if not (0.0 <= val <= 1.0):
                raise ValueError(f"{layer} cluster threshold must be in [0.0, 1.0], got {val}")
        return self


class WEMIScoringWeightsWithAuthor(BaseModel):
    title: float = Field(
        default_factory=lambda: float(
            _CLUSTER_DEFAULTS.get("scoring", {}).get("with_author", {}).get("title", 0.45)
        ),
        ge=0.0,
    )
    author: float = Field(
        default_factory=lambda: float(
            _CLUSTER_DEFAULTS.get("scoring", {}).get("with_author", {}).get("author", 0.35)
        ),
        ge=0.0,
    )
    year: float = Field(
        default_factory=lambda: float(
            _CLUSTER_DEFAULTS.get("scoring", {}).get("with_author", {}).get("year", 0.10)
        ),
        ge=0.0,
    )
    type: float = Field(
        default_factory=lambda: float(
            _CLUSTER_DEFAULTS.get("scoring", {}).get("with_author", {}).get("type", 0.10)
        ),
        ge=0.0,
    )

    @model_validator(mode="after")
    def validate_sum(self) -> WEMIScoringWeightsWithAuthor:
        total = self.title + self.author + self.year + self.type
        if round(total, 4) != 1.0:
            raise ValueError(f"WEMIScoringWeightsWithAuthor weights must sum to 1.0, got {total}")
        return self


class WEMIScoringWeightsWithoutAuthor(BaseModel):
    title: float = Field(
        default_factory=lambda: float(
            _CLUSTER_DEFAULTS.get("scoring", {}).get("without_author", {}).get("title", 0.70)
        ),
        ge=0.0,
    )
    year: float = Field(
        default_factory=lambda: float(
            _CLUSTER_DEFAULTS.get("scoring", {}).get("without_author", {}).get("year", 0.15)
        ),
        ge=0.0,
    )
    type: float = Field(
        default_factory=lambda: float(
            _CLUSTER_DEFAULTS.get("scoring", {}).get("without_author", {}).get("type", 0.15)
        ),
        ge=0.0,
    )

    @model_validator(mode="after")
    def validate_sum(self) -> WEMIScoringWeightsWithoutAuthor:
        total = self.title + self.year + self.type
        if round(total, 4) != 1.0:
            raise ValueError(f"WEMIScoringWeightsWithoutAuthor weights must sum to 1.0, got {total}")
        return self


class WEMIScoringConfig(BaseModel):
    with_author: WEMIScoringWeightsWithAuthor = Field(
        default_factory=lambda: WEMIScoringWeightsWithAuthor(
            **_CLUSTER_DEFAULTS.get("scoring", {}).get("with_author", {})
        )
    )
    without_author: WEMIScoringWeightsWithoutAuthor = Field(
        default_factory=lambda: WEMIScoringWeightsWithoutAuthor(
            **_CLUSTER_DEFAULTS.get("scoring", {}).get("without_author", {})
        )
    )
    author_missing_similarity: float = Field(
        default_factory=lambda: float(
            _CLUSTER_DEFAULTS.get("scoring", {}).get("author_missing_similarity", 0.60)
        ),
        ge=0.0,
        le=1.0,
    )
    year_missing_similarity: float = Field(
        default_factory=lambda: float(
            _CLUSTER_DEFAULTS.get("scoring", {}).get("year_missing_similarity", 0.80)
        ),
        ge=0.0,
        le=1.0,
    )
    year_max_difference: float = Field(
        default_factory=lambda: float(
            _CLUSTER_DEFAULTS.get("scoring", {}).get("year_max_difference", 20.0)
        ),
        ge=0.0,
    )
    cross_format_type_similarity: float = Field(
        default_factory=lambda: float(
            _CLUSTER_DEFAULTS.get("scoring", {}).get("cross_format_type_similarity", 0.95)
        ),
        ge=0.0,
        le=1.0,
    )
    default_type_similarity: float = Field(
        default_factory=lambda: float(
            _CLUSTER_DEFAULTS.get("scoring", {}).get("default_type_similarity", 0.50)
        ),
        ge=0.0,
        le=1.0,
    )


class MusicScoreConfig(BaseModel):
    terms: list[str] = Field(
        default_factory=lambda: list(_CLUSTER_DEFAULTS.get("music_score", {}).get("terms") or [])
    )
    standard_key_terms: list[str] = Field(
        default_factory=lambda: list(
            _CLUSTER_DEFAULTS.get("music_score", {}).get("standard_key_terms") or []
        )
    )


class AggregateClusteringConfig(BaseModel):
    """Configuration for constituent-first multiplex clustering of aggregate works."""

    enabled: bool = Field(
        default_factory=lambda: bool(_CLUSTER_DEFAULTS.get("aggregates", {}).get("enabled", True))
    )
    min_constituents_for_overlap: int = Field(
        default_factory=lambda: int(
            _CLUSTER_DEFAULTS.get("aggregates", {}).get("min_constituents_for_overlap", 3)
        ),
        ge=1,
    )
    jaccard_merge_threshold: float = Field(
        default_factory=lambda: float(
            _CLUSTER_DEFAULTS.get("aggregates", {}).get("jaccard_merge_threshold", 0.80)
        ),
        ge=0.0,
        le=1.0,
    )
    jaccard_veto_threshold: float = Field(
        default_factory=lambda: float(
            _CLUSTER_DEFAULTS.get("aggregates", {}).get("jaccard_veto_threshold", 0.30)
        ),
        ge=0.0,
        le=1.0,
    )
    merge_bonus_score: float = Field(
        default_factory=lambda: float(
            _CLUSTER_DEFAULTS.get("aggregates", {}).get("merge_bonus_score", 0.95)
        ),
        ge=0.0,
        le=1.0,
    )
    sequence_alignment: bool = Field(
        default_factory=lambda: bool(
            _CLUSTER_DEFAULTS.get("aggregates", {}).get("sequence_alignment", True)
        )
    )
    lcs_threshold: float = Field(
        default_factory=lambda: float(
            _CLUSTER_DEFAULTS.get("aggregates", {}).get("lcs_threshold", 0.70)
        ),
        ge=0.0,
        le=1.0,
    )
    blocking_family: str = Field(
        default_factory=lambda: str(
            _CLUSTER_DEFAULTS.get("aggregates", {}).get("blocking_family", "constituent_work")
        )
    )

    @model_validator(mode="after")
    def validate_thresholds(self) -> AggregateClusteringConfig:
        if self.jaccard_veto_threshold > self.jaccard_merge_threshold:
            raise ValueError(
                f"jaccard_veto_threshold ({self.jaccard_veto_threshold}) cannot exceed "
                f"jaccard_merge_threshold ({self.jaccard_merge_threshold})."
            )
        return self


_CORRELATION_DEFAULTS = dict(_CLUSTER_DEFAULTS.get("correlation") or {})
_HIERARCHICAL_DEFAULTS = dict(_CLUSTER_DEFAULTS.get("hierarchical") or {})
_DBSCAN_DEFAULTS = dict(_CLUSTER_DEFAULTS.get("dbscan") or {})
_MCL_DEFAULTS = dict(_CLUSTER_DEFAULTS.get("mcl") or {})
_BIPARTITE_DEFAULTS = dict(_CLUSTER_DEFAULTS.get("bipartite") or {})
_BIGCLUST_DEFAULTS = dict(_CLUSTER_DEFAULTS.get("bigclust") or {})
_HYBRID_DEFAULTS = dict(_CLUSTER_DEFAULTS.get("hybrid") or {})


class BigClustConfig(BaseModel):
    """Configuration for BigClust continuous nonsmooth clustering on GPU."""

    enabled: bool = Field(
        default_factory=lambda: bool(_BIGCLUST_DEFAULTS.get("enabled", True))
    )
    max_work_clusters: int = Field(
        default_factory=lambda: int(_BIGCLUST_DEFAULTS.get("max_work_clusters", 50)),
        ge=1,
    )
    min_work_clusters: int = Field(
        default_factory=lambda: int(_BIGCLUST_DEFAULTS.get("min_work_clusters", 2)),
        ge=1,
    )
    max_expression_clusters_per_work: int = Field(
        default_factory=lambda: int(_BIGCLUST_DEFAULTS.get("max_expression_clusters_per_work", 10)),
        ge=1,
    )
    batch_size: int = Field(
        default_factory=lambda: int(_BIGCLUST_DEFAULTS.get("batch_size", 5000)),
        ge=1,
    )
    max_candidate_points: int = Field(
        default_factory=lambda: int(_BIGCLUST_DEFAULTS.get("max_candidate_points", 20)),
        ge=1,
    )
    bundle_size: int = Field(
        default_factory=lambda: int(_BIGCLUST_DEFAULTS.get("bundle_size", 7)),
        ge=1,
    )
    max_iter: int = Field(
        default_factory=lambda: int(_BIGCLUST_DEFAULTS.get("max_iter", 300)),
        ge=1,
    )
    device: Optional[Literal["auto", "cuda", "mps", "cpu"]] = Field(
        default_factory=lambda: str(_BIGCLUST_DEFAULTS.get("device", "auto"))  # type: ignore[return-value]
    )

    @model_validator(mode="after")
    def validate_cluster_counts(self) -> BigClustConfig:
        if self.device is None:
            self.device = "auto"
        if self.min_work_clusters > self.max_work_clusters:
            raise ValueError(
                f"min_work_clusters ({self.min_work_clusters}) cannot exceed "
                f"max_work_clusters ({self.max_work_clusters})."
            )
        return self


class HybridClusterConfig(BaseModel):
    """Configuration for hybrid BigClust coarse macro-partitioning + graph fine-reranking."""

    boundary_margin: float = Field(
        default_factory=lambda: float(_HYBRID_DEFAULTS.get("boundary_margin", 0.15)),
        ge=0.0,
    )
    fine_threshold: float = Field(
        default_factory=lambda: float(_HYBRID_DEFAULTS.get("fine_threshold", 0.70)),
        ge=0.0,
        le=1.0,
    )
    max_candidates_per_boundary: int = Field(
        default_factory=lambda: int(_HYBRID_DEFAULTS.get("max_candidates_per_boundary", 20)),
        ge=1,
    )
    twilight_lower: float = Field(
        default_factory=lambda: float(_HYBRID_DEFAULTS.get("twilight_lower", 0.55)),
        ge=0.0,
        le=1.0,
    )
    neural_boundary_threshold: float = Field(
        default_factory=lambda: float(_HYBRID_DEFAULTS.get("neural_boundary_threshold", 0.75)),
        ge=0.0,
        le=1.0,
    )

    @model_validator(mode="after")
    def validate_twilight_bounds(self) -> HybridClusterConfig:
        if self.twilight_lower >= self.fine_threshold:
            raise ValueError(
                f"twilight_lower ({self.twilight_lower}) must be strictly less than "
                f"fine_threshold ({self.fine_threshold})."
            )
        return self


class CorrelationClusteringConfig(BaseModel):
    """Configuration for GPU-accelerated Correlation Clustering (CC-Pivot)."""

    enabled: bool = Field(
        default_factory=lambda: bool(_CORRELATION_DEFAULTS.get("enabled", True))
    )
    algorithm: Literal["parallel_pivot", "serial_pivot", "qp_relaxation"] = Field(
        default_factory=lambda: str(_CORRELATION_DEFAULTS.get("algorithm", "parallel_pivot"))  # type: ignore[return-value]
    )
    positive_threshold: float = Field(
        default_factory=lambda: float(_CORRELATION_DEFAULTS.get("positive_threshold", 0.70)),
        ge=0.0,
        le=1.0,
    )
    max_parallel_pivots: int = Field(
        default_factory=lambda: int(_CORRELATION_DEFAULTS.get("max_parallel_pivots", 256)),
        ge=1,
    )
    device: Optional[Literal["auto", "cuda", "mps", "cpu"]] = Field(
        default_factory=lambda: str(_CORRELATION_DEFAULTS.get("device", "auto"))  # type: ignore[return-value]
    )

    @model_validator(mode="after")
    def validate_algorithm(self) -> CorrelationClusteringConfig:
        if self.device is None:
            self.device = "auto"
        if self.algorithm == "qp_relaxation":
            raise ValueError(
                "Correlation clustering algorithm 'qp_relaxation' is not supported in CCPivotEngine. "
                "Supported algorithms: 'parallel_pivot', 'serial_pivot'."
            )
        return self


class HierarchicalClusterConfig(BaseModel):
    """Configuration for GPU-accelerated multi-tier WEMI HAC."""

    linkage: Literal["average", "single", "complete", "ward"] = Field(
        default_factory=lambda: str(_HIERARCHICAL_DEFAULTS.get("linkage", "average"))  # type: ignore[return-value]
    )
    work_distance_threshold: float = Field(
        default_factory=lambda: float(_HIERARCHICAL_DEFAULTS.get("work_distance_threshold", 0.35)),
        ge=0.0,
    )
    expression_distance_threshold: float = Field(
        default_factory=lambda: float(_HIERARCHICAL_DEFAULTS.get("expression_distance_threshold", 0.15)),
        ge=0.0,
    )
    manifestation_distance_threshold: float = Field(
        default_factory=lambda: float(_HIERARCHICAL_DEFAULTS.get("manifestation_distance_threshold", 0.05)),
        ge=0.0,
    )
    metric: Literal["cosine", "euclidean"] = Field(
        default_factory=lambda: str(_HIERARCHICAL_DEFAULTS.get("metric", "cosine"))  # type: ignore[return-value]
    )
    batch_size: int = Field(
        default_factory=lambda: int(_HIERARCHICAL_DEFAULTS.get("batch_size", 4096)),
        ge=1,
    )
    device: Optional[Literal["auto", "cuda", "mps", "cpu"]] = Field(
        default_factory=lambda: str(_HIERARCHICAL_DEFAULTS.get("device", "auto"))  # type: ignore[return-value]
    )

    @model_validator(mode="after")
    def validate_hierarchy_and_metric(self) -> HierarchicalClusterConfig:
        if self.device is None:
            self.device = "auto"
        if self.linkage == "ward" and self.metric != "euclidean":
            raise ValueError(
                f"Hierarchical clustering with linkage='ward' requires metric='euclidean', got '{self.metric}'."
            )
        if self.work_distance_threshold < self.expression_distance_threshold:
            raise ValueError(
                f"work_distance_threshold ({self.work_distance_threshold}) must be >= "
                f"expression_distance_threshold ({self.expression_distance_threshold}) to preserve WEMI hierarchy."
            )
        return self


class DBSCANClusterConfig(BaseModel):
    """Configuration for GPU-accelerated density-based clustering."""

    eps: float = Field(
        default_factory=lambda: float(_DBSCAN_DEFAULTS.get("eps", 0.25)),
        gt=0.0,
    )
    min_samples: int = Field(
        default_factory=lambda: int(_DBSCAN_DEFAULTS.get("min_samples", 2)),
        ge=1,
    )
    metric: Literal["cosine", "euclidean"] = Field(
        default_factory=lambda: str(_DBSCAN_DEFAULTS.get("metric", "cosine"))  # type: ignore[return-value]
    )
    noise_strategy: Literal["singleton", "isolate", "discard"] = Field(
        default_factory=lambda: str(_DBSCAN_DEFAULTS.get("noise_strategy", "singleton"))  # type: ignore[return-value]
    )
    tile_size: int = Field(
        default_factory=lambda: int(_DBSCAN_DEFAULTS.get("tile_size", 4096)),
        ge=1,
    )
    device: Optional[Literal["auto", "cuda", "mps", "cpu"]] = Field(
        default_factory=lambda: str(_DBSCAN_DEFAULTS.get("device", "auto"))  # type: ignore[return-value]
    )

    @model_validator(mode="after")
    def validate_device(self) -> DBSCANClusterConfig:
        if self.device is None:
            self.device = "auto"
        return self


class MarkovClusterConfig(BaseModel):
    """Configuration for GPU-accelerated Markov Cluster Algorithm (MCL)."""

    expansion_power: int = Field(
        default_factory=lambda: int(_MCL_DEFAULTS.get("expansion_power", 2)),
        ge=2,
    )
    inflation_power: float = Field(
        default_factory=lambda: float(_MCL_DEFAULTS.get("inflation_power", 2.0)),
        gt=1.0,
    )
    max_iterations: int = Field(
        default_factory=lambda: int(_MCL_DEFAULTS.get("max_iterations", 30)),
        ge=1,
    )
    convergence_tol: float = Field(
        default_factory=lambda: float(_MCL_DEFAULTS.get("convergence_tol", 1e-5)),
        gt=0.0,
    )
    pruning_threshold: float = Field(
        default_factory=lambda: float(_MCL_DEFAULTS.get("pruning_threshold", 1e-4)),
        ge=0.0,
        lt=1.0,
    )
    device: Optional[Literal["auto", "cuda", "mps", "cpu"]] = Field(
        default_factory=lambda: str(_MCL_DEFAULTS.get("device", "auto"))  # type: ignore[return-value]
    )

    @model_validator(mode="after")
    def validate_device(self) -> MarkovClusterConfig:
        if self.device is None:
            self.device = "auto"
        return self


class BipartiteCoClusterConfig(BaseModel):
    """Configuration for joint Work-Agent bipartite spectral co-clustering."""

    latent_dimensions: int = Field(
        default_factory=lambda: int(_BIPARTITE_DEFAULTS.get("latent_dimensions", 32)),
        ge=1,
    )
    work_k: int = Field(
        default_factory=lambda: int(_BIPARTITE_DEFAULTS.get("work_k", 50)),
        ge=1,
    )
    agent_k: int = Field(
        default_factory=lambda: int(_BIPARTITE_DEFAULTS.get("agent_k", 50)),
        ge=1,
    )
    iterations: int = Field(
        default_factory=lambda: int(_BIPARTITE_DEFAULTS.get("iterations", 100)),
        ge=1,
    )
    device: Optional[Literal["auto", "cuda", "mps", "cpu"]] = Field(
        default_factory=lambda: str(_BIPARTITE_DEFAULTS.get("device", "auto"))  # type: ignore[return-value]
    )

    @model_validator(mode="after")
    def validate_device(self) -> BipartiteCoClusterConfig:
        if self.device is None:
            self.device = "auto"
        return self


class WEMIClusterConfig(BaseModel):
    """Configuration for multiplex WEMI graph clustering and blocking."""

    enabled: bool = Field(default_factory=lambda: bool(_CLUSTER_DEFAULTS.get("enabled", True)))
    method: Literal[
        "multiplex_graph",
        "connected_components",
        "dbscan",
        "hierarchical",
        "bigclust",
        "hybrid_bigclust_graph",
        "hybrid",
        "correlation_pivot",
        "hierarchical_wemi",
        "gpu_dbscan",
        "gpu_mcl",
        "bipartite_coclust",
    ] = Field(
        default_factory=lambda: str(_CLUSTER_DEFAULTS.get("method", "multiplex_graph"))  # type: ignore[return-value]
    )
    device: Optional[Literal["auto", "cuda", "mps", "cpu"]] = Field(
        default_factory=lambda: str(_CLUSTER_DEFAULTS.get("device", "auto"))  # type: ignore[return-value]
    )
    bigclust: BigClustConfig = Field(default_factory=BigClustConfig)
    hybrid: HybridClusterConfig = Field(default_factory=HybridClusterConfig)
    correlation: CorrelationClusteringConfig = Field(default_factory=CorrelationClusteringConfig)
    hierarchical: HierarchicalClusterConfig = Field(default_factory=HierarchicalClusterConfig)
    dbscan: DBSCANClusterConfig = Field(default_factory=DBSCANClusterConfig)
    mcl: MarkovClusterConfig = Field(default_factory=MarkovClusterConfig)
    bipartite: BipartiteCoClusterConfig = Field(default_factory=BipartiteCoClusterConfig)
    similarity_threshold: float = Field(
        default_factory=lambda: float(_CLUSTER_DEFAULTS.get("similarity_threshold", 0.85)),
        ge=0.0,
        le=1.0,
    )
    max_cluster_size: int = Field(
        default_factory=lambda: int(_CLUSTER_DEFAULTS.get("max_cluster_size", 500)),
        ge=1,
    )
    blocking_families: Union[list[str], Literal["all"]] = Field(
        default_factory=lambda: [
            "work_blocking_key",
            "series_key",
            "music_work_key",
            "standard_key",
        ]
    )
    thresholds: MultiplexClusterThresholds = Field(default_factory=MultiplexClusterThresholds)
    message_passing: bool = Field(
        default_factory=lambda: bool(_CLUSTER_DEFAULTS.get("message_passing", True))
    )
    reconcile_items: bool = Field(
        default_factory=lambda: bool(_CLUSTER_DEFAULTS.get("reconcile_items", False))
    )
    aggregates: AggregateClusteringConfig = Field(default_factory=AggregateClusteringConfig)
    scoring: WEMIScoringConfig = Field(
        default_factory=lambda: WEMIScoringConfig(**_CLUSTER_DEFAULTS.get("scoring", {}))
    )
    music_score: MusicScoreConfig = Field(
        default_factory=lambda: MusicScoreConfig(**_CLUSTER_DEFAULTS.get("music_score", {}))
    )
    common_schemes: list[str] = Field(
        default_factory=lambda: list(_CLUSTER_DEFAULTS.get("common_schemes") or [])
    )
    domain_key_types: list[str] = Field(
        default_factory=lambda: list(_CLUSTER_DEFAULTS.get("domain_key_types") or [])
    )

    @model_validator(mode="after")
    def sync_device_to_subclusters(self) -> WEMIClusterConfig:
        if self.device is None:
            self.device = "auto"
        if self.device != "auto":
            for sub in (
                self.bigclust,
                self.correlation,
                self.hierarchical,
                self.dbscan,
                self.mcl,
                self.bipartite,
            ):
                if getattr(sub, "device", None) in ("auto", None):
                    sub.device = self.device
        return self


class AgentClusterScoringWeights(BaseModel):
    """Weights for multi-feature agent cluster scoring."""

    name_weight: float = Field(
        default_factory=lambda: float(
            _AGENT_CLUSTERING_DEFAULTS.get("scoring_weights", {}).get("name_weight", 0.40)
        ),
        ge=0.0,
    )
    dates_weight: float = Field(
        default_factory=lambda: float(
            _AGENT_CLUSTERING_DEFAULTS.get("scoring_weights", {}).get("dates_weight", 0.20)
        ),
        ge=0.0,
    )
    work_cluster_weight: float = Field(
        default_factory=lambda: float(
            _AGENT_CLUSTERING_DEFAULTS.get("scoring_weights", {}).get("work_cluster_weight", 0.15)
        ),
        ge=0.0,
    )
    coauthor_weight: float = Field(
        default_factory=lambda: float(
            _AGENT_CLUSTERING_DEFAULTS.get("scoring_weights", {}).get("coauthor_weight", 0.10)
        ),
        ge=0.0,
    )
    affiliation_weight: float = Field(
        default_factory=lambda: float(
            _AGENT_CLUSTERING_DEFAULTS.get("scoring_weights", {}).get("affiliation_weight", 0.05)
        ),
        ge=0.0,
    )
    concept_weight: float = Field(
        default_factory=lambda: float(
            _AGENT_CLUSTERING_DEFAULTS.get("scoring_weights", {}).get("concept_weight", 0.05)
        ),
        ge=0.0,
    )
    qualifier_weight: float = Field(
        default_factory=lambda: float(
            _AGENT_CLUSTERING_DEFAULTS.get("scoring_weights", {}).get("qualifier_weight", 0.05)
        ),
        ge=0.0,
    )

    @model_validator(mode="after")
    def validate_sum(self) -> AgentClusterScoringWeights:
        total = (
            self.name_weight
            + self.dates_weight
            + self.work_cluster_weight
            + self.coauthor_weight
            + self.affiliation_weight
            + self.concept_weight
            + self.qualifier_weight
        )
        if round(total, 4) != 1.0:
            raise ValueError(f"AgentClusterScoringWeights weights must sum to 1.0, got {total}")
        return self


class AgentClusterConfig(BaseModel):
    """Configuration for cross-source agent entity clustering."""

    enabled: bool = Field(
        default_factory=lambda: bool(_AGENT_CLUSTERING_DEFAULTS.get("enabled", True))
    )
    similarity_threshold: float = Field(
        default_factory=lambda: float(_AGENT_CLUSTERING_DEFAULTS.get("similarity_threshold", 0.85)),
        ge=0.0,
        le=1.0,
    )
    date_tolerance_years: int = Field(
        default_factory=lambda: int(_AGENT_CLUSTERING_DEFAULTS.get("date_tolerance_years", 1)),
        ge=0,
    )
    major_date_discrepancy_veto_years: int = Field(
        default_factory=lambda: int(
            _AGENT_CLUSTERING_DEFAULTS.get("major_date_discrepancy_veto_years", 2)
        ),
        ge=0,
    )
    require_corroboration_for_date_discrepancy: bool = Field(
        default_factory=lambda: bool(
            _AGENT_CLUSTERING_DEFAULTS.get("require_corroboration_for_date_discrepancy", True)
        )
    )
    min_career_overlap_tolerance_years: int = Field(
        default_factory=lambda: int(
            _AGENT_CLUSTERING_DEFAULTS.get("min_career_overlap_tolerance_years", 40)
        ),
        ge=0,
    )
    batch_size: int = Field(
        default_factory=lambda: int(_AGENT_CLUSTERING_DEFAULTS.get("batch_size", 1000)),
        ge=1,
    )
    token_matching_algorithm: str = Field(
        default_factory=lambda: str(
            _AGENT_CLUSTERING_DEFAULTS.get("token_matching_algorithm", "token_set_ratio")
        )
    )
    affiliation_fuzzy_threshold: float = Field(
        default_factory=lambda: float(
            _AGENT_CLUSTERING_DEFAULTS.get("affiliation_fuzzy_threshold", 80.0)
        ),
        ge=0.0,
        le=100.0,
    )
    affiliation_fuzzy_score: float = Field(
        default_factory=lambda: float(
            _AGENT_CLUSTERING_DEFAULTS.get("affiliation_fuzzy_score", 0.85)
        ),
        ge=0.0,
        le=1.0,
    )
    enable_hierarchical_concepts: bool = Field(
        default_factory=lambda: bool(
            _AGENT_CLUSTERING_DEFAULTS.get("enable_hierarchical_concepts", True)
        )
    )
    concept_similarity_weight: float = Field(
        default_factory=lambda: float(
            _AGENT_CLUSTERING_DEFAULTS.get("concept_similarity_weight", 0.85)
        ),
        ge=0.0,
        le=1.0,
    )
    enable_semantic_disambiguation: bool = Field(
        default_factory=lambda: bool(
            _AGENT_CLUSTERING_DEFAULTS.get("enable_semantic_disambiguation", True)
        )
    )
    semantic_homonym_veto_threshold: float = Field(
        default_factory=lambda: float(
            _AGENT_CLUSTERING_DEFAULTS.get("semantic_homonym_veto_threshold", 0.40)
        ),
        ge=0.0,
        le=1.0,
    )
    semantic_boost_threshold: float = Field(
        default_factory=lambda: float(
            _AGENT_CLUSTERING_DEFAULTS.get("semantic_boost_threshold", 0.85)
        ),
        ge=0.0,
        le=1.0,
    )
    max_blocking_bucket_size: int = Field(
        default_factory=lambda: int(_AGENT_CLUSTERING_DEFAULTS.get("max_blocking_bucket_size", 200)),
        ge=1,
    )
    date_tolerance_score: float = Field(
        default_factory=lambda: float(_AGENT_CLUSTERING_DEFAULTS.get("date_tolerance_score", 0.7)),
        ge=0.0,
        le=1.0,
    )
    date_missing_score: float = Field(
        default_factory=lambda: float(_AGENT_CLUSTERING_DEFAULTS.get("date_missing_score", 0.5)),
        ge=0.0,
        le=1.0,
    )
    source_authority_precedence: list[str] = Field(
        default_factory=lambda: list(
            _AGENT_CLUSTERING_DEFAULTS.get("source_authority_precedence") or []
        )
    )
    override_identifiers: list[str] = Field(
        default_factory=lambda: list(_AGENT_CLUSTERING_DEFAULTS.get("override_identifiers") or [])
    )
    blocking_keys: list[str] = Field(
        default_factory=lambda: list(_AGENT_CLUSTERING_DEFAULTS.get("blocking_keys") or [])
    )
    scoring_weights: AgentClusterScoringWeights = Field(
        default_factory=lambda: AgentClusterScoringWeights(
            **_AGENT_CLUSTERING_DEFAULTS.get("scoring_weights", {})
        )
    )
    coalesce_pseudonyms: bool = Field(
        default_factory=lambda: bool(_AGENT_CLUSTERING_DEFAULTS.get("coalesce_pseudonyms", False))
    )
    coalesce_corporate_lineage: bool = Field(
        default_factory=lambda: bool(
            _AGENT_CLUSTERING_DEFAULTS.get("coalesce_corporate_lineage", False)
        )
    )
    link_cluster_relationships: bool = Field(
        default_factory=lambda: bool(
            _AGENT_CLUSTERING_DEFAULTS.get("link_cluster_relationships", True)
        )
    )
    relationship_corroboration_score: float = Field(
        default_factory=lambda: float(
            _AGENT_CLUSTERING_DEFAULTS.get("relationship_corroboration_score", 0.90)
        ),
        ge=0.0,
        le=1.0,
    )


class ShortCircuitConfig(BaseModel):
    """High-confidence dense fast-path shortcut configuration."""

    enabled: bool = Field(
        default_factory=lambda: bool(_SHORT_CIRCUIT_DEFAULTS.get("enabled", False))
    )
    threshold: float = Field(
        default_factory=lambda: float(_SHORT_CIRCUIT_DEFAULTS.get("threshold", 0.98))
    )

    @model_validator(mode="after")
    def validate_threshold(self) -> ShortCircuitConfig:
        if not (0.0 <= self.threshold <= 1.0):
            raise ValueError(
                f"short_circuit threshold must be between 0.0 and 1.0, got {self.threshold}"
            )
        return self


class LLMJudgeConfig(BaseModel):
    """Configuration for autoregressive LLM Judge with KV-branching."""

    backend: Literal["auto", "torch", "mlx"] = Field(
        default_factory=lambda: _LLM_DEFAULTS.get("backend", "auto")
    )
    model_name: str = Field(
        default_factory=lambda: str(_LLM_DEFAULTS.get("model_name", "Qwen/Qwen2.5-7B-Instruct"))
    )
    quantization: Optional[Literal["4bit", "8bit"]] = Field(
        default_factory=lambda: _LLM_DEFAULTS.get("quantization", "8bit")
    )
    device: Literal["auto", "cuda", "mps", "cpu"] = Field(
        default_factory=lambda: _LLM_DEFAULTS.get("device", "auto")
    )
    torch_dtype: Literal["auto", "float16", "bfloat16", "float32"] = Field(
        default_factory=lambda: _LLM_DEFAULTS.get("torch_dtype", "auto")
    )
    attn_implementation: Literal["auto", "sdpa", "flash_attention_2"] = Field(
        default_factory=lambda: _LLM_DEFAULTS.get("attn_implementation", "auto")
    )
    pooling_strategy: Literal["last_token", "mean"] = Field(
        default_factory=lambda: _LLM_DEFAULTS.get("pooling_strategy", "last_token")
    )
    batch_size: int = Field(default_factory=lambda: int(_LLM_DEFAULTS.get("batch_size", 16)))
    positive_token: str = Field(
        default_factory=lambda: str(_LLM_DEFAULTS.get("positive_token", "YES"))
    )
    negative_token: str = Field(
        default_factory=lambda: str(_LLM_DEFAULTS.get("negative_token", "NO"))
    )
    target_template: str = Field(
        default_factory=lambda: str(
            _LLM_DEFAULTS.get(
                "target_template",
                "Task: Determine if two bibliographic records represent the same intellectual work.\nCriteria: Editions, translations, and reprints match. Different volumes, parts, or authors do not match.\n\nTarget Record: {text}\n",
            )
        )
    )
    candidate_suffix_template: str = Field(
        default_factory=lambda: str(
            _LLM_DEFAULTS.get(
                "candidate_suffix_template",
                "Candidate Record: {text}\n\nQuestion: Do Target Record and Candidate Record represent the same work? Answer YES or NO: ",
            )
        )
    )
    max_seq_length: int = Field(
        default_factory=lambda: int(_LLM_DEFAULTS.get("max_seq_length", 2048))
    )
    calibrate_prior: bool = Field(
        default_factory=lambda: bool(_LLM_DEFAULTS.get("calibrate_prior", True))
    )
    marginalize_tokens: bool = Field(
        default_factory=lambda: bool(_LLM_DEFAULTS.get("marginalize_tokens", True))
    )
    prefix_lm_mask: bool = Field(
        default_factory=lambda: bool(_LLM_DEFAULTS.get("prefix_lm_mask", True))
    )
    symmetric_scoring: bool = Field(
        default_factory=lambda: bool(_LLM_DEFAULTS.get("symmetric_scoring", True))
    )
    symmetric_margin_threshold: float = Field(
        default_factory=lambda: float(_LLM_DEFAULTS.get("symmetric_margin_threshold", 0.05))
    )
    adaptive_cot: bool = Field(
        default_factory=lambda: bool(_LLM_DEFAULTS.get("adaptive_cot", False))
    )
    adaptive_cot_max_tokens: int = Field(
        default_factory=lambda: int(_LLM_DEFAULTS.get("adaptive_cot_max_tokens", 64))
    )


class CascadeRouterConfig(BaseModel):
    """Configuration for cascading evaluation and LLM judge escalation."""

    model_config = ConfigDict(extra="forbid")

    heuristic_engine: Literal["bm25", "cross_encoder", "fuzzy", "noop"] = Field(
        default_factory=lambda: str(_CASCADE_DEFAULTS.get("heuristic_engine", "bm25"))  # type: ignore[assignment]
    )
    high_confidence_threshold: float = Field(
        default_factory=lambda: float(_CASCADE_DEFAULTS.get("high_confidence_threshold", 0.90)),
        ge=0.0,
        le=1.0,
    )
    low_confidence_threshold: float = Field(
        default_factory=lambda: float(_CASCADE_DEFAULTS.get("low_confidence_threshold", 0.55)),
        ge=0.0,
        le=1.0,
    )
    ambiguity_margin: float = Field(
        default_factory=lambda: float(_CASCADE_DEFAULTS.get("ambiguity_margin", 0.08)),
        ge=0.0,
        le=1.0,
    )
    uncertainty_band_min: float = Field(
        default_factory=lambda: float(_CASCADE_DEFAULTS.get("uncertainty_band_min", 0.60)),
        ge=0.0,
        le=1.0,
    )
    uncertainty_band_max: float = Field(
        default_factory=lambda: float(_CASCADE_DEFAULTS.get("uncertainty_band_max", 0.88)),
        ge=0.0,
        le=1.0,
    )
    max_escalated_candidates: int = Field(
        default_factory=lambda: int(_CASCADE_DEFAULTS.get("max_escalated_candidates", 5)),
        ge=1,
    )

    @model_validator(mode="after")
    def validate_threshold_order(self) -> CascadeRouterConfig:
        if self.low_confidence_threshold > self.high_confidence_threshold:
            raise ValueError(
                f"low_confidence_threshold ({self.low_confidence_threshold}) must be <= "
                f"high_confidence_threshold ({self.high_confidence_threshold})"
            )
        if self.uncertainty_band_min > self.uncertainty_band_max:
            raise ValueError(
                f"uncertainty_band_min ({self.uncertainty_band_min}) must be <= "
                f"uncertainty_band_max ({self.uncertainty_band_max})"
            )
        if self.uncertainty_band_min < self.low_confidence_threshold:
            raise ValueError(
                f"uncertainty_band_min ({self.uncertainty_band_min}) must be >= "
                f"low_confidence_threshold ({self.low_confidence_threshold})"
            )
        if self.uncertainty_band_max > self.high_confidence_threshold:
            raise ValueError(
                f"uncertainty_band_max ({self.uncertainty_band_max}) must be <= "
                f"high_confidence_threshold ({self.high_confidence_threshold})"
            )
        return self


class RerankConfig(BaseModel):
    """Configuration for candidate reranking, metadata discriminators, and LLM judge."""

    enabled: bool = Field(default_factory=lambda: bool(_RERANK_DEFAULTS.get("enabled", True)))
    engine: RerankEngineType = Field(
        default_factory=lambda: str(_RERANK_DEFAULTS.get("engine", "cascade"))  # type: ignore[assignment]
    )
    model_name: Optional[str] = Field(default_factory=lambda: _RERANK_DEFAULTS.get("model_name"))
    device: Optional[Literal["auto", "cpu", "cuda", "mps"]] = Field(
        default_factory=lambda: _RERANK_DEFAULTS.get("device", "auto")
    )
    batch_size: int = Field(default_factory=lambda: int(_RERANK_DEFAULTS.get("batch_size", 16)), ge=1)
    dense_weight: float = Field(
        default_factory=lambda: float(_RERANK_DEFAULTS.get("dense_weight", 0.5)),
        ge=0.0,
        le=1.0,
    )
    rerank_weight: float = Field(
        default_factory=lambda: float(_RERANK_DEFAULTS.get("rerank_weight", 0.5)),
        ge=0.0,
        le=1.0,
    )
    top_k: int = Field(default_factory=lambda: int(_RERANK_DEFAULTS.get("top_k", 20)), ge=1)
    short_circuit: ShortCircuitConfig = Field(default_factory=ShortCircuitConfig)
    require_volume_match: bool = Field(
        default_factory=lambda: bool(_RERANK_DEFAULTS.get("require_volume_match", True))
    )
    require_surname_match: bool = Field(
        default_factory=lambda: bool(_RERANK_DEFAULTS.get("require_surname_match", True))
    )
    max_year_difference: int = Field(
        default_factory=lambda: int(_RERANK_DEFAULTS.get("max_year_difference", 5)),
        ge=0,
    )
    year_penalty_threshold: int = Field(
        default_factory=lambda: int(_RERANK_DEFAULTS.get("year_penalty_threshold", 1)),
        ge=0,
    )
    apply_pre_veto: bool = Field(
        default_factory=lambda: bool(_RERANK_DEFAULTS.get("apply_pre_veto", True))
    )
    judge: Optional[LLMJudgeConfig] = None
    cascade: CascadeRouterConfig = Field(default_factory=CascadeRouterConfig)

    @model_validator(mode="after")
    def validate_weights(self) -> RerankConfig:
        if self.device is None:
            self.device = "auto"
        if not (0.0 <= self.dense_weight <= 1.0):
            raise ValueError(f"dense_weight must be in [0.0, 1.0], got {self.dense_weight}")
        if not (0.0 <= self.rerank_weight <= 1.0):
            raise ValueError(f"rerank_weight must be in [0.0, 1.0], got {self.rerank_weight}")
        if round(self.dense_weight + self.rerank_weight, 5) != 1.0:
            raise ValueError(
                f"dense_weight and rerank_weight must sum to 1.0, got {self.dense_weight + self.rerank_weight}"
            )
        return self


class AudiobookFunnelConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    default_language: str = Field(
        default_factory=lambda: str(
            _FUNNELS_DEFAULTS.get("audiobook", {}).get("default_language", "fin")
        )
    )
    title_suffix: str = Field(
        default_factory=lambda: str(
            _FUNNELS_DEFAULTS.get("audiobook", {}).get("title_suffix", " (äänikirja)")
        )
    )
    undefined_language: str = Field(
        default_factory=lambda: str(
            _FUNNELS_DEFAULTS.get("audiobook", {}).get("undefined_language", "und")
        )
    )
    source_id_suffix: str = Field(
        default_factory=lambda: str(
            _FUNNELS_DEFAULTS.get("audiobook", {}).get("source_id_suffix", "_audio")
        )
    )
    expression_uri_prefix: str = Field(
        default_factory=lambda: str(
            _FUNNELS_DEFAULTS.get("audiobook", {}).get("expression_uri_prefix", "#spoken_word_")
        )
    )
    content_type: str = Field(
        default_factory=lambda: str(
            _FUNNELS_DEFAULTS.get("audiobook", {}).get("content_type", "spoken_word")
        )
    )


class RecordedSoundFunnelConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    jaccard_threshold: float = Field(
        default_factory=lambda: float(
            _FUNNELS_DEFAULTS.get("recorded_sound", {}).get("jaccard_threshold", 0.75)
        ),
        ge=0.0,
        le=1.0,
    )
    max_candidates: int = Field(
        default_factory=lambda: int(
            _FUNNELS_DEFAULTS.get("recorded_sound", {}).get("max_candidates", 50)
        ),
        ge=1,
    )
    match_tier: str = Field(
        default_factory=lambda: str(
            _FUNNELS_DEFAULTS.get("recorded_sound", {}).get("match_tier", "tier3_tracklist_jaccard")
        )
    )


class NotatedMusicFunnelConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fallback_match_tier: str = Field(
        default_factory=lambda: str(
            _FUNNELS_DEFAULTS.get("notated_music", {}).get(
                "fallback_match_tier", "tier2_domain_key"
            )
        )
    )


class FunnelPatternsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    notated_music_title: str = Field(
        default_factory=lambda: str(
            _FUNNELS_DEFAULTS.get("patterns", {}).get(
                "notated_music_title", "(?i)score|partituuri|nuotti|stemma"
            )
        )
    )
    podcast_title: str = Field(
        default_factory=lambda: str(
            _FUNNELS_DEFAULTS.get("patterns", {}).get("podcast_title", "podcast")
        )
    )
    parallel_translators_signature: str = Field(
        default_factory=lambda: str(
            _FUNNELS_DEFAULTS.get("patterns", {}).get(
                "parallel_translators_signature", "(?i)parallel"
            )
        )
    )
    government_valto_urn: str = Field(
        default_factory=lambda: str(
            _FUNNELS_DEFAULTS.get("patterns", {}).get("government_valto_urn", "(?i)valto")
        )
    )
    exhibition_title: str = Field(
        default_factory=lambda: str(
            _FUNNELS_DEFAULTS.get("patterns", {}).get("exhibition_title", "näyttely")
        )
    )
    legal_statute_title: str = Field(
        default_factory=lambda: str(
            _FUNNELS_DEFAULTS.get("patterns", {}).get(
                "legal_statute_title", "(?i)laki|asetus|säädös"
            )
        )
    )


class FunnelsConfig(BaseModel):
    """Configuration surface for funnel thresholds, heuristics, and pattern defaults."""

    model_config = ConfigDict(extra="forbid")
    default_match_tier: str = Field(
        default_factory=lambda: str(_FUNNELS_DEFAULTS.get("default_match_tier", "tier2_domain_key"))
    )
    default_confidence: float = Field(
        default_factory=lambda: float(_FUNNELS_DEFAULTS.get("default_confidence", 0.90)),
        ge=0.0,
        le=1.0,
    )
    audiobook: AudiobookFunnelConfig = Field(default_factory=AudiobookFunnelConfig)
    recorded_sound: RecordedSoundFunnelConfig = Field(default_factory=RecordedSoundFunnelConfig)
    notated_music: NotatedMusicFunnelConfig = Field(default_factory=NotatedMusicFunnelConfig)
    patterns: FunnelPatternsConfig = Field(default_factory=FunnelPatternsConfig)
