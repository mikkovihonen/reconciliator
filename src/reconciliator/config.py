"""Unified configuration models and YAML loader for Reconciliator."""

from __future__ import annotations

import functools
from pathlib import Path
from typing import Any, Final, Literal, Optional

import yaml
from pydantic import BaseModel, Field, model_validator


class VectorEngineConfig(BaseModel):
    """Vector search engine backend and hardware acceleration configuration."""

    backend: Literal["auto", "faiss", "torch"] = "auto"
    device: Literal["auto", "mps", "cuda", "cpu"] = "auto"
    threads: Optional[int] = None


class StorageConfig(BaseModel):
    """File paths for databases and vector indices."""

    authority_db: Path = Path("data/authority.db")
    vocab_db: Path = Path("data/vocab.db")
    progress_db: Path = Path("data/progress.db")
    index_path: Path = Path("data/authority.index")
    vector_engine: VectorEngineConfig = Field(default_factory=VectorEngineConfig)


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


_BUNDLED_NORMALIZATION_YAML: Final[Path] = (
    Path(__file__).parent / "defaults" / "default_normalization.yaml"
)


@functools.lru_cache(maxsize=1)
def get_bundled_normalization_data() -> dict[str, Any]:
    """Load default normalization configuration from package-bundled YAML resource."""
    with _BUNDLED_NORMALIZATION_YAML.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if "normalization" in data and isinstance(data["normalization"], dict):
        return data["normalization"]
    return data


def get_bundled_record_defaults() -> dict[str, Any]:
    """Get bundled default record normalization dictionary."""
    return dict(get_bundled_normalization_data().get("record") or {})


def get_bundled_vocab_defaults() -> dict[str, Any]:
    """Get bundled default vocabulary normalization dictionary."""
    return dict(get_bundled_normalization_data().get("vocab") or {})


_REC_DEFAULTS = get_bundled_record_defaults()
_VOCAB_DEFAULTS = get_bundled_vocab_defaults()

DEFAULT_NON_FILING_ARTICLES: dict[str, list[str]] = _REC_DEFAULTS["non_filing_articles"]
DEFAULT_PUBLISHER_SUFFIXES: list[str] = _REC_DEFAULTS["publisher_suffixes"]
DEFAULT_VOLUME_PREFIXES: list[str] = _REC_DEFAULTS["volume_prefixes"]
DEFAULT_VOLUME_SUFFIXES: list[str] = _REC_DEFAULTS["volume_suffixes"]
DEFAULT_VOLUME_FORMAT_TEMPLATE: str = _REC_DEFAULTS["volume_format_template"]
DEFAULT_ROMAN_NUMERALS: dict[str, int] = _REC_DEFAULTS["roman_numerals"]
DEFAULT_FICTIONAL_QUALIFIERS: list[str] = _REC_DEFAULTS["fictional_qualifiers"]
DEFAULT_RESPONSIBILITY_PREFIXES: list[str] = _REC_DEFAULTS["responsibility_prefixes"]
DEFAULT_RESPONSIBILITY_CONJUNCTIONS: list[str] = _REC_DEFAULTS["responsibility_conjunctions"]
DEFAULT_CLASSIFICATION_PRIORITY: list[str] = _REC_DEFAULTS["classification_priority"]
DEFAULT_RELATOR_MAPPINGS: dict[str, str] = _REC_DEFAULTS["relator_mappings"]
DEFAULT_PRESERVED_DIACRITICS: Final[str] = _REC_DEFAULTS["preserved_diacritics"]
DEFAULT_BIOGRAPHICAL_DATE_PREFIXES: Final[list[str]] = _REC_DEFAULTS["biographical_date_prefixes"]
DEFAULT_BIOGRAPHICAL_ERA_BC: Final[list[str]] = _REC_DEFAULTS["biographical_era_bc"]
DEFAULT_BIOGRAPHICAL_ERA_AD: Final[list[str]] = _REC_DEFAULTS["biographical_era_ad"]
DEFAULT_BIOGRAPHICAL_CENTURY_SUFFIXES: Final[list[str]] = _REC_DEFAULTS[
    "biographical_century_suffixes"
]


