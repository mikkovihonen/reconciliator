"""Graph clustering and agent entity clustering configuration models."""

from __future__ import annotations

from typing import Literal, Union

from pydantic import BaseModel, Field, model_validator

from reconciliator.config.defaults import (
    get_bundled_agent_clustering_defaults,
    get_bundled_reconcile_defaults,
)

_RECONCILE_DEFAULTS = get_bundled_reconcile_defaults()
_CLUSTER_DEFAULTS = dict(_RECONCILE_DEFAULTS.get("cluster") or {})
_AGENT_CLUSTERING_DEFAULTS = get_bundled_agent_clustering_defaults()


class MultiplexClusterThresholds(BaseModel):
    """Layer-specific cosine similarity thresholds for 3-layer multiplex graph clustering."""

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
            _CLUSTER_DEFAULTS.get("scoring", {}).get("with_author", {}).get("title", 0.6)
        )
    )
    author: float = Field(
        default_factory=lambda: float(
            _CLUSTER_DEFAULTS.get("scoring", {}).get("with_author", {}).get("author", 0.2)
        )
    )
    year: float = Field(
        default_factory=lambda: float(
            _CLUSTER_DEFAULTS.get("scoring", {}).get("with_author", {}).get("year", 0.1)
        )
    )
    type: float = Field(
        default_factory=lambda: float(
            _CLUSTER_DEFAULTS.get("scoring", {}).get("with_author", {}).get("type", 0.1)
        )
    )


