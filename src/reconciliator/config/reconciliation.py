"""Reconciliation, matching, consolidation, and reporting configuration models."""

from __future__ import annotations

from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel, Field, model_validator

from reconciliator.config.clustering import ClusterConfig
from reconciliator.config.defaults import (
    get_bundled_consolidation_defaults,
    get_bundled_funnels_defaults,
    get_bundled_reconcile_defaults,
    get_bundled_vocabularies_defaults,
)
from reconciliator.config.enrichment import EnrichmentConfig
from reconciliator.config.funnels import FunnelsConfig, FunnelSpec
from reconciliator.config.ingestion import IngestFilterConfig
from reconciliator.config.normalization import RecordNormalizationConfig
from reconciliator.config.rerank import RerankConfig

_CONSOLIDATION_DEFAULTS = get_bundled_consolidation_defaults()
_RECONCILE_DEFAULTS = get_bundled_reconcile_defaults()
_FUNNELS_DEFAULTS = get_bundled_funnels_defaults()
_VOCAB_DEFAULTS = get_bundled_vocabularies_defaults()


class WorkConsolidationConfig(BaseModel):
    """Configuration for cross-source work identity consolidation.

    Each contributing source mints its own work URI, so the same work arrives once
    per source. Consolidation records `same_work_as` edges between those rows and
    elects one canonical work per equivalence group, without deleting anything.
    """

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


ConsolidationConfig = WorkConsolidationConfig


class TemplateSetPathsConfig(BaseModel):
    """File paths for the WEMI and agent serialization templates."""

    work: Path = Path("reconciliator/embedding/templates/work_serialization.j2")
    expression: Path = Path("reconciliator/embedding/templates/expression_serialization.j2")
    manifestation: Path = Path("reconciliator/embedding/templates/manifestation_serialization.j2")
    agent: Path = Path("reconciliator/embedding/templates/agent_serialization.j2")


class ReportSetPathsConfig(BaseModel):
    """Paths for detailed and summary TSV reports."""

    detailed_file: Path
    summary_file: Path


class ReconcileFTSFallbackConfig(BaseModel):
    """Configuration for WorkMatcher Tier 3 PostgreSQL full-text search fallback during reconciliation."""

    enabled: bool = True
    limit: int = 5
    confidence_score: float = 0.80

    @model_validator(mode="after")
    def validate_params(self) -> ReconcileFTSFallbackConfig:
        if not (0.0 <= self.confidence_score <= 1.0):
            raise ValueError(f"confidence_score must be in [0.0, 1.0], got {self.confidence_score}")
        if self.limit <= 0:
            raise ValueError(f"limit must be a positive integer, got {self.limit}")
        return self


FTSFallbackConfig = ReconcileFTSFallbackConfig


