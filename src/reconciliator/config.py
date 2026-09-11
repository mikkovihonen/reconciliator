"""Unified configuration models and YAML loader for Reconciliator."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal, Optional

import yaml
from pydantic import BaseModel, Field, model_validator


class StorageConfig(BaseModel):
    """File paths for databases and vector indices."""

    authority_db: Path = Path("data/authority.db")
    finto_db: Path = Path("data/finto.db")
    progress_db: Path = Path("data/progress.db")
    index_path: Path = Path("data/authority.faiss")


class EmbeddingConfig(BaseModel):
    """Embedding model and hardware acceleration configuration."""

    model_name: str = "TurkuNLP/sbert-cased-finnish-paraphrase"
    template_path: Path = Path("reconciliator/templates/default_serialization.j2")
    device: Literal["auto", "cuda", "mps", "cpu"] = "auto"
    batch_size: int = 64
    force_template: bool = False

    @model_validator(mode="after")
    def validate_batch_size(self) -> EmbeddingConfig:
        if self.batch_size <= 0:
            raise ValueError(f"batch_size must be a positive integer, got {self.batch_size}")
        return self


class FintoDownloadConfig(BaseModel):
    """Configuration for downloading FINTO value vocabularies."""

    api_url: str = "https://api.finto.fi/rest/v1"
    output_dir: Path = Path("data/finto")
    vocabularies: list[str] = Field(
        default_factory=lambda: ["yso", "yso-paikat", "slm", "kauno", "muso", "seko"]
    )
    format: str = "text/turtle"
    timeout_seconds: float = 120.0
    overwrite: bool = False


class FintoIngestConfig(BaseModel):
    """Configuration for FINTO thesauri and migration ingestion."""

    input_path: Path = Path("data/finto")
    vocabularies: list[str] = Field(
        default_factory=lambda: ["yso", "yso-paikat", "kauno", "muso", "slm", "seko"]
    )
    legacy_thesauri: list[str] = Field(
        default_factory=lambda: ["ysa", "allars", "kaunokki", "musa"]
    )


class OAIIngestConfig(BaseModel):
    """Configuration for an individual OAI-PMH or MARCXML authority source."""

    enabled: bool = True
    source_type: Literal["oai", "file"] = "oai"
    input_path: Optional[Path] = None
    oai_url: Optional[str] = "https://oai-pmh.api.melinda.kansalliskirjasto.fi/bib"
    metadata_prefix: str = "marc21"
    set_spec: Optional[str] = None
    id_source: Literal["marc_001", "oai_identifier", "auto"] = "marc_001"
    workers: int = 4
    from_date: Optional[str] = None
    until_date: Optional[str] = None
    slice_days: int = 30
    batch_size: int = 500
    request_delay: float = 0.1
    timeout: float = 60.0


class WikidataIngestConfig(BaseModel):
    """Configuration for streaming Wikidata JSON ingestion."""

    dump_path: Path = Path("data/wikidata/latest-all.json.gz")
    batch_size: int = 1000


class ShortCircuitConfig(BaseModel):
    """High-confidence dense fast-path shortcut configuration."""

    enabled: bool = True
    threshold: float = 0.98

    @model_validator(mode="after")
    def validate_threshold(self) -> ShortCircuitConfig:
        if not (0.0 <= self.threshold <= 1.0):
            raise ValueError(
                f"short_circuit threshold must be between 0.0 and 1.0, got {self.threshold}"
            )
        return self


class SparseRerankConfig(BaseModel):
    """Configuration for lexical BM25 reranking and metadata discriminators."""

    enabled: bool = True
    dense_weight: float = 0.6
    sparse_weight: float = 0.4
    top_k: int = 50
    short_circuit: ShortCircuitConfig = Field(default_factory=ShortCircuitConfig)
    require_volume_match: bool = True
    require_surname_match: bool = True
    max_year_difference: int = 5
    year_penalty_threshold: int = 1

    @model_validator(mode="after")
    def validate_weights(self) -> SparseRerankConfig:
        if not (0.0 <= self.dense_weight <= 1.0):
            raise ValueError(f"dense_weight must be in [0.0, 1.0], got {self.dense_weight}")
        if not (0.0 <= self.sparse_weight <= 1.0):
            raise ValueError(f"sparse_weight must be in [0.0, 1.0], got {self.sparse_weight}")
        if round(self.dense_weight + self.sparse_weight, 5) != 1.0:
            raise ValueError(
                f"dense_weight and sparse_weight must sum to 1.0, got {self.dense_weight + self.sparse_weight}"
            )
        return self


class ReportSetPathsConfig(BaseModel):
    """Paths for detailed and summary TSV reports."""

    detailed_file: Path
    summary_file: Path


class ReconcileConfig(BaseModel):
    """Offline reconciliation and enrichment configuration."""

    target_file: Path
    target_format: Literal["auto", "marcxml", "bibra"] = "auto"
    output_file: Optional[Path] = None
    output_format: Literal["auto", "marcxml", "bibra"] = "auto"
    agent_reports: ReportSetPathsConfig
    subject_reports: ReportSetPathsConfig
    enrich: bool = True
    fold_legacy: bool = True
    threshold: float = 0.85
    sparse_rerank: SparseRerankConfig = Field(default_factory=SparseRerankConfig)
    link_policy: Literal["priority_fallback", "all"] = "priority_fallback"
    vocabularies: list[str] = Field(
        default_factory=lambda: [
            "KANTO",
            "YSO",
            "MUSO",
            "KAUNO",
            "SLM",
            "SEKO",
            "VIAF",
            "ISNI",
            "WIKIDATA",
        ]
    )
    agent_priority: list[str] = Field(default_factory=lambda: ["KANTO", "ISNI", "VIAF", "WIKIDATA"])
    subject_priority: list[str] = Field(
        default_factory=lambda: ["YSO", "KAUNO", "MUSO", "SLM", "SEKO", "WIKIDATA"]
    )

    @model_validator(mode="after")
    def validate_threshold(self) -> ReconcileConfig:
        if not (0.0 <= self.threshold <= 1.0):
            raise ValueError(f"reconcile threshold must be in [0.0, 1.0], got {self.threshold}")
        return self


class TelemetryConfig(BaseModel):
    """Progress heartbeat and query monitoring intervals."""

    heartbeat_interval_seconds: float = 1.0
    watch_interval_seconds: float = 1.0


class AppConfig(BaseModel):
    """Root configuration object combining all subsystems."""

    storage: StorageConfig = Field(default_factory=StorageConfig)
    embedding: EmbeddingConfig = Field(default_factory=EmbeddingConfig)
    download_finto: Optional[FintoDownloadConfig] = None
    ingest_finto: Optional[FintoIngestConfig] = None
    ingest_oai: dict[str, OAIIngestConfig] = Field(default_factory=dict)
    ingest_wikidata: Optional[WikidataIngestConfig] = None
    reconcile: Optional[ReconcileConfig] = None
    telemetry: TelemetryConfig = Field(default_factory=TelemetryConfig)


def _resolve_paths(data: Any, base_dir: Path) -> Any:
    """Recursively resolve relative Path instances in config data against base_dir."""
    if isinstance(data, dict):
        return {k: _resolve_paths(v, base_dir) for k, v in data.items()}
    elif isinstance(data, list):
        return [_resolve_paths(item, base_dir) for item in data]
    return data


def _resolve_config_paths(config: AppConfig, base_dir: Path) -> AppConfig:
    """Resolve all relative paths in AppConfig relative to base_dir."""

    def resolve_p(p: Optional[Path]) -> Optional[Path]:
        if p is None:
            return None
        if p.is_absolute():
            return p
        return (base_dir / p).resolve()

    # Storage paths
    config.storage.authority_db = resolve_p(config.storage.authority_db)  # type: ignore[assignment]
    config.storage.finto_db = resolve_p(config.storage.finto_db)  # type: ignore[assignment]
    config.storage.progress_db = resolve_p(config.storage.progress_db)  # type: ignore[assignment]
    config.storage.index_path = resolve_p(config.storage.index_path)  # type: ignore[assignment]

    # Embedding template
    if config.embedding.template_path:
        t_path = config.embedding.template_path
        if not t_path.is_absolute():
            # Check if relative to base_dir
            candidate = (base_dir / t_path).resolve()
            if candidate.exists():
                config.embedding.template_path = candidate
            else:
                # Check package bundled template
                pkg_template = Path(__file__).parent / "templates" / t_path.name
                if pkg_template.exists():
                    config.embedding.template_path = pkg_template
                else:
                    config.embedding.template_path = candidate

    # Download configs
    if config.download_finto and config.download_finto.output_dir:
        config.download_finto.output_dir = resolve_p(config.download_finto.output_dir)  # type: ignore[assignment]

    # Ingestion configs
    if config.ingest_finto and config.ingest_finto.input_path:
        config.ingest_finto.input_path = resolve_p(config.ingest_finto.input_path)  # type: ignore[assignment]

    for oai_cfg in config.ingest_oai.values():
        if oai_cfg.input_path:
            oai_cfg.input_path = resolve_p(oai_cfg.input_path)

    if config.ingest_wikidata and config.ingest_wikidata.dump_path:
        config.ingest_wikidata.dump_path = resolve_p(config.ingest_wikidata.dump_path)  # type: ignore[assignment]

    # Reconciliation paths
    if config.reconcile:
        config.reconcile.target_file = resolve_p(config.reconcile.target_file)  # type: ignore[assignment]
        if config.reconcile.output_file:
            config.reconcile.output_file = resolve_p(config.reconcile.output_file)
        config.reconcile.agent_reports.detailed_file = resolve_p(  # type: ignore[assignment]
            config.reconcile.agent_reports.detailed_file
        )
        config.reconcile.agent_reports.summary_file = resolve_p(  # type: ignore[assignment]
            config.reconcile.agent_reports.summary_file
        )
        config.reconcile.subject_reports.detailed_file = resolve_p(  # type: ignore[assignment]
            config.reconcile.subject_reports.detailed_file
        )
        config.reconcile.subject_reports.summary_file = resolve_p(  # type: ignore[assignment]
            config.reconcile.subject_reports.summary_file
        )

    return config


def load_config(config_path: Path | str) -> AppConfig:
    """Load, validate, and resolve paths for a Reconciliator YAML configuration file."""
    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"Configuration file not found: {path}")

    with path.open("r", encoding="utf-8") as f:
        try:
            raw_data = yaml.safe_load(f) or {}
        except yaml.YAMLError as e:
            raise ValueError(f"YAML parsing error in {path}: {e}") from e

    config = AppConfig.model_validate(raw_data)
    return _resolve_config_paths(config, path.parent)