class RecordNormalizationConfig(BaseModel):
    """Configuration for bibliographic record normalization rules."""

    non_filing_articles: dict[str, list[str]] = Field(
        default_factory=lambda: {k: list(v) for k, v in DEFAULT_NON_FILING_ARTICLES.items()}
    )
    publisher_suffixes: list[str] = Field(default_factory=lambda: list(DEFAULT_PUBLISHER_SUFFIXES))
    volume_prefixes: list[str] = Field(default_factory=lambda: list(DEFAULT_VOLUME_PREFIXES))
    volume_suffixes: list[str] = Field(default_factory=lambda: list(DEFAULT_VOLUME_SUFFIXES))
    volume_format_template: str = DEFAULT_VOLUME_FORMAT_TEMPLATE
    roman_numerals: dict[str, int] = Field(default_factory=lambda: dict(DEFAULT_ROMAN_NUMERALS))
    fictional_qualifiers: list[str] = Field(
        default_factory=lambda: list(DEFAULT_FICTIONAL_QUALIFIERS)
    )
    responsibility_prefixes: list[str] = Field(
        default_factory=lambda: list(DEFAULT_RESPONSIBILITY_PREFIXES)
    )
    responsibility_conjunctions: list[str] = Field(
        default_factory=lambda: list(DEFAULT_RESPONSIBILITY_CONJUNCTIONS)
    )
    classification_priority: list[str] = Field(
        default_factory=lambda: list(DEFAULT_CLASSIFICATION_PRIORITY)
    )
    default_classification_scheme: str = "ykl"
    control_number_prefix: Optional[str] = None
    control_number_pattern: Optional[str] = None
    control_number_padding_digits: Optional[int] = None
    control_number_strip_prefixes: Optional[list[str]] = None
    urn_pattern: str = r"(urn:nbn:[^\s]+)"
    preserved_diacritics: str = DEFAULT_PRESERVED_DIACRITICS
    biographical_date_prefixes: list[str] = Field(
        default_factory=lambda: list(DEFAULT_BIOGRAPHICAL_DATE_PREFIXES)
    )
    biographical_era_bc: list[str] = Field(
        default_factory=lambda: list(DEFAULT_BIOGRAPHICAL_ERA_BC)
    )
    biographical_era_ad: list[str] = Field(
        default_factory=lambda: list(DEFAULT_BIOGRAPHICAL_ERA_AD)
    )
    biographical_century_suffixes: list[str] = Field(
        default_factory=lambda: list(DEFAULT_BIOGRAPHICAL_CENTURY_SUFFIXES)
    )
    biographical_date_pattern: Optional[str] = None
    relator_mappings: dict[str, str] = Field(default_factory=lambda: dict(DEFAULT_RELATOR_MAPPINGS))
    custom_relator_terms: dict[str, str] = Field(default_factory=dict)
    default_language: str = "fi"


class VocabNormalizationConfig(BaseModel):
    """Configuration for vocabulary normalization and folding rules."""

    legacy_to_modern: dict[str, str] = Field(
        default_factory=lambda: dict(_VOCAB_DEFAULTS.get("legacy_to_modern", {}))
    )
    thesaurus_languages: dict[str, str] = Field(
        default_factory=lambda: dict(_VOCAB_DEFAULTS.get("thesaurus_languages", {}))
    )
    language_subcode_vocabularies: dict[str, str] = Field(
        default_factory=lambda: dict(_VOCAB_DEFAULTS.get("language_subcode_vocabularies", {}))
    )
    field_to_vocabularies: dict[str, list[str]] = Field(
        default_factory=lambda: {
            k: list(v) for k, v in _VOCAB_DEFAULTS.get("field_to_vocabularies", {}).items()
        }
    )
    default_field_vocabularies: dict[str, str] = Field(
        default_factory=lambda: dict(_VOCAB_DEFAULTS.get("default_field_vocabularies", {}))
    )
    preserved_diacritics: str = _VOCAB_DEFAULTS.get(
        "preserved_diacritics", DEFAULT_PRESERVED_DIACRITICS
    )
    biographical_date_prefixes: list[str] = Field(
        default_factory=lambda: list(_VOCAB_DEFAULTS.get("biographical_date_prefixes", []))
    )
    biographical_era_bc: list[str] = Field(
        default_factory=lambda: list(_VOCAB_DEFAULTS.get("biographical_era_bc", []))
    )
    biographical_era_ad: list[str] = Field(
        default_factory=lambda: list(_VOCAB_DEFAULTS.get("biographical_era_ad", []))
    )
    biographical_century_suffixes: list[str] = Field(
        default_factory=lambda: list(_VOCAB_DEFAULTS.get("biographical_century_suffixes", []))
    )
    biographical_date_pattern: Optional[str] = _VOCAB_DEFAULTS.get("biographical_date_pattern")
    default_language: str = _VOCAB_DEFAULTS.get("default_language", "fi")


