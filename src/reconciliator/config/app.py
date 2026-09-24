"""Root application configuration model combining all subsystems."""

from __future__ import annotations

from pathlib import Path
from typing import Literal, Optional, Self

from pydantic import BaseModel, Field, model_validator

from reconciliator.config.authority import (
    AgentAuthorityConfig,
    ConstituentsConfig,
    VirtualAuthorityConfig,
)
from reconciliator.config.defaults import (
    get_bundled_agent_authority_defaults,
    get_bundled_agent_clustering_defaults,
    get_bundled_bruno_defaults,
    get_bundled_compute_defaults,
    get_bundled_consolidation_defaults,
    get_bundled_constituents_defaults,
    get_bundled_embedding_defaults,
    get_bundled_enrichment_defaults,
    get_bundled_funnels_defaults,
    get_bundled_ingestion_defaults,
    get_bundled_llm_defaults,
    get_bundled_logging_defaults,
    get_bundled_reconcile_defaults,
    get_bundled_sources_defaults,
    get_bundled_storage_defaults,
    get_bundled_telemetry_defaults,
    get_bundled_vector_model_default,
    get_bundled_virtual_authority_defaults,
    get_bundled_vocabularies_defaults,
)
from reconciliator.config.infrastructure import (
    BrunoConfig,
    ComputeConfig,
    EmbeddingConfig,
    EntraAuthConfig,
    LoggingConfig,
    SourceConfig,
    StorageConfig,
    TelemetryConfig,
)
from reconciliator.config.ingestion import (
    FieldTruncationConfig,
    IngestFilterConfig,
    IngestionConfig,
)
from reconciliator.config.matching import (
    AgentClusterConfig,
    FunnelsConfig,
    FunnelSpec,
    LLMJudgeConfig,
    ReconcileFTSFallbackConfig,
    RerankConfig,
    WEMIClusterConfig,
    WorkConsolidationConfig,
)
from reconciliator.config.normalization import RecordNormalizationConfig
from reconciliator.config.semantics import (
    EnrichmentConfig,
    LinguisticConfig,
    VocabulariesConfig,
)

_COMPUTE_DEFAULTS = get_bundled_compute_defaults()
_STORAGE_DEFAULTS = get_bundled_storage_defaults()
_TELEMETRY_DEFAULTS = get_bundled_telemetry_defaults()
_LOGGING_DEFAULTS = get_bundled_logging_defaults()
_VECTOR_MODEL_DEFAULT = get_bundled_vector_model_default()
_EMBED_DEFAULTS = get_bundled_embedding_defaults()
_LLM_DEFAULTS = get_bundled_llm_defaults()
_CONSTITUENTS_DEFAULTS = get_bundled_constituents_defaults()
_INGESTION_DEFAULTS = get_bundled_ingestion_defaults()
_CONSOLIDATION_DEFAULTS = get_bundled_consolidation_defaults()
_AGENT_CLUSTERING_DEFAULTS = get_bundled_agent_clustering_defaults()
_VIRTUAL_AUTHORITY_DEFAULTS = get_bundled_virtual_authority_defaults()
_AGENT_AUTHORITY_DEFAULTS = get_bundled_agent_authority_defaults()
_BRUNO_DEFAULTS = get_bundled_bruno_defaults()
_FUNNELS_DEFAULTS = get_bundled_funnels_defaults()
_ENRICH_DEFAULTS = get_bundled_enrichment_defaults()
_RECONCILE_DEFAULTS = get_bundled_reconcile_defaults()
_VOCAB_DEFAULTS = get_bundled_vocabularies_defaults()


class TemplateSetPathsConfig(BaseModel):
    """File paths for WEMI and agent serialization templates."""

    work: Path = Path("reconciliator/embedding/templates/work_serialization.j2")
    expression: Path = Path("reconciliator/embedding/templates/expression_serialization.j2")
    manifestation: Path = Path("reconciliator/embedding/templates/manifestation_serialization.j2")
    agent: Path = Path("reconciliator/embedding/templates/agent_serialization.j2")


class ReportSetPathsConfig(BaseModel):
    """Paths for detailed and summary TSV reports."""

    detailed_file: Path
    summary_file: Path


class ReconcileConfig(IngestFilterConfig, FieldTruncationConfig):
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
    cluster: WEMIClusterConfig = Field(default_factory=WEMIClusterConfig)
    enrichment: EnrichmentConfig = Field(default_factory=EnrichmentConfig)
    constituents: ConstituentsConfig = Field(
        default_factory=lambda: ConstituentsConfig(**_CONSTITUENTS_DEFAULTS)
    )
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


