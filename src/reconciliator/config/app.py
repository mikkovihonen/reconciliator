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
from reconciliator.config.clustering import AgentClusterConfig
from reconciliator.config.compute import ComputeConfig
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
    get_bundled_sources_defaults,
    get_bundled_storage_defaults,
    get_bundled_telemetry_defaults,
    get_bundled_vector_model_default,
    get_bundled_virtual_authority_defaults,
)
from reconciliator.config.embedding import EmbeddingConfig
from reconciliator.config.enrichment import EnrichmentConfig
from reconciliator.config.funnels import FunnelsConfig
from reconciliator.config.ingestion import IngestionConfig
from reconciliator.config.linguistic import LinguisticConfig
from reconciliator.config.normalization import RecordNormalizationConfig
from reconciliator.config.reconciliation import ReconcileConfig, WorkConsolidationConfig
from reconciliator.config.rerank import LLMJudgeConfig
from reconciliator.config.storage import StorageConfig
from reconciliator.config.system import (
    BrunoConfig,
    EntraAuthConfig,
    LoggingConfig,
    SourceConfig,
    TelemetryConfig,
)
from reconciliator.config.vocabularies import VocabulariesConfig

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

        # Propagate compute hardware device if subsystem is set to "auto"
        if self.storage.vector_engine.device == "auto" and shared_dev != "auto":
            self.storage.vector_engine.device = shared_dev
        if self.storage.vector_engine.threads is None and shared_threads is not None:
            self.storage.vector_engine.threads = shared_threads

        if self.embedding and self.embedding.device == "auto" and shared_dev != "auto":
            self.embedding.device = shared_dev
        if self.llm and self.llm.device == "auto" and shared_dev != "auto":
            self.llm.device = shared_dev

        if self.reconcile:
            if self.reconcile.rerank.device == "auto" and shared_dev != "auto":
                self.reconcile.rerank.device = shared_dev

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