class NormalizationConfig(BaseModel):
    """Container for record and vocabulary normalization defaults."""

    record: RecordNormalizationConfig = Field(default_factory=RecordNormalizationConfig)
    vocab: VocabNormalizationConfig = Field(default_factory=VocabNormalizationConfig)


class SkosmosDownloadConfig(BaseModel):
    """Configuration for downloading value vocabularies from a Skosmos REST instance."""

    enabled: bool = True
    api_url: str = "https://api.finto.fi/rest/v1"
    output_dir: Path = Path("data/vocabularies")
    vocabularies: list[str] = Field(
        default_factory=lambda: ["yso", "yso-paikat", "slm", "kauno", "muso", "seko"]
    )
    dump_urls: dict[str, str] = Field(default_factory=dict)
    format: str = "text/turtle"
    timeout_seconds: float = 120.0
    overwrite: bool = False
    normalization: Optional[VocabNormalizationConfig] = None


class VocabIngestConfig(BaseModel):
    """Configuration for value vocabulary and migration thesauri ingestion."""

    input_path: Path = Path("data/vocabularies")
    vocabularies: list[str] = Field(
        default_factory=lambda: ["yso", "yso-paikat", "kauno", "muso", "slm", "seko"]
    )
    legacy_thesauri: list[str] = Field(
        default_factory=lambda: ["ysa", "allars", "kaunokki", "musa"]
    )
    normalization: Optional[VocabNormalizationConfig] = None


class OAIIngestConfig(BaseModel):
    """Configuration for an individual OAI-PMH, ResourceSync, or file authority source."""

    enabled: bool = True
    source_type: Literal["oai", "file", "resourcesync"] = "oai"
    format: Literal["marcxml", "bibframe"] = "marcxml"
    schema_profile: Optional[str] = None
    input_path: Optional[Path] = None
    oai_url: Optional[str] = None
    resourcesync_url: Optional[str] = None
    metadata_prefix: str = "marc21"
    set_spec: Optional[str] = None
    id_source: Literal["marc_001", "oai_identifier", "auto"] = "marc_001"
    workers: int = 4
    from_date: Optional[str] = None
    until_date: Optional[str] = None
    slice_days: int = 30
    batch_size: int = 500
    checkpoint_interval: int = 50000
    normalization_workers: int = 4
    queue_size: Optional[int] = None
    batch_queue_size: int = 4
    request_delay: float = 0.1
    timeout: float = 60.0
    normalization: Optional[RecordNormalizationConfig] = None

    @model_validator(mode="after")
    def validate_workers_and_queues(self) -> OAIIngestConfig:
        if self.workers < 1:
            raise ValueError(f"workers must be at least 1, got {self.workers}")
        if self.normalization_workers < 1:
            raise ValueError(
                f"normalization_workers must be at least 1, got {self.normalization_workers}"
            )
        if self.batch_queue_size < 1:
            raise ValueError(f"batch_queue_size must be at least 1, got {self.batch_queue_size}")
        if self.queue_size is not None and self.queue_size < 1:
            raise ValueError(f"queue_size must be at least 1, got {self.queue_size}")
        return self


class WikidataIngestConfig(BaseModel):
    """Configuration for streaming Wikidata JSON ingestion."""

    dump_path: Path = Path("data/wikidata/latest-all.json.gz")
    batch_size: int = 1000
    normalization: Optional[RecordNormalizationConfig] = None


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


RerankEngineType = Literal["bm25", "cross_encoder", "fuzzy", "noop"]