class AppConfig(BaseModel):
    """Root configuration object combining all subsystems."""

    compute: ComputeConfig = Field(default_factory=lambda: ComputeConfig(**_COMPUTE_DEFAULTS))
    storage: StorageConfig = Field(default_factory=lambda: StorageConfig(**_STORAGE_DEFAULTS))
    telemetry: TelemetryConfig = Field(
        default_factory=lambda: TelemetryConfig(**_TELEMETRY_DEFAULTS)
    )
    logging: LoggingConfig = Field(default_factory=lambda: LoggingConfig(**_LOGGING_DEFAULTS))
    vector_model: Literal["embedding", "llm"] = Field(default_factory=lambda: _VECTOR_MODEL_DEFAULT)
    embedding: EmbeddingConfig = Field(default_factory=lambda: EmbeddingConfig(**_EMBED_DEFAULTS))
    llm: LLMJudgeConfig = Field(default_factory=lambda: LLMJudgeConfig(**_LLM_DEFAULTS))
    linguistic: LinguisticConfig = Field(default_factory=LinguisticConfig)
    vocabularies: VocabulariesConfig = Field(default_factory=VocabulariesConfig)
    record_normalization: RecordNormalizationConfig = Field(
        default_factory=RecordNormalizationConfig
    )
    enrichment: EnrichmentConfig = Field(
        default_factory=lambda: EnrichmentConfig(**_ENRICH_DEFAULTS)
    )
    ingestion: IngestionConfig = Field(
        default_factory=lambda: IngestionConfig(**_INGESTION_DEFAULTS)
    )
    constituents: ConstituentsConfig = Field(
        default_factory=lambda: ConstituentsConfig(**_CONSTITUENTS_DEFAULTS)
    )
    agent_authority: AgentAuthorityConfig = Field(
        default_factory=lambda: AgentAuthorityConfig(**_AGENT_AUTHORITY_DEFAULTS)
    )
    agent_clustering: AgentClusterConfig = Field(
        default_factory=lambda: AgentClusterConfig(**_AGENT_CLUSTERING_DEFAULTS)
    )
    virtual_authority: VirtualAuthorityConfig = Field(
        default_factory=lambda: VirtualAuthorityConfig(**_VIRTUAL_AUTHORITY_DEFAULTS)
    )
    funnels: FunnelsConfig = Field(default_factory=lambda: FunnelsConfig(**_FUNNELS_DEFAULTS))
    consolidation: WorkConsolidationConfig = Field(
        default_factory=lambda: WorkConsolidationConfig(**_CONSOLIDATION_DEFAULTS)
    )
    reconcile: Optional[ReconcileConfig] = None
    bruno: BrunoConfig = Field(default_factory=lambda: BrunoConfig(**_BRUNO_DEFAULTS))
    sources: dict[str, SourceConfig] = Field(
        default_factory=lambda: {
            k: SourceConfig(**v) if isinstance(v, dict) else v
            for k, v in get_bundled_sources_defaults().items()
        }
    )
    auth: EntraAuthConfig = Field(default_factory=EntraAuthConfig)
    config_dir: Optional[Path] = None

    @model_validator(mode="after")
    def _validate_vector_model(self) -> Self:
        if self.vector_model not in ("embedding", "llm"):
            raise ValueError(
                f"Invalid vector_model: '{self.vector_model}'. "
                "Must be explicitly set to either 'embedding' or 'llm'."
            )
        if self.vector_model == "llm":
            if self.reconcile is not None:
                self.reconcile.rerank.judge = self.llm
        elif self.vector_model == "embedding":
            if self.reconcile is not None:
                if self.reconcile.rerank.engine == "llm_judge":
                    raise ValueError(
                        "Rerank engine 'llm_judge' cannot be used when vector_model is 'embedding'. "
                        "Set vector_model: 'llm' to use LLM Judge."
                    )
                self.reconcile.rerank.judge = None
        return self

    @model_validator(mode="after")
    def _propagate_shared_settings(self) -> Self:
        shared_dev = self.compute.device
        shared_threads = self.compute.threads

        if self.storage.vector_engine.device == "auto" and shared_dev != "auto":
            self.storage.vector_engine.device = shared_dev
        if self.storage.vector_engine.threads is None and shared_threads is not None:
            self.storage.vector_engine.threads = shared_threads

        if self.embedding and self.embedding.device == "auto" and shared_dev != "auto":
            self.embedding.device = shared_dev
        if self.embedding and self.embedding.num_cpu_threads is None and shared_threads is not None:
            self.embedding.num_cpu_threads = shared_threads

        if self.llm and self.llm.device == "auto" and shared_dev != "auto":
            self.llm.device = shared_dev

        if self.reconcile:
            if self.reconcile.rerank.device == "auto" and shared_dev != "auto":
                self.reconcile.rerank.device = shared_dev

        # Propagate linguistic configuration to record_normalization
        if self.linguistic and self.record_normalization:
            self.record_normalization.linguistic = self.linguistic

        # Propagate vocabularies defaults to ingestion.skosmos
        for skos_cfg in self.ingestion.skosmos.values():
            if skos_cfg.vocabularies is None:
                skos_cfg.vocabularies = list(self.vocabularies.download_list)

        # Propagate vocabularies defaults to ingestion.vocab
        if self.ingestion.vocab is not None:
            if self.ingestion.vocab.vocabularies is None:
                self.ingestion.vocab.vocabularies = list(self.vocabularies.download_list)
            if self.ingestion.vocab.legacy_thesauri is None:
                self.ingestion.vocab.legacy_thesauri = list(
                    self.vocabularies.legacy_to_modern.keys()
                )

        # Propagate wikidata author properties to ingestion.wikidata
        if self.ingestion.wikidata is not None:
            if self.ingestion.wikidata.author_authority_properties is None:
                self.ingestion.wikidata.author_authority_properties = list(
                    self.vocabularies.wikidata_author_properties.values()
                )

        # Propagate global ingest filter rules to sources if not explicitly defined
        ingest_filter = self.ingestion.filter
        if ingest_filter:
            for src in self.ingestion.oai.values():
                if not src.target_work_types and ingest_filter.target_work_types:
                    src.target_work_types = list(ingest_filter.target_work_types)
                if not src.exclude_work_types and ingest_filter.exclude_work_types:
                    src.exclude_work_types = list(ingest_filter.exclude_work_types)
                if not src.target_languages and ingest_filter.target_languages:
                    src.target_languages = list(ingest_filter.target_languages)
                if src.from_year is None and ingest_filter.from_year is not None:
                    src.from_year = ingest_filter.from_year
                if src.until_year is None and ingest_filter.until_year is not None:
                    src.until_year = ingest_filter.until_year
            if self.ingestion.wikidata is not None:
                if (
                    not self.ingestion.wikidata.target_work_types
                    and ingest_filter.target_work_types
                ):
                    self.ingestion.wikidata.target_work_types = list(
                        ingest_filter.target_work_types
                    )
                if (
                    not self.ingestion.wikidata.exclude_work_types
                    and ingest_filter.exclude_work_types
                ):
                    self.ingestion.wikidata.exclude_work_types = list(
                        ingest_filter.exclude_work_types
                    )
                if not self.ingestion.wikidata.target_languages and ingest_filter.target_languages:
                    self.ingestion.wikidata.target_languages = list(ingest_filter.target_languages)
                if (
                    self.ingestion.wikidata.from_year is None
                    and ingest_filter.from_year is not None
                ):
                    self.ingestion.wikidata.from_year = ingest_filter.from_year
                if (
                    self.ingestion.wikidata.until_year is None
                    and ingest_filter.until_year is not None
                ):
                    self.ingestion.wikidata.until_year = ingest_filter.until_year
            if self.reconcile is not None:
                if not self.reconcile.target_work_types and ingest_filter.target_work_types:
                    self.reconcile.target_work_types = list(ingest_filter.target_work_types)
                if not self.reconcile.exclude_work_types and ingest_filter.exclude_work_types:
                    self.reconcile.exclude_work_types = list(ingest_filter.exclude_work_types)
                if not self.reconcile.target_languages and ingest_filter.target_languages:
                    self.reconcile.target_languages = list(ingest_filter.target_languages)
                if self.reconcile.from_year is None and ingest_filter.from_year is not None:
                    self.reconcile.from_year = ingest_filter.from_year
                if self.reconcile.until_year is None and ingest_filter.until_year is not None:
                    self.reconcile.until_year = ingest_filter.until_year

        return self


def get_source_aliases(source: str, app_config: Optional[AppConfig] = None) -> list[str]:
    """Return configured aliases for a given source name."""
    s_norm = (source or "").strip().lower()
    if app_config and app_config.sources:
        cfg = app_config.sources.get(s_norm)
        if cfg:
            return list(cfg.aliases)
    defaults = get_bundled_sources_defaults()
    src_data = defaults.get(s_norm)
    if isinstance(src_data, dict):
        return list(src_data.get("aliases") or [])
    return []