class ReconcileConfig(IngestFilterConfig):
    """Offline reconciliation and enrichment configuration."""

    target_file: Path
    corpus_source_name: Optional[str] = None
    funnels: list[FunnelSpec] = Field(default_factory=list)
    funnel_settings: FunnelsConfig = Field(
        default_factory=lambda: FunnelsConfig(**_FUNNELS_DEFAULTS)
    )
    config_dir: Optional[Path] = None
    format: Literal["auto", "marcxml", "bibra", "bibframe"] = "auto"
    schema_profile: Optional[str] = None
    output_file: Optional[Path] = None
    reports_dir: Optional[Path] = Path("data/reports")
    agent_reports: Optional[ReportSetPathsConfig] = None
    subject_reports: Optional[ReportSetPathsConfig] = None
    work_reports: Optional[ReportSetPathsConfig] = None
    expression_reports: Optional[ReportSetPathsConfig] = None
    templates: TemplateSetPathsConfig = Field(default_factory=TemplateSetPathsConfig)
    cluster: ClusterConfig = Field(default_factory=ClusterConfig)
    enrichment: EnrichmentConfig = Field(default_factory=EnrichmentConfig)
    enrich: bool = True
    fold_legacy: bool = True
    threshold: float = 0.85
    rerank: RerankConfig = Field(default_factory=RerankConfig)
    fts_fallback: ReconcileFTSFallbackConfig = Field(default_factory=ReconcileFTSFallbackConfig)
    link_policy: Literal["priority_fallback", "all"] = "priority_fallback"
    quality_brackets: Optional[list[float]] = None
    normalization: Optional[RecordNormalizationConfig] = None
    fallback_agent_reconciliation: bool = True
    agent_fallback_threshold: float = 0.90
    verify_chronology: bool = True
    chronology_min_author_age: int = 12
    chronology_max_posthumous_years: int = 70
    strict_homonym_guard: bool = True
    max_editions_compared: int = Field(
        default_factory=lambda: int(_RECONCILE_DEFAULTS.get("max_editions_compared", 50))
    )
    translator_alignment_threshold: float = Field(
        default_factory=lambda: float(
            _RECONCILE_DEFAULTS.get("translator_alignment_threshold", 0.80)
        )
    )
    agent_aligner_threshold: float = Field(
        default_factory=lambda: float(_RECONCILE_DEFAULTS.get("agent_aligner_threshold", 0.85))
    )
    life_date_tolerance_years: int = Field(
        default_factory=lambda: int(_RECONCILE_DEFAULTS.get("life_date_tolerance_years", 2))
    )
    homonym_score_margin: float = Field(
        default_factory=lambda: float(_RECONCILE_DEFAULTS.get("homonym_score_margin", 0.05))
    )
    qualifier_similarity_threshold: float = Field(
        default_factory=lambda: float(
            _RECONCILE_DEFAULTS.get("qualifier_similarity_threshold", 0.80)
        )
    )
    distribution_match_threshold: float = Field(
        default_factory=lambda: float(_RECONCILE_DEFAULTS.get("distribution_match_threshold", 0.85))
    )
    surname_similarity_threshold: float = Field(
        default_factory=lambda: float(_RECONCILE_DEFAULTS.get("surname_similarity_threshold", 0.85))
    )
    allow_pseudonym_author_match: bool = Field(
        default_factory=lambda: bool(_RECONCILE_DEFAULTS.get("allow_pseudonym_author_match", True))
    )
    pseudonym_author_match_weight: float = Field(
        default_factory=lambda: float(
            _RECONCILE_DEFAULTS.get("pseudonym_author_match_weight", 0.95)
        )
    )

    vocabularies: list[str] = Field(default_factory=list)
    agent_priority: list[str] = Field(default_factory=list)
    subject_priority: list[str] = Field(default_factory=list)
    supported_subject_vocabularies: list[str] = Field(default_factory=list)
    entity_vocabularies: list[str] = Field(default_factory=list)
    vocabulary_patterns: dict[str, list[str]] = Field(default_factory=dict)
    truncate_overlong_values: bool = True
    truncation_marker: str = ""
    default_max_field_length: Optional[int] = None
    field_length_overrides: dict[str, int] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_params_and_derive_reports(self) -> ReconcileConfig:
        if not (0.0 <= self.threshold <= 1.0):
            raise ValueError(f"reconcile threshold must be in [0.0, 1.0], got {self.threshold}")
        if not (0.0 <= self.agent_fallback_threshold <= 1.0):
            raise ValueError(
                f"agent_fallback_threshold must be in [0.0, 1.0], got {self.agent_fallback_threshold}"
            )
        if self.chronology_min_author_age < 0:
            raise ValueError(
                f"chronology_min_author_age must be non-negative, got {self.chronology_min_author_age}"
            )
        if self.chronology_max_posthumous_years < 0:
            raise ValueError(
                f"chronology_max_posthumous_years must be non-negative, got {self.chronology_max_posthumous_years}"
            )
        if self.quality_brackets is not None:
            if not self.quality_brackets:
                raise ValueError("quality_brackets cannot be empty if specified")
            for b in self.quality_brackets:
                if not (0.0 <= b <= 1.0):
                    raise ValueError(f"quality_brackets values must be in [0.0, 1.0], got {b}")
            if self.quality_brackets != sorted(set(self.quality_brackets)):
                raise ValueError(
                    f"quality_brackets must be strictly ascending and unique, got {self.quality_brackets}"
                )

        # Derive default report paths from reports_dir
        r_dir = self.reports_dir or Path("data/reports")
        if self.agent_reports is None:
            self.agent_reports = ReportSetPathsConfig(
                detailed_file=r_dir / "agent_reconciliation_uris.tsv",
                summary_file=r_dir / "agent_reconciliation_summary.tsv",
            )
        if self.subject_reports is None:
            self.subject_reports = ReportSetPathsConfig(
                detailed_file=r_dir / "subject_reconciliation_uris.tsv",
                summary_file=r_dir / "subject_reconciliation_summary.tsv",
            )

        # Populate vocabulary priority defaults if not specified
        if not self.agent_priority:
            self.agent_priority = list(_VOCAB_DEFAULTS.get("agent_priority", []))
        if not self.subject_priority:
            self.subject_priority = list(_VOCAB_DEFAULTS.get("subject_priority", []))
        if not self.supported_subject_vocabularies:
            self.supported_subject_vocabularies = list(
                _VOCAB_DEFAULTS.get("supported_subjects", [])
            )
        if not self.entity_vocabularies:
            self.entity_vocabularies = list(_VOCAB_DEFAULTS.get("entity_vocabularies", []))
        if not self.vocabulary_patterns:
            self.vocabulary_patterns = dict(_VOCAB_DEFAULTS.get("vocabulary_patterns", {}))
        if not self.vocabularies:
            self.vocabularies = list(self.agent_priority) + list(self.subject_priority)

        return self