class RerankConfig(BaseModel):
    """Configuration for candidate reranking and metadata discriminators."""

    enabled: bool = True
    engine: RerankEngineType = "bm25"
    model_name: Optional[str] = None
    device: Literal["auto", "cpu", "cuda", "mps"] = "auto"
    batch_size: int = 16
    dense_weight: float = 0.6
    rerank_weight: float = 0.4
    top_k: int = 50
    short_circuit: ShortCircuitConfig = Field(default_factory=ShortCircuitConfig)
    require_volume_match: bool = True
    require_surname_match: bool = True
    max_year_difference: int = 5
    year_penalty_threshold: int = 1

    @model_validator(mode="after")
    def validate_weights(self) -> RerankConfig:
        if not (0.0 <= self.dense_weight <= 1.0):
            raise ValueError(f"dense_weight must be in [0.0, 1.0], got {self.dense_weight}")
        if not (0.0 <= self.rerank_weight <= 1.0):
            raise ValueError(f"rerank_weight must be in [0.0, 1.0], got {self.rerank_weight}")
        if round(self.dense_weight + self.rerank_weight, 5) != 1.0:
            raise ValueError(
                f"dense_weight and rerank_weight must sum to 1.0, got {self.dense_weight + self.rerank_weight}"
            )
        return self


class ReportSetPathsConfig(BaseModel):
    """Paths for detailed and summary TSV reports."""

    detailed_file: Path
    summary_file: Path