class WEMIScoringWeightsWithoutAuthor(BaseModel):
    title: float = Field(
        default_factory=lambda: float(
            _CLUSTER_DEFAULTS.get("scoring", {}).get("without_author", {}).get("title", 0.8)
        )
    )
    year: float = Field(
        default_factory=lambda: float(
            _CLUSTER_DEFAULTS.get("scoring", {}).get("without_author", {}).get("year", 0.1)
        )
    )
    type: float = Field(
        default_factory=lambda: float(
            _CLUSTER_DEFAULTS.get("scoring", {}).get("without_author", {}).get("type", 0.1)
        )
    )


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
            _CLUSTER_DEFAULTS.get("scoring", {}).get("author_missing_similarity", 0.5)
        )
    )
    year_missing_similarity: float = Field(
        default_factory=lambda: float(
            _CLUSTER_DEFAULTS.get("scoring", {}).get("year_missing_similarity", 0.5)
        )
    )
    year_max_difference: float = Field(
        default_factory=lambda: float(
            _CLUSTER_DEFAULTS.get("scoring", {}).get("year_max_difference", 5.0)
        )
    )
    cross_format_type_similarity: float = Field(
        default_factory=lambda: float(
            _CLUSTER_DEFAULTS.get("scoring", {}).get("cross_format_type_similarity", 0.5)
        )
    )
    default_type_similarity: float = Field(
        default_factory=lambda: float(
            _CLUSTER_DEFAULTS.get("scoring", {}).get("default_type_similarity", 0.5)
        )
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

    enabled: bool = Field(default=True)
    min_constituents_for_overlap: int = Field(
        default_factory=lambda: int(
            _CLUSTER_DEFAULTS.get("aggregates", {}).get("min_constituents_for_overlap", 3)
        )
    )
    jaccard_merge_threshold: float = Field(
        default_factory=lambda: float(
            _CLUSTER_DEFAULTS.get("aggregates", {}).get("jaccard_merge_threshold", 0.80)
        )
    )
    jaccard_veto_threshold: float = Field(
        default_factory=lambda: float(
            _CLUSTER_DEFAULTS.get("aggregates", {}).get("jaccard_veto_threshold", 0.30)
        )
    )
    merge_bonus_score: float = Field(
        default_factory=lambda: float(
            _CLUSTER_DEFAULTS.get("aggregates", {}).get("merge_bonus_score", 0.95)
        )
    )
    sequence_alignment: bool = Field(
        default_factory=lambda: bool(
            _CLUSTER_DEFAULTS.get("aggregates", {}).get("sequence_alignment", True)
        )
    )
    lcs_threshold: float = Field(
        default_factory=lambda: float(
            _CLUSTER_DEFAULTS.get("aggregates", {}).get("lcs_threshold", 0.70)
        )
    )
    blocking_family: str = Field(
        default_factory=lambda: str(
            _CLUSTER_DEFAULTS.get("aggregates", {}).get("blocking_family", "constituent_work")
        )
    )


class ClusterConfig(BaseModel):
    """Configuration for multiplex graph clustering and blocking."""

    enabled: bool = True
    similarity_threshold: float = 0.88
    max_cluster_size: int = 500
    blocking_families: Union[list[str], Literal["all"]] = Field(
        default_factory=lambda: [
            "work_blocking_key",
            "series_key",
            "music_work_key",
            "standard_key",
        ]
    )
    thresholds: MultiplexClusterThresholds = Field(default_factory=MultiplexClusterThresholds)
    message_passing: bool = True
    reconcile_items: bool = False
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


class AgentClusterScoringWeights(BaseModel):
    """Weights for multi-feature agent cluster scoring."""

    name_weight: float = Field(
        default_factory=lambda: float(
            _AGENT_CLUSTERING_DEFAULTS.get("scoring_weights", {}).get("name_weight", 0.40)
        )
    )
    dates_weight: float = Field(
        default_factory=lambda: float(
            _AGENT_CLUSTERING_DEFAULTS.get("scoring_weights", {}).get("dates_weight", 0.20)
        )
    )
    work_cluster_weight: float = Field(
        default_factory=lambda: float(
            _AGENT_CLUSTERING_DEFAULTS.get("scoring_weights", {}).get("work_cluster_weight", 0.15)
        )
    )
    coauthor_weight: float = Field(
        default_factory=lambda: float(
            _AGENT_CLUSTERING_DEFAULTS.get("scoring_weights", {}).get("coauthor_weight", 0.10)
        )
    )
    affiliation_weight: float = Field(
        default_factory=lambda: float(
            _AGENT_CLUSTERING_DEFAULTS.get("scoring_weights", {}).get("affiliation_weight", 0.05)
        )
    )
    concept_weight: float = Field(
        default_factory=lambda: float(
            _AGENT_CLUSTERING_DEFAULTS.get("scoring_weights", {}).get("concept_weight", 0.05)
        )
    )
    qualifier_weight: float = Field(
        default_factory=lambda: float(
            _AGENT_CLUSTERING_DEFAULTS.get("scoring_weights", {}).get("qualifier_weight", 0.05)
        )
    )


class AgentClusterConfig(BaseModel):
    """Configuration for cross-source agent entity clustering."""

    enabled: bool = Field(
        default_factory=lambda: bool(_AGENT_CLUSTERING_DEFAULTS.get("enabled", True))
    )
    similarity_threshold: float = Field(
        default_factory=lambda: float(_AGENT_CLUSTERING_DEFAULTS.get("similarity_threshold", 0.85))
    )
    date_tolerance_years: int = Field(
        default_factory=lambda: int(_AGENT_CLUSTERING_DEFAULTS.get("date_tolerance_years", 1))
    )
    major_date_discrepancy_veto_years: int = Field(
        default_factory=lambda: int(
            _AGENT_CLUSTERING_DEFAULTS.get("major_date_discrepancy_veto_years", 2)
        )
    )
    require_corroboration_for_date_discrepancy: bool = Field(
        default_factory=lambda: bool(
            _AGENT_CLUSTERING_DEFAULTS.get("require_corroboration_for_date_discrepancy", True)
        )
    )
    min_career_overlap_tolerance_years: int = Field(
        default_factory=lambda: int(
            _AGENT_CLUSTERING_DEFAULTS.get("min_career_overlap_tolerance_years", 40)
        )
    )
    batch_size: int = Field(
        default_factory=lambda: int(_AGENT_CLUSTERING_DEFAULTS.get("batch_size", 1000))
    )
    token_matching_algorithm: str = Field(
        default_factory=lambda: str(
            _AGENT_CLUSTERING_DEFAULTS.get("token_matching_algorithm", "token_set_ratio")
        )
    )
    affiliation_fuzzy_threshold: float = Field(
        default_factory=lambda: float(
            _AGENT_CLUSTERING_DEFAULTS.get("affiliation_fuzzy_threshold", 80.0)
        )
    )
    affiliation_fuzzy_score: float = Field(
        default_factory=lambda: float(
            _AGENT_CLUSTERING_DEFAULTS.get("affiliation_fuzzy_score", 0.85)
        )
    )
    enable_hierarchical_concepts: bool = Field(
        default_factory=lambda: bool(
            _AGENT_CLUSTERING_DEFAULTS.get("enable_hierarchical_concepts", True)
        )
    )
    concept_similarity_weight: float = Field(
        default_factory=lambda: float(
            _AGENT_CLUSTERING_DEFAULTS.get("concept_similarity_weight", 0.85)
        )
    )
    max_blocking_bucket_size: int = Field(
        default_factory=lambda: int(_AGENT_CLUSTERING_DEFAULTS.get("max_blocking_bucket_size", 200))
    )
    date_tolerance_score: float = Field(
        default_factory=lambda: float(_AGENT_CLUSTERING_DEFAULTS.get("date_tolerance_score", 0.7))
    )
    date_missing_score: float = Field(
        default_factory=lambda: float(_AGENT_CLUSTERING_DEFAULTS.get("date_missing_score", 0.5))
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
        )
    )


AgentClusteringConfig = AgentClusterConfig