class ReconcileConfig(BaseModel):
    """Offline reconciliation and enrichment configuration."""

    target_file: Path
    format: Literal["auto", "marcxml", "bibra", "bibframe"] = "auto"
    schema_profile: Optional[str] = None
    output_file: Optional[Path] = None
    agent_reports: ReportSetPathsConfig
    subject_reports: ReportSetPathsConfig
    enrich: bool = True
    fold_legacy: bool = True
    threshold: float = 0.85
    rerank: RerankConfig = Field(default_factory=RerankConfig)
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
    normalization: Optional[RecordNormalizationConfig] = None

    @model_validator(mode="before")
    @classmethod
    def _migrate_format_fields(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if "target_format" in data and "format" not in data:
                data["format"] = data["target_format"]
        return data

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
    download_skosmos: dict[str, SkosmosDownloadConfig] = Field(default_factory=dict)
    ingest_vocab: Optional[VocabIngestConfig] = None
    ingest_oai: dict[str, OAIIngestConfig] = Field(default_factory=dict)
    ingest_wikidata: Optional[WikidataIngestConfig] = None
    reconcile: Optional[ReconcileConfig] = None
    telemetry: TelemetryConfig = Field(default_factory=TelemetryConfig)
    normalization: NormalizationConfig = Field(default_factory=NormalizationConfig)


def _resolve_paths(data: Any, base_dir: Path) -> Any:
    """Recursively resolve relative Path instances in config data against base_dir."""
    if isinstance(data, dict):
        return {k: _resolve_paths(v, base_dir) for k, v in data.items()}
    elif isinstance(data, list):
        return [_resolve_paths(item, base_dir) for item in data]
    return data


def _get_source_stem(p: Path) -> str:
    name = p.name
    for suffix in [".example.yaml", ".example.yml", ".yaml", ".yml"]:
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return p.stem


def _load_source_file(file_path: Path) -> dict[str, Any]:
    with file_path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ValueError(
            f"Expected mapping in source config file {file_path}, got {type(data).__name__}"
        )
    return data


def _resolve_source_entry(
    val: Any, base_dir: Path, default_key: Optional[str] = None
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    if isinstance(val, str):
        target = (base_dir / val).resolve() if not Path(val).is_absolute() else Path(val)
        if target.is_dir():
            files = sorted(list(target.glob("*.yaml")) + list(target.glob("*.yml")))
            for f in files:
                loaded = _load_source_file(f)
                if (
                    len(loaded) == 1
                    and isinstance(next(iter(loaded.values())), dict)
                    and not any(
                        k in loaded
                        for k in ("enabled", "source_type", "api_url", "output_dir", "input_path")
                    )
                ):
                    result.update(loaded)
                else:
                    result[_get_source_stem(f)] = loaded
        elif "*" in val:
            for f in sorted(base_dir.glob(val)):
                loaded = _load_source_file(f)
                if (
                    len(loaded) == 1
                    and isinstance(next(iter(loaded.values())), dict)
                    and not any(
                        k in loaded
                        for k in ("enabled", "source_type", "api_url", "output_dir", "input_path")
                    )
                ):
                    result.update(loaded)
                else:
                    result[_get_source_stem(f)] = loaded
        elif target.is_file():
            loaded = _load_source_file(target)
            if (
                len(loaded) == 1
                and isinstance(next(iter(loaded.values())), dict)
                and not any(
                    k in loaded
                    for k in ("enabled", "source_type", "api_url", "output_dir", "input_path")
                )
            ):
                result.update(loaded)
            elif default_key:
                result[default_key] = loaded
            else:
                result[_get_source_stem(target)] = loaded
        else:
            raise FileNotFoundError(
                f"Source configuration file or directory not found: {val} (resolved to {target})"
            )
    elif isinstance(val, dict):
        if default_key and any(
            k in val for k in ("enabled", "source_type", "api_url", "output_dir", "input_path")
        ):
            result[default_key] = val
        else:
            result.update(val)

    for s_name, s_data in list(result.items()):
        if isinstance(s_data, dict) and "normalization" in s_data:
            if isinstance(s_data["normalization"], (str, Path)):
                s_data["normalization"] = _load_modular_normalization(
                    s_data["normalization"], base_dir
                )

    return result


def _load_modular_normalization(norm_val: Any, base_dir: Path) -> dict[str, Any]:
    """Load normalization settings from an external YAML file or return dict."""
    if isinstance(norm_val, (str, Path)):
        p = Path(norm_val)
        target = (base_dir / p).resolve() if not p.is_absolute() else p
        if not target.is_file():
            raise FileNotFoundError(
                f"Normalization configuration file not found: {norm_val} (resolved to {target})"
            )
        loaded = _load_source_file(target)
        if "normalization" in loaded and isinstance(loaded["normalization"], dict):
            return loaded["normalization"]
        return loaded
    elif isinstance(norm_val, dict):
        return norm_val
    return {}


def _load_modular_sources(section_data: Any, base_dir: Path) -> dict[str, Any]:
    if section_data is None:
        return {}
    if isinstance(section_data, str):
        return _resolve_source_entry(section_data, base_dir)
    elif isinstance(section_data, list):
        merged: dict[str, Any] = {}
        for item in section_data:
            merged.update(_resolve_source_entry(item, base_dir))
        return merged
    elif isinstance(section_data, dict):
        merged = {}
        for k, v in section_data.items():
            if isinstance(v, str):
                loaded = _resolve_source_entry(v, base_dir, default_key=k)
                merged.update(loaded)
            else:
                merged[k] = v
        return merged
    return dict(section_data) if isinstance(section_data, dict) else {}


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
    config.storage.vocab_db = resolve_p(config.storage.vocab_db)  # type: ignore[assignment]
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
    for skos_cfg in config.download_skosmos.values():
        if skos_cfg.output_dir:
            skos_cfg.output_dir = resolve_p(skos_cfg.output_dir)  # type: ignore[assignment]

    # Ingestion configs
    if config.ingest_vocab and config.ingest_vocab.input_path:
        config.ingest_vocab.input_path = resolve_p(config.ingest_vocab.input_path)  # type: ignore[assignment]

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

    base_dir = path.parent
    if "download_skosmos" in raw_data:
        raw_data["download_skosmos"] = _load_modular_sources(raw_data["download_skosmos"], base_dir)
    if "ingest_oai" in raw_data:
        raw_data["ingest_oai"] = _load_modular_sources(raw_data["ingest_oai"], base_dir)
    if "ingest_vocab" in raw_data and isinstance(raw_data["ingest_vocab"], dict):
        if (
            "normalization" in raw_data["ingest_vocab"]
            and raw_data["ingest_vocab"]["normalization"] is not None
        ):
            raw_data["ingest_vocab"]["normalization"] = _load_modular_normalization(
                raw_data["ingest_vocab"]["normalization"], base_dir
            )
    if "ingest_wikidata" in raw_data and isinstance(raw_data["ingest_wikidata"], dict):
        if (
            "normalization" in raw_data["ingest_wikidata"]
            and raw_data["ingest_wikidata"]["normalization"] is not None
        ):
            raw_data["ingest_wikidata"]["normalization"] = _load_modular_normalization(
                raw_data["ingest_wikidata"]["normalization"], base_dir
            )
    if "reconcile" in raw_data and isinstance(raw_data["reconcile"], dict):
        if (
            "normalization" in raw_data["reconcile"]
            and raw_data["reconcile"]["normalization"] is not None
        ):
            raw_data["reconcile"]["normalization"] = _load_modular_normalization(
                raw_data["reconcile"]["normalization"], base_dir
            )
    if "normalization" in raw_data and raw_data["normalization"] is not None:
        raw_data["normalization"] = _load_modular_normalization(raw_data["normalization"], base_dir)

    config = AppConfig.model_validate(raw_data)
    return _resolve_config_paths(config, base_dir)
