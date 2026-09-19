"""Unified configuration models and YAML loader for Reconciliator."""

from __future__ import annotations

import functools
import logging
import os
import sys
from pathlib import Path
from typing import Any, Final, Literal, Optional, Self, Union

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# ---------------------------------------------------------------------------
# Default Resources Loader
# ---------------------------------------------------------------------------

_BUNDLED_DEFAULTS_YAML: Final[Path] = Path(__file__).parent / "defaults" / "defaults.yaml"


@functools.lru_cache(maxsize=1)
def get_bundled_defaults() -> dict[str, Any]:
    """Load default configuration from package-bundled defaults.yaml."""
    if _BUNDLED_DEFAULTS_YAML.exists():
        with _BUNDLED_DEFAULTS_YAML.open("r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    return {}


def get_bundled_linguistic_defaults() -> dict[str, Any]:
    """Get bundled default linguistic rules dictionary."""
    return dict(get_bundled_defaults().get("linguistic") or {})


def get_bundled_sources_defaults() -> dict[str, Any]:
    """Get bundled default source definitions dictionary."""
    return dict(get_bundled_defaults().get("sources") or {})


def get_bundled_vocabularies_defaults() -> dict[str, Any]:
    """Get bundled default vocabulary definitions dictionary with derived mappings."""
    raw = dict(get_bundled_defaults().get("vocabularies") or {})
    sf = raw.get("subject_fields") or {}
    if "field_to_vocabularies" not in raw:
        raw["field_to_vocabularies"] = {k: list(v.get("allowed_vocabs", [])) for k, v in sf.items()}
    if "default_field_vocabularies" not in raw:
        raw["default_field_vocabularies"] = {
            k: str(v.get("default_vocab", "")) for k, v in sf.items()
        }
    if "vocab_target_fields" not in raw:
        raw["vocab_target_fields"] = {
            str(v.get("default_vocab", "")): k
            for k, v in sf.items()
            if v.get("category") != "topical"
        }
    if "subject_field_category_map" not in raw:
        raw["subject_field_category_map"] = {k: str(v.get("category", "")) for k, v in sf.items()}
    if "default_vocab_target_field" not in raw:
        raw["default_vocab_target_field"] = str(raw.get("default_vocab_target_field") or "")
    if "default_target_field" not in raw:
        raw["default_target_field"] = str(
            raw.get("default_target_field") or raw.get("default_vocab_target_field") or ""
        )
    if "default_subject_field" not in raw:
        raw["default_subject_field"] = str(
            raw.get("default_subject_field") or raw.get("default_vocab_target_field") or ""
        )
    if "supported_subject_vocabularies" not in raw:
        raw["supported_subject_vocabularies"] = list(raw.get("supported_subjects", []))
    if "vocabularies" not in raw:
        seen = set()
        vocabs = []
        for key in ("agent_priority", "subject_priority", "entity_vocabularies"):
            for val in raw.get(key, []):
                v_up = str(val).upper()
                if v_up not in seen:
                    seen.add(v_up)
                    vocabs.append(v_up)
        raw["vocabularies"] = vocabs
    ling = get_bundled_linguistic_defaults()
    if "preserved_diacritics" not in raw:
        raw["preserved_diacritics"] = str(ling.get("preserved_diacritics") or "")
    b_dates = ling.get("biographical_dates", {})
    if "biographical_date_prefixes" not in raw:
        raw["biographical_date_prefixes"] = list(b_dates.get("prefixes", []))
    if "biographical_era_bc" not in raw:
        raw["biographical_era_bc"] = list(b_dates.get("era_bc", []))
    if "biographical_era_ad" not in raw:
        raw["biographical_era_ad"] = list(b_dates.get("era_ad", []))
    if "biographical_century_suffixes" not in raw:
        raw["biographical_century_suffixes"] = list(b_dates.get("century_suffixes", []))
    if "biographical_date_pattern" not in raw:
        raw["biographical_date_pattern"] = b_dates.get("pattern")
    if "default_language" not in raw:
        raw["default_language"] = str(
            raw.get("default_language") or ling.get("default_language") or ""
        )
    return raw


def get_bundled_record_defaults() -> dict[str, Any]:
    """Get bundled default record normalization dictionary with enrichments and linguistic defaults."""
    data = get_bundled_defaults()
    raw = dict(data.get("record_normalization") or data.get("record") or {})
    enrich = dict(data.get("enrichment") or {})
    if "marc_predicate_profiles" not in raw and "predicate_profiles" in enrich:
        raw["marc_predicate_profiles"] = enrich["predicate_profiles"]
    if "marc_schemes" not in raw and "schemes" in enrich:
        raw["marc_schemes"] = enrich["schemes"]
    if "marc_domain_concepts" not in raw and "domain_concepts" in enrich:
        raw["marc_domain_concepts"] = enrich["domain_concepts"]
    if "marc_linking_labels" not in raw and "linking_labels" in enrich:
        raw["marc_linking_labels"] = enrich["linking_labels"]
    if "marc_accessibility" not in raw and "accessibility" in enrich:
        raw["marc_accessibility"] = enrich["accessibility"]
    if "thesis_institution_uri" not in raw and "thesis_institution_uri" in enrich:
        raw["thesis_institution_uri"] = enrich["thesis_institution_uri"]
    if "thesis_relator_code" not in raw and "thesis_relator_code" in enrich:
        raw["thesis_relator_code"] = enrich["thesis_relator_code"]
    if "surrogate_title_keywords" not in raw and "surrogate_title_keywords" in enrich:
        raw["surrogate_title_keywords"] = enrich["surrogate_title_keywords"]
    if "ephemera_terms" not in raw and "ephemera_terms" in enrich:
        raw["ephemera_terms"] = enrich["ephemera_terms"]

    ling = get_bundled_linguistic_defaults()
    if "preserved_diacritics" not in raw:
        raw["preserved_diacritics"] = str(ling.get("preserved_diacritics") or "")
    b_dates = ling.get("biographical_dates", {})
    if "biographical_date_prefixes" not in raw:
        raw["biographical_date_prefixes"] = list(b_dates.get("prefixes", []))
    if "biographical_era_bc" not in raw:
        raw["biographical_era_bc"] = list(b_dates.get("era_bc", []))
    if "biographical_era_ad" not in raw:
        raw["biographical_era_ad"] = list(b_dates.get("era_ad", []))
    if "biographical_century_suffixes" not in raw:
        raw["biographical_century_suffixes"] = list(b_dates.get("century_suffixes", []))
    if "biographical_date_pattern" not in raw:
        raw["biographical_date_pattern"] = b_dates.get("pattern")
    if "default_language" not in raw:
        raw["default_language"] = str(ling.get("default_language") or "")

    vocab = dict(data.get("vocabularies") or {})
    sf = vocab.get("subject_fields") or {}
    if "subject_field_category_map" not in raw:
        raw["subject_field_category_map"] = {k: str(v.get("category", "")) for k, v in sf.items()}
    if "default_subject_field" not in raw:
        raw["default_subject_field"] = str(
            vocab.get("default_subject_field") or vocab.get("default_vocab_target_field") or ""
        )
    return raw


def get_bundled_enrichment_defaults() -> dict[str, Any]:
    """Get bundled default enrichment settings dictionary."""
    return dict(get_bundled_defaults().get("enrichment") or {})


def get_bundled_section_defaults(section: str) -> dict[str, Any]:
    """Get bundled default settings dictionary for a top-level section."""
    return dict(get_bundled_defaults().get(section) or {})


# Backward-compatible accessors
def get_bundled_vocab_defaults() -> dict[str, Any]:
    return get_bundled_vocabularies_defaults()


_LING_DEFAULTS = get_bundled_linguistic_defaults()
_VOCAB_DEFAULTS = get_bundled_vocabularies_defaults()
_REC_DEFAULTS = get_bundled_record_defaults()
_ENRICH_DEFAULTS = get_bundled_enrichment_defaults()
_COMPUTE_DEFAULTS = get_bundled_section_defaults("compute")
_STORAGE_DEFAULTS = get_bundled_section_defaults("storage")
_EMBED_DEFAULTS = get_bundled_section_defaults("embedding")
_RECONCILE_DEFAULTS = get_bundled_section_defaults("reconcile")
_CLUSTER_DEFAULTS = dict(_RECONCILE_DEFAULTS.get("cluster") or {})
_CONSOLIDATION_DEFAULTS = get_bundled_section_defaults("consolidation")
_TELEMETRY_DEFAULTS = get_bundled_section_defaults("telemetry")
_LOGGING_DEFAULTS = get_bundled_section_defaults("logging")
_CONSTITUENTS_DEFAULTS = get_bundled_section_defaults("constituents")
_FUNNELS_DEFAULTS = get_bundled_section_defaults("funnels")
_AGENT_CLUSTERING_DEFAULTS = get_bundled_section_defaults("agent_clustering")
_PG_DEFAULTS = dict(_STORAGE_DEFAULTS.get("postgres") or {})


def get_bundled_agent_clustering_defaults() -> dict[str, Any]:
    """Get bundled default agent clustering settings dictionary."""
    return dict(_AGENT_CLUSTERING_DEFAULTS)


def get_bundled_funnels_defaults() -> dict[str, Any]:
    """Get bundled default funnels settings dictionary."""
    return dict(_FUNNELS_DEFAULTS)


def get_bundled_postgres_defaults() -> dict[str, Any]:
    """Get bundled default PostgreSQL storage settings dictionary."""
    return dict(_PG_DEFAULTS)


def get_default_database_url() -> str:
    """Get default PostgreSQL database URL constructed from bundled configuration defaults."""
    env_url = os.environ.get("RECONCILIATOR_DATABASE_URL")
    if env_url:
        return env_url
    cfg_url = _PG_DEFAULTS.get("database_url")
    if cfg_url:
        return cfg_url
    user = _PG_DEFAULTS.get("user", "postgres")
    password = _PG_DEFAULTS.get("password", "postgres")
    host = _PG_DEFAULTS.get("host", "localhost")
    port = _PG_DEFAULTS.get("port", 5432)
    database = _PG_DEFAULTS.get("database", "reconciliator")
    return f"postgresql+psycopg://{user}:{password}@{host}:{port}/{database}"


DEFAULT_LANGUAGE: str = str(_LING_DEFAULTS.get("default_language") or "")
DEFAULT_PRESERVED_DIACRITICS: str = str(_LING_DEFAULTS.get("preserved_diacritics") or "")
DEFAULT_BIOGRAPHICAL_DATES: dict[str, Any] = dict(_LING_DEFAULTS.get("biographical_dates") or {})
DEFAULT_BIOGRAPHICAL_DATE_PREFIXES: list[str] = list(
    DEFAULT_BIOGRAPHICAL_DATES.get("prefixes") or []
)
DEFAULT_BIOGRAPHICAL_ERA_BC: list[str] = list(DEFAULT_BIOGRAPHICAL_DATES.get("era_bc") or [])
DEFAULT_BIOGRAPHICAL_ERA_AD: list[str] = list(DEFAULT_BIOGRAPHICAL_DATES.get("era_ad") or [])
DEFAULT_BIOGRAPHICAL_CENTURY_SUFFIXES: list[str] = list(
    DEFAULT_BIOGRAPHICAL_DATES.get("century_suffixes") or []
)

DEFAULT_NON_FILING_ARTICLES: dict[str, list[str]] = dict(
    _REC_DEFAULTS.get("non_filing_articles") or {}
)
DEFAULT_PUBLISHER_SUFFIXES: list[str] = list(_REC_DEFAULTS.get("publisher_suffixes") or [])
DEFAULT_VOLUME_PREFIXES: list[str] = list(_REC_DEFAULTS.get("volume_prefixes") or [])
DEFAULT_VOLUME_SUFFIXES: list[str] = list(_REC_DEFAULTS.get("volume_suffixes") or [])
DEFAULT_VOLUME_FORMAT_TEMPLATE: str = str(_REC_DEFAULTS.get("volume_format_template") or "")
DEFAULT_ROMAN_NUMERALS: dict[str, int] = dict(_REC_DEFAULTS.get("roman_numerals") or {})
DEFAULT_FICTIONAL_QUALIFIERS: list[str] = list(_REC_DEFAULTS.get("fictional_qualifiers") or [])
DEFAULT_RESPONSIBILITY_PREFIXES: list[str] = list(
    _REC_DEFAULTS.get("responsibility_prefixes") or []
)
DEFAULT_RESPONSIBILITY_CONJUNCTIONS: list[str] = list(
    _REC_DEFAULTS.get("responsibility_conjunctions") or []
)
DEFAULT_CLASSIFICATION_PRIORITY: list[str] = list(
    _REC_DEFAULTS.get("classification_priority") or []
)
DEFAULT_RELATOR_MAPPINGS: dict[str, str] = dict(_REC_DEFAULTS.get("relator_mappings") or {})

DEFAULT_SUBJECT_FIELDS: dict[str, dict[str, Any]] = dict(
    _VOCAB_DEFAULTS.get("subject_fields") or {}
)
DEFAULT_SUBJECT_FIELD_CATEGORY_MAP: dict[str, str] = {
    tag: str(data.get("category", "")) for tag, data in DEFAULT_SUBJECT_FIELDS.items()
}


def get_subject_field_category_map() -> dict[str, str]:
    """Load default MARC subject field to category name mapping."""
    return dict(DEFAULT_SUBJECT_FIELD_CATEGORY_MAP)


def get_category_default_field_map() -> dict[str, str]:
    """Load default category name to MARC subject field mapping."""
    return {cat: tag for tag, cat in DEFAULT_SUBJECT_FIELD_CATEGORY_MAP.items()}


def get_wikidata_author_properties() -> dict[str, str]:
    """Load default mapping of identifier schemes to Wikidata authority property IDs."""
    return dict(_VOCAB_DEFAULTS.get("wikidata_author_properties", {}))


# ---------------------------------------------------------------------------
# Compute and Acceleration Configuration
# ---------------------------------------------------------------------------


class ComputeConfig(BaseModel):
    """Hardware acceleration and compute parallelism configuration."""

    device: Literal["auto", "mps", "cuda", "cpu"] = "auto"
    threads: Optional[int] = None


# ---------------------------------------------------------------------------
# Vector Search Engine and Storage Models
# ---------------------------------------------------------------------------


class TorchEngineConfig(BaseModel):
    """PyTorch vector index engine configuration."""

    dtype: Literal["float32", "float16", "bfloat16"] = "float32"
    mmap: bool = False
    chunk_size: int = 50000
    initial_capacity: int = 10000


class FAISSEngineConfig(BaseModel):
    """FAISS vector index engine configuration."""

    index_type: Literal["flat", "sq8"] = "flat"
    mmap: bool = False
    nprobe: int = 16
    nlist: int = 2048


class USearchEngineConfig(BaseModel):
    """USearch vector index engine configuration."""

    metric: Literal["cos", "ip", "l2"] = "cos"
    dtype: Literal["f32", "f16", "i8", "b1"] = "f16"
    connectivity: int = 16
    expansion_add: int = 128
    expansion_search: int = 64
    mmap: bool = True


class VectorDaemonConfig(BaseModel):
    """Configuration for the singleton background vector service."""

    enabled: bool = True
    socket_path: Optional[Path] = None
    auto_start: bool = True
    idle_timeout_seconds: Optional[int] = 1800
    auto_save_interval_seconds: int = 60
    heartbeat_interval_seconds: float = 2.0
    max_batch_bytes: int = 67108864


class VectorEngineConfig(BaseModel):
    """Vector search engine backend and hardware acceleration configuration."""

    backend: Literal["auto", "faiss", "torch", "usearch"] = "auto"
    device: Literal["auto", "mps", "cuda", "cpu"] = "auto"
    mode: Literal["daemon", "embedded"] = "embedded"
    threads: Optional[int] = None
    torch: TorchEngineConfig = Field(default_factory=TorchEngineConfig)
    faiss: FAISSEngineConfig = Field(default_factory=FAISSEngineConfig)
    usearch: USearchEngineConfig = Field(default_factory=USearchEngineConfig)
    daemon: VectorDaemonConfig = Field(default_factory=VectorDaemonConfig)


class RepositoryFTSConfig(BaseModel):
    """Configuration for SQLite FTS5 lexical database search & ranking in WEMIRepository."""

    ranking_function: str = Field(
        default="bm25",
        pattern=r"^[a-zA-Z_][a-zA-Z0-9_]*$",
        description="SQLite FTS5 ranking function (e.g. bm25 or registered auxiliary function)",
    )
    column_weights: Optional[list[float]] = None
    default_limit: int = Field(default=20, ge=1)
    prefix_search: bool = False


class PostgresConfig(BaseModel):
    """PostgreSQL connection, engine tuning, and container configuration."""

    host: str = Field(default_factory=lambda: str(_PG_DEFAULTS.get("host", "localhost")))
    port: int = Field(default_factory=lambda: int(_PG_DEFAULTS.get("port", 5432)))
    database: str = Field(default_factory=lambda: str(_PG_DEFAULTS.get("database", "reconciliator")))
    user: str = Field(default_factory=lambda: str(_PG_DEFAULTS.get("user", "postgres")))
    password: str = Field(default_factory=lambda: str(_PG_DEFAULTS.get("password", "postgres")))
    database_url: Optional[str] = Field(default_factory=lambda: _PG_DEFAULTS.get("database_url"))

    # Connection pooling & timeouts
    pool_size: int = Field(default_factory=lambda: int(_PG_DEFAULTS.get("pool_size", 10)))
    max_overflow: int = Field(default_factory=lambda: int(_PG_DEFAULTS.get("max_overflow", 20)))
    pool_timeout: float = Field(default_factory=lambda: float(_PG_DEFAULTS.get("pool_timeout", 30.0)))
    pool_recycle: int = Field(default_factory=lambda: int(_PG_DEFAULTS.get("pool_recycle", 1800)))
    pool_pre_ping: bool = Field(default_factory=lambda: bool(_PG_DEFAULTS.get("pool_pre_ping", True)))

    # Performance tuning parameters
    shared_buffers: str = Field(default_factory=lambda: str(_PG_DEFAULTS.get("shared_buffers", "1GB")))
    work_mem: str = Field(default_factory=lambda: str(_PG_DEFAULTS.get("work_mem", "64MB")))
    maintenance_work_mem: str = Field(default_factory=lambda: str(_PG_DEFAULTS.get("maintenance_work_mem", "256MB")))
    effective_cache_size: str = Field(default_factory=lambda: str(_PG_DEFAULTS.get("effective_cache_size", "3GB")))
    random_page_cost: float = Field(default_factory=lambda: float(_PG_DEFAULTS.get("random_page_cost", 1.1)))
    synchronous_commit: str = Field(default_factory=lambda: str(_PG_DEFAULTS.get("synchronous_commit", "off")))

    # Container settings
    container_name: str = Field(default_factory=lambda: str(_PG_DEFAULTS.get("container_name", "reconciliator-pg")))
    container_image: str = Field(default_factory=lambda: str(_PG_DEFAULTS.get("container_image", "docker.io/library/postgres:16-alpine")))
    container_timeout_seconds: float = Field(default_factory=lambda: float(_PG_DEFAULTS.get("container_timeout_seconds", 30.0)))
    auto_start_container: bool = Field(default_factory=lambda: bool(_PG_DEFAULTS.get("auto_start_container", True)))
    locale: Optional[str] = Field(default_factory=lambda: _PG_DEFAULTS.get("locale"))
    lc_collate: Optional[str] = Field(default_factory=lambda: _PG_DEFAULTS.get("lc_collate"))
    lc_ctype: Optional[str] = Field(default_factory=lambda: _PG_DEFAULTS.get("lc_ctype"))
    initdb_args: Optional[str] = Field(default_factory=lambda: _PG_DEFAULTS.get("initdb_args"))

    # Batch execution limits
    max_parameters_per_batch: int = Field(default_factory=lambda: int(_PG_DEFAULTS.get("max_parameters_per_batch", 32767)))
    default_chunk_size: Optional[int] = Field(default_factory=lambda: _PG_DEFAULTS.get("default_chunk_size"))

    def get_database_url(self) -> str:
        if self.database_url:
            return self.database_url
        env_url = os.environ.get("RECONCILIATOR_DATABASE_URL")
        if env_url:
            return env_url
        return f"postgresql+psycopg://{self.user}:{self.password}@{self.host}:{self.port}/{self.database}"


class StorageConfig(BaseModel):
    """File paths for databases, vector indices, and backend engines."""

    data_dir: Path = Path("data")
    database_url: Optional[str] = None
    postgres: PostgresConfig = Field(default_factory=PostgresConfig)
    concept_index: Optional[Path] = None
    work_index: Optional[Path] = None
    manifestation_index: Optional[Path] = None
    agent_index: Optional[Path] = None
    vector_engine: VectorEngineConfig = Field(default_factory=VectorEngineConfig)
    fts: RepositoryFTSConfig = Field(default_factory=RepositoryFTSConfig)

    @model_validator(mode="after")
    def _resolve_defaults(self) -> Self:
        d = self.data_dir
        if self.database_url is None:
            base_url = (
                os.environ.get("RECONCILIATOR_DATABASE_URL") or self.postgres.get_database_url()
            )
            is_default_data = d == Path("data") or (
                d.name == "data" and d.resolve() == (Path.cwd() / "data").resolve()
            )
            if not is_default_data and "?" not in base_url:
                import hashlib

                import psycopg

                schema = f"test_{hashlib.md5(str(d.resolve()).encode()).hexdigest()[:10]}"
                raw_url = base_url.split("?")[0].replace(
                    "postgresql+psycopg://", "postgresql://", 1
                )
                try:
                    with psycopg.connect(raw_url) as conn:
                        with conn.cursor() as cur:
                            cur.execute(f"CREATE SCHEMA IF NOT EXISTS {schema};")
                except Exception:
                    pass
                self.database_url = f"{base_url}?options=-csearch_path%3D{schema},public"
            else:
                self.database_url = base_url
        if self.concept_index is None:
            self.concept_index = d / "concept.index"
        if self.work_index is None:
            self.work_index = d / "work.index"
        if self.manifestation_index is None:
            self.manifestation_index = d / "manifestation.index"
        if self.agent_index is None:
            self.agent_index = d / "agent.index"
        return self


# ---------------------------------------------------------------------------
# Embedding Configuration
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Linguistic Rules Configuration
# ---------------------------------------------------------------------------


class BiographicalDatesConfig(BaseModel):
    """Rules for parsing and stripping biographical birth/death date qualifiers."""

    prefixes: list[str] = Field(default_factory=lambda: list(DEFAULT_BIOGRAPHICAL_DATE_PREFIXES))
    era_bc: list[str] = Field(default_factory=lambda: list(DEFAULT_BIOGRAPHICAL_ERA_BC))
    era_ad: list[str] = Field(default_factory=lambda: list(DEFAULT_BIOGRAPHICAL_ERA_AD))
    century_suffixes: list[str] = Field(
        default_factory=lambda: list(DEFAULT_BIOGRAPHICAL_CENTURY_SUFFIXES)
    )
    pattern: Optional[str] = None


class LinguisticConfig(BaseModel):
    """Linguistic, diacritic, and biographical date normalization conventions."""

    default_language: str = DEFAULT_LANGUAGE
    preserved_diacritics: str = DEFAULT_PRESERVED_DIACRITICS
    biographical_dates: BiographicalDatesConfig = Field(default_factory=BiographicalDatesConfig)


# ---------------------------------------------------------------------------
# Controlled Vocabularies Configuration
# ---------------------------------------------------------------------------


class SubjectFieldDefinition(BaseModel):
    """Configuration for a MARC subject heading field tag and associated vocabularies."""

    category: str
    default_vocab: str
    allowed_vocabs: list[str] = Field(default_factory=list)


class SchemeDefinition(BaseModel):
    """MARC subfield $2 scheme configuration for emission and deduplication."""

    default: str
    accepted: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _coerce_input(cls, v: Any) -> Any:
        if isinstance(v, str):
            return {"default": v, "accepted": [v]}
        return v

    @model_validator(mode="after")
    def _ensure_default_in_accepted(self) -> Self:
        if self.default and self.default not in self.accepted:
            self.accepted.insert(0, self.default)
        return self


class SourceConfig(BaseModel):
    """Configuration for an authority or bibliographic source identifier / URI scheme."""

    uri_patterns: list[str] = Field(default_factory=list)
    canonical_uri_template: Optional[str] = None
    tag_prefixes: list[str] = Field(default_factory=list)
    aliases: list[str] = Field(default_factory=list)
    identifier_pattern: Optional[str] = None
    uppercase_identifier: bool = False
    subfield: str = "0"


class VocabulariesConfig(BaseModel):
    """Centralized controlled vocabulary definitions, priorities, and matching policies."""

    agent_priority: list[str] = Field(
        default_factory=lambda: list(_VOCAB_DEFAULTS.get("agent_priority", []))
    )
    subject_priority: list[str] = Field(
        default_factory=lambda: list(_VOCAB_DEFAULTS.get("subject_priority", []))
    )
    entity_vocabularies: list[str] = Field(
        default_factory=lambda: list(_VOCAB_DEFAULTS.get("entity_vocabularies", []))
    )
    supported_subjects: list[str] = Field(
        default_factory=lambda: list(_VOCAB_DEFAULTS.get("supported_subjects", []))
    )
    download_list: list[str] = Field(
        default_factory=lambda: list(_VOCAB_DEFAULTS.get("download_list", []))
    )
    legacy_to_modern: dict[str, str] = Field(
        default_factory=lambda: dict(_VOCAB_DEFAULTS.get("legacy_to_modern", {}))
    )
    thesaurus_languages: dict[str, str] = Field(
        default_factory=lambda: dict(_VOCAB_DEFAULTS.get("thesaurus_languages", {}))
    )
    language_subcode_vocabularies: dict[str, str] = Field(
        default_factory=lambda: dict(_VOCAB_DEFAULTS.get("language_subcode_vocabularies", {}))
    )
    language_subcode_suffixes: dict[str, str] = Field(
        default_factory=lambda: dict(_VOCAB_DEFAULTS.get("language_subcode_suffixes", {}))
    )

    # --- Subject heading rewriting policy -------------------------------------
    # Append the Finto language subcode to the $2 vocabulary code of the
    # vocabularies listed in language_subcode_vocabularies (yso -> yso/fin).
    # Set false to emit the bare vocabulary code instead.
    apply_language_subcode: bool = True
    # Replace a resolved heading's $a with the ontology's preferred label
    # (tohtorit -> lääkärit). Set false to leave the catalogued heading in place
    # while still injecting the $0 URI.
    replace_heading_with_pref_label: bool = True
    # Also keep the heading as catalogued, in original_heading_subfield, so the
    # record itself retains it rather than only the subject report.
    preserve_original_heading: bool = False
    original_heading_subfield: str = "9"
    preferred_label_languages: list[str] = Field(
        default_factory=lambda: list(_VOCAB_DEFAULTS.get("preferred_label_languages") or [])
    )
    default_language: str = Field(
        default_factory=lambda: str(
            _VOCAB_DEFAULTS.get("default_language") or _LING_DEFAULTS.get("default_language") or ""
        )
    )
    default_classification_scheme: str = Field(
        default_factory=lambda: str(
            get_bundled_defaults().get("classification", {}).get("default_scheme") or ""
        )
    )
    digital_repository_patterns: list[str] = Field(
        default_factory=lambda: list(
            get_bundled_defaults().get("digital_repositories", {}).get("uri_patterns") or []
        )
    )
    language_code_map: dict[str, str] = Field(
        default_factory=lambda: dict(_VOCAB_DEFAULTS.get("language_code_map", {}))
    )
    ingest_pref_label_languages: Optional[list[str]] = None
    ingest_alt_label_languages: Optional[list[str]] = None
    subject_fields: dict[str, SubjectFieldDefinition] = Field(
        default_factory=lambda: {
            str(k): SubjectFieldDefinition(**v) if isinstance(v, dict) else v
            for k, v in DEFAULT_SUBJECT_FIELDS.items()
        }
    )
    vocabulary_patterns: dict[str, list[str]] = Field(
        default_factory=lambda: {
            k: list(v) for k, v in _VOCAB_DEFAULTS.get("vocabulary_patterns", {}).items()
        }
    )
    concept_id_rules: dict[str, dict[str, Any]] = Field(
        default_factory=lambda: {
            k.upper(): dict(v) for k, v in _VOCAB_DEFAULTS.get("concept_id_rules", {}).items()
        }
    )
    wikidata_author_properties: dict[str, str] = Field(
        default_factory=lambda: dict(_VOCAB_DEFAULTS.get("wikidata_author_properties", {}))
    )

    default_vocab_target_field: str = Field(
        default_factory=lambda: str(_VOCAB_DEFAULTS.get("default_vocab_target_field") or "")
    )
    field_to_vocabularies: dict[str, list[str]] = Field(default_factory=dict)
    default_field_vocabularies: dict[str, str] = Field(default_factory=dict)
    vocab_target_fields: dict[str, str] = Field(default_factory=dict)
    subject_field_category_map: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _populate_derived_subject_fields(self) -> Self:
        if self.subject_fields:
            if not self.field_to_vocabularies:
                self.field_to_vocabularies = {
                    k: list(v.allowed_vocabs) for k, v in self.subject_fields.items()
                }
            if not self.default_field_vocabularies:
                self.default_field_vocabularies = {
                    k: v.default_vocab for k, v in self.subject_fields.items()
                }
            if not self.vocab_target_fields:
                self.vocab_target_fields = {
                    v.default_vocab: k
                    for k, v in self.subject_fields.items()
                    if v.category != "topical"
                }
            if not self.subject_field_category_map:
                self.subject_field_category_map = {
                    k: v.category for k, v in self.subject_fields.items()
                }
        return self

    @property
    def default_target_field(self) -> str:
        return self.default_vocab_target_field

    @property
    def default_subject_field(self) -> str:
        return self.default_vocab_target_field

    @property
    def supported_subject_vocabularies(self) -> list[str]:
        return self.supported_subjects

    @property
    def vocabularies(self) -> list[str]:
        seen: set[str] = set()
        res: list[str] = []
        for lst in (self.agent_priority, self.subject_priority, self.entity_vocabularies):
            for v in lst:
                v_up = v.upper()
                if v_up not in seen:
                    seen.add(v_up)
                    res.append(v_up)
        return res

    # Linguistic accessors for VocabFolder / VocabStore compatibility
    @property
    def preserved_diacritics(self) -> str:
        return DEFAULT_PRESERVED_DIACRITICS

    @property
    def biographical_date_prefixes(self) -> list[str]:
        return DEFAULT_BIOGRAPHICAL_DATE_PREFIXES

    @property
    def biographical_era_bc(self) -> list[str]:
        return DEFAULT_BIOGRAPHICAL_ERA_BC

    @property
    def biographical_era_ad(self) -> list[str]:
        return DEFAULT_BIOGRAPHICAL_ERA_AD

    @property
    def biographical_century_suffixes(self) -> list[str]:
        return DEFAULT_BIOGRAPHICAL_CENTURY_SUFFIXES

    @property
    def biographical_date_pattern(self) -> Optional[str]:
        return None


# Alias for backward compatibility
VocabNormalizationConfig = VocabulariesConfig


# ---------------------------------------------------------------------------
# Record Normalization Configuration
# ---------------------------------------------------------------------------


class RecordNormalizationConfig(BaseModel):
    """Configuration for bibliographic record parsing and normalization heuristics."""

    # ISBD presentation punctuation trailing a heading: "Waltari, Mika," ->
    # "Waltari, Mika". The authority decomposer always stripped it and the
    # bibliographic one never did, so the same person arrived in two forms
    # depending on which path ingested them (Phase 12's known discrepancy).
    # Stripping is the default because the punctuation is presentational. It does
    # not move any blocking key - compute_author_key and compute_title_stem
    # already discard punctuation - so it governs the stored and exported form.
    strip_isbd_punctuation: bool = Field(
        default_factory=lambda: bool(_REC_DEFAULTS.get("strip_isbd_punctuation", True))
    )
    # Characters treated as ISBD punctuation when stripping. Not just the comma:
    # "." and "/" are equally presentational in a MARC $a.
    isbd_punctuation: str = Field(
        default_factory=lambda: str(_REC_DEFAULTS.get("isbd_punctuation") or "")
    )

    non_filing_articles: dict[str, list[str]] = Field(
        default_factory=lambda: {k: list(v) for k, v in DEFAULT_NON_FILING_ARTICLES.items()}
    )
    publisher_suffixes: list[str] = Field(default_factory=lambda: list(DEFAULT_PUBLISHER_SUFFIXES))
    volume_prefixes: list[str] = Field(default_factory=lambda: list(DEFAULT_VOLUME_PREFIXES))
    volume_suffixes: list[str] = Field(default_factory=lambda: list(DEFAULT_VOLUME_SUFFIXES))
    volume_format_template: str = Field(default=DEFAULT_VOLUME_FORMAT_TEMPLATE)
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
    default_classification_scheme: str = Field(
        default_factory=lambda: str(_REC_DEFAULTS.get("default_classification_scheme") or "")
    )
    control_number_prefix: Optional[str] = None
    control_number_pattern: Optional[str] = None
    control_number_padding_digits: Optional[int] = None
    control_number_strip_prefixes: Optional[list[str]] = None
    urn_pattern: str = Field(default_factory=lambda: str(_REC_DEFAULTS.get("urn_pattern") or ""))
    source_identifier_templates: dict[str, str] = Field(
        default_factory=lambda: dict(_REC_DEFAULTS.get("source_identifier_templates", {}))
    )
    standard_identifier_rules: dict[str, dict[str, Any]] = Field(
        default_factory=lambda: {
            k: dict(v) for k, v in _REC_DEFAULTS.get("standard_identifier_rules", {}).items()
        }
    )
    relator_mappings: dict[str, str] = Field(default_factory=lambda: dict(DEFAULT_RELATOR_MAPPINGS))
    custom_relator_terms: dict[str, str] = Field(default_factory=dict)

    # Linguistic and subject field delegations for direct RecordNormalizer compatibility
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
    default_language: str = DEFAULT_LANGUAGE
    subject_field_category_map: dict[str, str] = Field(
        default_factory=lambda: dict(DEFAULT_SUBJECT_FIELD_CATEGORY_MAP)
    )
    default_subject_field: str = Field(
        default_factory=lambda: str(
            _VOCAB_DEFAULTS.get("default_subject_field")
            or _VOCAB_DEFAULTS.get("default_vocab_target_field")
            or ""
        )
    )


class NormalizationContainer(BaseModel):
    """Compatibility container pairing record normalization with vocabulary settings."""

    record: RecordNormalizationConfig = Field(default_factory=RecordNormalizationConfig)
    vocab: VocabulariesConfig = Field(default_factory=VocabulariesConfig)


NormalizationConfig = NormalizationContainer


# ---------------------------------------------------------------------------
# Native Format Enrichment Configuration
# ---------------------------------------------------------------------------


def get_marc_predicate_profiles() -> dict[str, dict[str, tuple[str, str]]]:
    """Load default MARC predicate profiles mapping profile name to work/expression URI and scheme tag."""
    raw = _ENRICH_DEFAULTS.get("predicate_profiles") or {}
    res: dict[str, dict[str, tuple[str, str]]] = {}
    for prof_name, preds in raw.items():
        if isinstance(preds, dict):
            res[prof_name] = {}
            for rel, pair in preds.items():
                if isinstance(pair, (list, tuple)) and len(pair) >= 2:
                    res[prof_name][rel] = (str(pair[0]), str(pair[1]))
    if not res:
        res = {
            "bffi": {
                "work": ("http://urn.fi/URN:NBN:fi:schema:bffi:workManifested", "bffi"),
                "expression": ("http://urn.fi/URN:NBN:fi:schema:bffi:expressionManifested", "bffi"),
            },
            "bibframe": {
                "work": ("http://id.loc.gov/ontologies/bibframe/instanceOf", "lcba"),
                "expression": ("http://id.loc.gov/ontologies/bibframe/expressionOf", "lcba"),
            },
            "rda": {
                "work": ("http://rdaregistry.info/Elements/m/P30135", "rda"),
                "expression": ("http://rdaregistry.info/Elements/m/P30139", "rda"),
            },
        }
    return res


DEFAULT_MARC_PREDICATE_PROFILES: dict[str, dict[str, tuple[str, str]]] = (
    get_marc_predicate_profiles()
)


def get_marc_schemes() -> dict[str, SchemeDefinition]:
    """Load default MARC scheme configurations mapping domain name to SchemeDefinition."""
    raw = _ENRICH_DEFAULTS.get("schemes") or {}
    res: dict[str, SchemeDefinition] = {}
    for domain, val in raw.items():
        if isinstance(val, dict):
            res[domain] = SchemeDefinition(
                default=str(val.get("default", "")),
                accepted=[str(x) for x in val.get("accepted", [])],
            )
        elif isinstance(val, str):
            res[domain] = SchemeDefinition(default=val, accepted=[val])
    return res


def get_marc_domain_concepts() -> dict[str, str]:
    """Load default MARC domain concept mappings."""
    return {str(k): str(v) for k, v in _ENRICH_DEFAULTS.get("domain_concepts", {}).items()}


def get_marc_linking_labels() -> dict[str, dict[str, str]]:
    """Load multilingual MARC linking display labels ($i)."""
    raw = _ENRICH_DEFAULTS.get("linking_labels") or {}
    return {
        str(lang): {str(rel): str(lbl) for rel, lbl in rels.items()}
        for lang, rels in raw.items()
        if isinstance(rels, dict)
    }


def get_marc_accessibility() -> dict[str, Any]:
    """Load MARC accessibility description profiles."""
    return dict(_ENRICH_DEFAULTS.get("accessibility", {}))


def get_default_thesis_institution_uri() -> str:
    """Load default thesis degree-granting institution URI."""
    return str(
        _ENRICH_DEFAULTS.get(
            "thesis_institution_uri", "http://urn.fi/URN:NBN:fi:au:pn:dgg_institution"
        )
    )


def get_surrogate_title_keywords() -> list[str]:
    """Load keywords triggering surrogate/reproduction linking."""
    return list(_ENRICH_DEFAULTS.get("surrogate_title_keywords", ["surrogate"]))


def get_default_thesis_relator_code() -> str:
    """Load default thesis degree-granting institution relator code ($4)."""
    return str(_ENRICH_DEFAULTS.get("thesis_relator_code", "dgg"))


def get_ephemera_terms() -> list[str]:
    """Load default ephemera corporate authority terms."""
    return list(
        _ENRICH_DEFAULTS.get("ephemera_terms")
        or [
            "pienpainatteet",
            "småtryck",
            "accidenstryck",
            "tilapäispainatteet",
            "ephemera",
        ]
    )


DEFAULT_MARC_SCHEMES: dict[str, SchemeDefinition] = get_marc_schemes()
DEFAULT_MARC_DOMAIN_CONCEPTS: dict[str, str] = get_marc_domain_concepts()
DEFAULT_MARC_LINKING_LABELS: dict[str, dict[str, str]] = get_marc_linking_labels()
DEFAULT_MARC_ACCESSIBILITY: dict[str, Any] = get_marc_accessibility()
DEFAULT_THESIS_INSTITUTION_URI: str = get_default_thesis_institution_uri()
DEFAULT_THESIS_RELATOR_CODE: str = get_default_thesis_relator_code()
DEFAULT_SURROGATE_TITLE_KEYWORDS: list[str] = get_surrogate_title_keywords()
DEFAULT_EPHEMERA_TERMS: list[str] = get_ephemera_terms()


class EnrichmentConfig(BaseModel):
    """Fine-grained toggles and profiles for native format tree enrichment."""

    inject_work_links: bool = True
    inject_expression_links: bool = True
    inject_hub_links: bool = True
    inject_same_as: bool = True
    inject_component_links: bool = True
    active_profiles: list[str] = Field(default_factory=lambda: ["bibframe", "bffi", "rda"])
    predicate_profiles: dict[str, dict[str, tuple[str, str]]] = Field(
        default_factory=lambda: {
            k: {rel: tuple(pair) for rel, pair in v.items()}
            for k, v in DEFAULT_MARC_PREDICATE_PROFILES.items()
        }
    )
    marc_predicate_mode: Literal["stacked", "distinct"] = "stacked"
    language: str = DEFAULT_LANGUAGE
    schemes: dict[str, SchemeDefinition] = Field(default_factory=get_marc_schemes)
    domain_concepts: dict[str, str] = Field(default_factory=get_marc_domain_concepts)
    linking_labels: dict[str, dict[str, str]] = Field(default_factory=get_marc_linking_labels)
    accessibility: dict[str, Any] = Field(default_factory=get_marc_accessibility)
    thesis_institution_uri: str = Field(default_factory=get_default_thesis_institution_uri)
    thesis_relator_code: str = Field(default_factory=get_default_thesis_relator_code)
    surrogate_title_keywords: list[str] = Field(default_factory=get_surrogate_title_keywords)
    ephemera_terms: list[str] = Field(default_factory=get_ephemera_terms)

    @property
    def marc_predicate_profiles(self) -> list[str]:
        return self.active_profiles

    @marc_predicate_profiles.setter
    def marc_predicate_profiles(self, val: list[str]) -> None:
        self.active_profiles = val

    @field_validator("schemes", mode="before")
    @classmethod
    def _coerce_schemes(cls, v: Any) -> Any:
        if isinstance(v, dict):
            coerced: dict[str, SchemeDefinition] = {}
            for domain, val in v.items():
                if isinstance(val, SchemeDefinition):
                    coerced[domain] = val
                elif isinstance(val, dict):
                    coerced[domain] = SchemeDefinition(**val)
                elif isinstance(val, str):
                    coerced[domain] = SchemeDefinition(default=val, accepted=[val])
            return coerced
        return v

    def get_relationship_label(self, rel: str, lang: Optional[str] = None) -> str:
        """Resolve relationship display label for $i in the specified language."""
        target_lang = (lang or self.language or DEFAULT_LANGUAGE).strip().lower()
        lang_dict = self.linking_labels.get(target_lang) or self.linking_labels.get(
            DEFAULT_LANGUAGE, {}
        )
        return lang_dict.get(rel, "")

    def get_scheme(self, domain: str) -> SchemeDefinition:
        """Resolve SchemeDefinition for a domain."""
        if domain in self.schemes:
            val = self.schemes[domain]
            if isinstance(val, SchemeDefinition):
                return val
            if isinstance(val, dict):
                return SchemeDefinition(**val)
            if isinstance(val, str):
                return SchemeDefinition(default=val, accepted=[val])
        return SchemeDefinition(default="", accepted=[])

    def get_accessibility(self, profile: str, lang: Optional[str] = None) -> dict[str, str]:
        """Resolve accessibility fields (content_summary, mode, note) for a profile."""
        target_lang = (lang or self.language or DEFAULT_LANGUAGE).strip().lower()
        prof_data = self.accessibility.get(profile, {})
        notes = prof_data.get("notes", {})
        note_str = notes.get(target_lang) or notes.get(DEFAULT_LANGUAGE, "")
        return {
            "content_summary": str(prof_data.get("content_summary", "")),
            "mode": str(prof_data.get("mode", "")),
            "note": str(note_str),
        }

    @field_validator("predicate_profiles", mode="before")
    @classmethod
    def _coerce_predicate_profiles(cls, v: Any) -> Any:
        if isinstance(v, dict):
            coerced: dict[str, dict[str, tuple[str, str]]] = {}
            for prof, preds in v.items():
                if isinstance(preds, dict):
                    coerced[prof] = {}
                    for rel, pair in preds.items():
                        if isinstance(pair, (list, tuple)) and len(pair) >= 2:
                            coerced[prof][rel] = (str(pair[0]), str(pair[1]))
            return coerced
        return v

    @model_validator(mode="before")
    @classmethod
    def _handle_predicate_profiles_input(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if "marc_predicate_definitions" in data and "predicate_profiles" not in data:
                data["predicate_profiles"] = data.pop("marc_predicate_definitions")
            if "marc_predicate_profiles" in data and isinstance(
                data["marc_predicate_profiles"], dict
            ):
                if "predicate_profiles" not in data:
                    data["predicate_profiles"] = data["marc_predicate_profiles"]
                data["active_profiles"] = list(data["marc_predicate_profiles"].keys())
            elif "marc_predicate_profiles" in data and isinstance(
                data["marc_predicate_profiles"], list
            ):
                data["active_profiles"] = data.pop("marc_predicate_profiles")
        return data


EnrichmentTogglesConfig = EnrichmentConfig


# ---------------------------------------------------------------------------
# Downloader and Ingestion Configuration Models
# ---------------------------------------------------------------------------


class SkosmosDownloadConfig(BaseModel):
    """Configuration for downloading value vocabularies from a Skosmos REST instance."""

    enabled: bool = True
    api_url: str = "https://api.finto.fi/rest/v1"
    output_dir: Path = Path("data/vocabularies")
    vocabularies: Optional[list[str]] = None
    dump_urls: dict[str, str] = Field(default_factory=dict)
    format: str = "text/turtle"
    timeout_seconds: float = 120.0
    overwrite: bool = False


class VocabIngestConfig(BaseModel):
    """Configuration for value vocabulary and migration thesauri ingestion."""

    input_path: Path = Path("data/vocabularies")
    vocabularies: Optional[list[str]] = None
    legacy_thesauri: Optional[list[str]] = None
    pref_label_languages: Optional[list[str] | str] = None
    alt_label_languages: Optional[list[str] | str] = None
    normalization: Optional[VocabulariesConfig] = None


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
    # Whether this source's records may be used as authority when matching.
    # A corpus being reconciled is ingested the same way as any other source,
    # with this set false, rather than through a separate staging path.
    authoritative: bool = True
    # What a re-ingest does to rows this source wrote before.
    #   accumulate  add to them - the historical behaviour
    #   replace     remove this source's rows first, leaving other sources alone
    # Deterministic ids (Phase 13.1) are what make `replace` a refresh rather
    # than a churn of identity: the same input yields the same URIs.
    ingest_mode: Literal["accumulate", "replace"] = "accumulate"
    id_source: Literal["marc_001", "oai_identifier", "auto"] = "marc_001"
    control_number_prefix: Optional[str] = None
    control_number_padding_digits: Optional[int] = None
    control_number_pattern: Optional[str] = None
    workers: int = 4
    max_concurrent_requests: int = 8
    from_date: Optional[str] = None
    until_date: Optional[str] = None
    slice_days: int = 30
    batch_size: int = 500
    batch_queue_size: int = 4
    db_batch_chunk_size: int = 1000
    request_delay: float = 0.1
    timeout: float = 60.0
    max_retries: int = 5
    base_delay: float = 1.0
    http2: bool = True
    prefetch: bool = True
    checkpoint_interval_records: int = 10000
    checkpoint_interval_seconds: float = 30.0
    include_items: bool = False
    normalization: Optional[RecordNormalizationConfig] = None
    # How this source records what a publication contains: a bundled profile
    # name, a path to a YAML profile, or "off". Two sources in the same format
    # disagree about this, so it belongs to the source. See
    # constituents/spec.py and plans/aggregates.md.
    constituents: Optional[str] = None
    # What ingest does with them: off | keys_only | entities. None inherits the
    # global default.
    constituents_mode: Optional[Literal["off", "keys_only", "entities"]] = None

    @model_validator(mode="after")
    def validate_workers_and_queues(self) -> OAIIngestConfig:
        if self.workers < 1:
            raise ValueError(f"workers must be at least 1, got {self.workers}")
        if self.max_concurrent_requests < 1:
            raise ValueError(
                f"max_concurrent_requests must be at least 1, got {self.max_concurrent_requests}"
            )
        if self.batch_queue_size < 1:
            raise ValueError(f"batch_queue_size must be at least 1, got {self.batch_queue_size}")
        if self.slice_days < 1:
            raise ValueError(f"slice_days must be at least 1, got {self.slice_days}")
        if self.batch_size < 1:
            raise ValueError(f"batch_size must be at least 1, got {self.batch_size}")
        if self.db_batch_chunk_size < 1:
            raise ValueError(
                f"db_batch_chunk_size must be at least 1, got {self.db_batch_chunk_size}"
            )
        if self.max_retries < 0:
            raise ValueError(f"max_retries cannot be negative, got {self.max_retries}")
        if self.base_delay < 0:
            raise ValueError(f"base_delay cannot be negative, got {self.base_delay}")
        if self.checkpoint_interval_records < 1:
            raise ValueError(
                f"checkpoint_interval_records must be at least 1, got {self.checkpoint_interval_records}"
            )
        if self.checkpoint_interval_seconds <= 0:
            raise ValueError(
                f"checkpoint_interval_seconds must be positive, got {self.checkpoint_interval_seconds}"
            )
        return self


class WikidataIngestConfig(BaseModel):
    """Configuration for streaming Wikidata JSON ingestion."""

    dump_path: Path = Path("data/wikidata/latest-all.json.gz")
    batch_size: int = 1000
    pass1_only: bool = False
    extract_works: bool = True
    normalization: Optional[RecordNormalizationConfig] = None
    require_author_authority_id: bool = True
    author_authority_properties: list[str] = Field(
        default_factory=lambda: [
            "P3595",
            "P213",
            "P214",
            "P227",
            "P244",
            "P906",
            "P496",
        ]
    )
    languages: dict[str, str] = Field(
        default_factory=lambda: dict(
            get_bundled_defaults().get("wikidata", {}).get("languages", {})
        )
    )
    subject_mappings: dict[str, str] = Field(
        default_factory=lambda: dict(
            get_bundled_defaults().get("wikidata", {}).get("subject_mappings", {})
        )
    )


# ---------------------------------------------------------------------------
# Reconciliation and Matching Configuration Models
# ---------------------------------------------------------------------------


class TemplateSetPathsConfig(BaseModel):
    """File paths for the WEMI and agent serialization templates."""

    work: Path = Path("reconciliator/templates/work_serialization.j2")
    expression: Path = Path("reconciliator/templates/expression_serialization.j2")
    manifestation: Path = Path("reconciliator/templates/manifestation_serialization.j2")
    agent: Path = Path("reconciliator/templates/agent_serialization.j2")


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
            _CLUSTER_DEFAULTS.get("scoring", {}).get("with_author", {}).get("title", 0.45)
        )
    )
    author: float = Field(
        default_factory=lambda: float(
            _CLUSTER_DEFAULTS.get("scoring", {}).get("with_author", {}).get("author", 0.35)
        )
    )
    year: float = Field(
        default_factory=lambda: float(
            _CLUSTER_DEFAULTS.get("scoring", {}).get("with_author", {}).get("year", 0.10)
        )
    )
    type: float = Field(
        default_factory=lambda: float(
            _CLUSTER_DEFAULTS.get("scoring", {}).get("with_author", {}).get("type", 0.10)
        )
    )


class WEMIScoringWeightsWithoutAuthor(BaseModel):
    title: float = Field(
        default_factory=lambda: float(
            _CLUSTER_DEFAULTS.get("scoring", {}).get("without_author", {}).get("title", 0.70)
        )
    )
    year: float = Field(
        default_factory=lambda: float(
            _CLUSTER_DEFAULTS.get("scoring", {}).get("without_author", {}).get("year", 0.15)
        )
    )
    type: float = Field(
        default_factory=lambda: float(
            _CLUSTER_DEFAULTS.get("scoring", {}).get("without_author", {}).get("type", 0.15)
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
            _CLUSTER_DEFAULTS.get("scoring", {}).get("author_missing_similarity", 0.60)
        )
    )
    year_missing_similarity: float = Field(
        default_factory=lambda: float(
            _CLUSTER_DEFAULTS.get("scoring", {}).get("year_missing_similarity", 0.80)
        )
    )
    year_max_difference: float = Field(
        default_factory=lambda: float(
            _CLUSTER_DEFAULTS.get("scoring", {}).get("year_max_difference", 20.0)
        )
    )
    cross_format_type_similarity: float = Field(
        default_factory=lambda: float(
            _CLUSTER_DEFAULTS.get("scoring", {}).get("cross_format_type_similarity", 0.95)
        )
    )
    default_type_similarity: float = Field(
        default_factory=lambda: float(
            _CLUSTER_DEFAULTS.get("scoring", {}).get("default_type_similarity", 0.50)
        )
    )


class MusicScoreConfig(BaseModel):
    terms: list[str] = Field(
        default_factory=lambda: list(
            _CLUSTER_DEFAULTS.get("music_score", {}).get(
                "terms", ["score", "partituuri", "nuotti", "part", "stemma"]
            )
        )
    )
    standard_key_terms: list[str] = Field(
        default_factory=lambda: list(
            _CLUSTER_DEFAULTS.get("music_score", {}).get("standard_key_terms", ["ismn", "plate"])
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
    scoring: WEMIScoringConfig = Field(
        default_factory=lambda: WEMIScoringConfig(
            **_CLUSTER_DEFAULTS.get("scoring", {})
        )
    )
    music_score: MusicScoreConfig = Field(
        default_factory=lambda: MusicScoreConfig(
            **_CLUSTER_DEFAULTS.get("music_score", {})
        )
    )


class WorkConsolidationConfig(BaseModel):
    """Configuration for cross-source work identity consolidation.

    Each contributing source mints its own work URI, so the same work arrives once
    per source. Consolidation records `same_work_as` edges between those rows and
    elects one canonical work per equivalence group, without deleting anything.
    """

    enabled: bool = True
    # Evidence admitted when deciding two work rows are the same work.
    use_wikidata_qid: bool = True
    use_shared_identifiers: bool = True
    use_metadata: bool = True
    # Identifier schemes strong enough to assert work identity on their own.
    # Deliberately excludes weak or source-local schemes such as control_number.
    strong_identifier_schemes: list[str] = Field(
        default_factory=lambda: [
            "isbn",
            "issn",
            "ismn",
            "isrc",
            "ean",
            "doi",
            "urn",
            "asteri",
        ]
    )
    # Metadata agreement required when no identifier or QID is shared.
    metadata_threshold: float = 0.93
    # Consolidate works held by the same source as well as across sources.
    include_same_source: bool = False
    # Sources preferred as the canonical representative, best first. Sources not
    # listed rank after those that are; ties break on work_uri for stability.
    canonical_source_priority: list[str] = Field(default_factory=list)


class AgentClusterScoringWeights(BaseModel):

    """Weights for multi-feature agent cluster scoring."""

    name_weight: float = Field(
        default_factory=lambda: float(
            _AGENT_CLUSTERING_DEFAULTS.get("scoring_weights", {}).get("name_weight", 0.35)
        )
    )
    dates_weight: float = Field(
        default_factory=lambda: float(
            _AGENT_CLUSTERING_DEFAULTS.get("scoring_weights", {}).get("dates_weight", 0.20)
        )
    )
    work_cluster_weight: float = Field(
        default_factory=lambda: float(
            _AGENT_CLUSTERING_DEFAULTS.get("scoring_weights", {}).get("work_cluster_weight", 0.20)
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
    source_authority_precedence: list[str] = Field(
        default_factory=lambda: list(
            _AGENT_CLUSTERING_DEFAULTS.get("source_authority_precedence") or []
        )
    )
    override_identifiers: list[str] = Field(
        default_factory=lambda: list(
            _AGENT_CLUSTERING_DEFAULTS.get("override_identifiers") or []
        )
    )
    blocking_keys: list[str] = Field(
        default_factory=lambda: list(
            _AGENT_CLUSTERING_DEFAULTS.get("blocking_keys") or []
        )
    )
    scoring_weights: AgentClusterScoringWeights = Field(
        default_factory=lambda: AgentClusterScoringWeights(
            **_AGENT_CLUSTERING_DEFAULTS.get("scoring_weights", {})
        )
    )


class ReconcileFTSFallbackConfig(BaseModel):
    """Configuration for WorkMatcher Tier 3 SQLite FTS5 fallback during reconciliation."""

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


class AudiobookFunnelConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    default_language: str = "fin"
    title_suffix: str = " (äänikirja)"
    undefined_language: str = "und"
    source_id_suffix: str = "_audio"
    expression_uri_prefix: str = "#spoken_word_"
    content_type: str = "spoken_word"


class RecordedSoundFunnelConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    jaccard_threshold: float = 0.75
    max_candidates: int = 50
    match_tier: str = "tier3_tracklist_jaccard"


class NotatedMusicFunnelConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fallback_match_tier: str = "tier2_domain_key"


class FunnelPatternsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    notated_music_title: str = "(?i)score|partituuri|nuotti|stemma"
    podcast_title: str = "podcast"
    parallel_translators_signature: str = "(?i)parallel"
    government_valto_urn: str = "(?i)valto"
    exhibition_title: str = "näyttely"


class FunnelsConfig(BaseModel):
    """Configuration surface for funnel thresholds, heuristics, and pattern defaults."""

    model_config = ConfigDict(extra="forbid")
    default_match_tier: str = "tier2_domain_key"
    default_confidence: float = 0.90
    audiobook: AudiobookFunnelConfig = Field(
        default_factory=lambda: AudiobookFunnelConfig(
            **dict(_FUNNELS_DEFAULTS.get("audiobook") or {})
        )
    )
    recorded_sound: RecordedSoundFunnelConfig = Field(
        default_factory=lambda: RecordedSoundFunnelConfig(
            **dict(_FUNNELS_DEFAULTS.get("recorded_sound") or {})
        )
    )
    notated_music: NotatedMusicFunnelConfig = Field(
        default_factory=lambda: NotatedMusicFunnelConfig(
            **dict(_FUNNELS_DEFAULTS.get("notated_music") or {})
        )
    )
    patterns: FunnelPatternsConfig = Field(
        default_factory=lambda: FunnelPatternsConfig(
            **dict(_FUNNELS_DEFAULTS.get("patterns") or {})
        )
    )


from reconciliator.funnels.spec import FunnelSpec


class ReconcileConfig(BaseModel):
    """Offline reconciliation and enrichment configuration."""

    target_file: Path
    # Name the corpus is stored under. It becomes `record_origin` on the corpus's
    # WEMI rows and is registered non-authoritative, so the corpus is ingested
    # like any other source rather than through a staging table.
    corpus_source_name: Optional[str] = None
    # Funnel overrides. An entry whose `id` matches a built-in replaces it in
    # place; a new id is appended just before the fall-through funnel. Omitting
    # this leaves the 42 built-ins as they are.
    funnels: list[FunnelSpec] = Field(default_factory=list)
    # Global funnel configuration settings (thresholds, patterns, defaults)
    funnel_settings: FunnelsConfig = Field(
        default_factory=lambda: FunnelsConfig(**_FUNNELS_DEFAULTS)
    )
    # The directory the config file was loaded from, set by `load_config`. It is
    # what a relative `handler` or `constituents` path resolves against, so a
    # config keeps working wherever the CLI is invoked from. `WorkMatcher` has
    # read this since funnel handlers gained paths; nothing ever set it, so a
    # relative path silently resolved against the process working directory.
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

    vocabularies: list[str] = Field(default_factory=list)
    agent_priority: list[str] = Field(default_factory=list)
    subject_priority: list[str] = Field(default_factory=list)
    supported_subject_vocabularies: list[str] = Field(default_factory=list)
    entity_vocabularies: list[str] = Field(default_factory=list)
    vocabulary_patterns: dict[str, list[str]] = Field(default_factory=dict)

    @property
    def enrichment_toggles(self) -> EnrichmentConfig:
        return self.enrichment

    @enrichment_toggles.setter
    def enrichment_toggles(self, val: EnrichmentConfig) -> None:
        self.enrichment = val

    @model_validator(mode="before")
    @classmethod
    def _handle_format_and_enrichment(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if "target_format" in data and "format" not in data:
                data["format"] = data["target_format"]
            if "enrichment_toggles" in data and "enrichment" not in data:
                data["enrichment"] = data["enrichment_toggles"]
        return data

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


# ---------------------------------------------------------------------------
# Telemetry and Logging Models
# ---------------------------------------------------------------------------


class TelemetryConfig(BaseModel):
    """Progress heartbeat and query monitoring intervals."""

    heartbeat_interval_seconds: float = 1.0
    watch_interval_seconds: float = 1.0
    show_vector_daemon: bool = True


class LoggingConfig(BaseModel):
    """Logging subsystem configuration surface."""

    level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    format: str = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    date_format: str = "%Y-%m-%d %H:%M:%S"
    log_file: Optional[Path] = None
    capture_warnings: bool = False

    @field_validator("level", mode="before")
    @classmethod
    def normalize_level(cls, v: Any) -> str:
        if isinstance(v, str):
            v_up = v.upper()
            if v_up == "WARN":
                return "WARNING"
            return v_up
        return str(v)


# ---------------------------------------------------------------------------
# Root Application Configuration
# ---------------------------------------------------------------------------


class ConstituentsConfig(BaseModel):
    """Global default for constituent extraction, overridable per source.

    The shipped default profile is the empty one, so a source nobody has
    described behaves exactly as it did before constituents existed.
    """

    profile: Optional[str] = None
    mode: Literal["off", "keys_only", "entities"] = "keys_only"
    #: Whether constituent works enter the work vector index. Off by default:
    #: measured on 200 albums of 12 tracks they are 82% of the index, and a
    #: title-only query returns tracks and no albums, so tier 3's top-k window
    #: fills with things that are not what the target is. A source that
    #: reconciles track-level targets wants them indexed.
    index_works: bool = False


class AppConfig(BaseModel):
    """Root configuration object combining all subsystems."""

    compute: ComputeConfig = Field(default_factory=lambda: ComputeConfig(**_COMPUTE_DEFAULTS))
    storage: StorageConfig = Field(default_factory=lambda: StorageConfig(**_STORAGE_DEFAULTS))
    embedding: EmbeddingConfig = Field(default_factory=lambda: EmbeddingConfig(**_EMBED_DEFAULTS))
    linguistic: LinguisticConfig = Field(default_factory=LinguisticConfig)
    vocabularies: VocabulariesConfig = Field(default_factory=VocabulariesConfig)
    sources: dict[str, SourceConfig] = Field(
        default_factory=lambda: {
            k: SourceConfig(**v) if isinstance(v, dict) else v
            for k, v in get_bundled_sources_defaults().items()
        }
    )
    record_normalization: RecordNormalizationConfig = Field(
        default_factory=RecordNormalizationConfig
    )
    constituents: ConstituentsConfig = Field(
        default_factory=lambda: ConstituentsConfig(**_CONSTITUENTS_DEFAULTS)
    )
    #: Set by `load_config`; anchors every relative path in configuration that
    #: names code or a profile.
    config_dir: Optional[Path] = None
    download_skosmos: dict[str, SkosmosDownloadConfig] = Field(default_factory=dict)
    ingest_vocab: Optional[VocabIngestConfig] = None
    ingest_oai: dict[str, OAIIngestConfig] = Field(default_factory=dict)
    ingest_wikidata: Optional[WikidataIngestConfig] = None
    consolidation: WorkConsolidationConfig = Field(
        default_factory=lambda: WorkConsolidationConfig(**_CONSOLIDATION_DEFAULTS)
    )
    agent_clustering: AgentClusterConfig = Field(
        default_factory=lambda: AgentClusterConfig(**_AGENT_CLUSTERING_DEFAULTS)
    )
    funnels: FunnelsConfig = Field(
        default_factory=lambda: FunnelsConfig(**_FUNNELS_DEFAULTS)
    )

    reconcile: Optional[ReconcileConfig] = None
    telemetry: TelemetryConfig = Field(
        default_factory=lambda: TelemetryConfig(**_TELEMETRY_DEFAULTS)
    )
    logging: LoggingConfig = Field(default_factory=lambda: LoggingConfig(**_LOGGING_DEFAULTS))

    @property
    def normalization(self) -> NormalizationContainer:
        """Compatibility view of record normalization and vocabulary settings."""
        return NormalizationContainer(record=self.record_normalization, vocab=self.vocabularies)

    @model_validator(mode="before")
    @classmethod
    def _handle_legacy_sections(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if "normalization" in data:
                norm = data.pop("normalization")
                if isinstance(norm, dict):
                    if "record" in norm and "record_normalization" not in data:
                        data["record_normalization"] = norm["record"]
                    if "vocab" in norm and "vocabularies" not in data:
                        data["vocabularies"] = norm["vocab"]
        return data

    @model_validator(mode="after")
    def _propagate_shared_settings(self) -> Self:
        shared_dev = self.compute.device
        shared_threads = self.compute.threads

        # Propagate compute hardware device if subsystem is set to "auto"
        if self.storage.vector_engine.device == "auto" and shared_dev != "auto":
            self.storage.vector_engine.device = shared_dev
        if self.storage.vector_engine.threads is None and shared_threads is not None:
            self.storage.vector_engine.threads = shared_threads

        if self.embedding.device == "auto" and shared_dev != "auto":
            self.embedding.device = shared_dev

        if self.reconcile:
            if self.reconcile.rerank.device == "auto" and shared_dev != "auto":
                self.reconcile.rerank.device = shared_dev

        # Propagate vocabularies defaults to download_skosmos
        for skos_cfg in self.download_skosmos.values():
            if skos_cfg.vocabularies is None:
                skos_cfg.vocabularies = list(self.vocabularies.download_list)

        # Propagate vocabularies defaults to ingest_vocab
        if self.ingest_vocab is not None:
            if self.ingest_vocab.vocabularies is None:
                self.ingest_vocab.vocabularies = list(self.vocabularies.download_list)
            if self.ingest_vocab.legacy_thesauri is None:
                self.ingest_vocab.legacy_thesauri = list(self.vocabularies.legacy_to_modern.keys())

        # Propagate wikidata author properties to ingest_wikidata
        if self.ingest_wikidata is not None:
            if self.ingest_wikidata.author_authority_properties is None:
                self.ingest_wikidata.author_authority_properties = list(
                    self.vocabularies.wikidata_author_properties.values()
                )

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


# ---------------------------------------------------------------------------
# Path Resolution and Loader Functions
# ---------------------------------------------------------------------------


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
                s_data["normalization"] = _load_modular_file(s_data["normalization"], base_dir)

    return result


def _load_modular_file(file_val: Any, base_dir: Path) -> dict[str, Any]:
    """Load settings from an external YAML file or return dict."""
    if isinstance(file_val, (str, Path)):
        p = Path(file_val)
        target = (base_dir / p).resolve() if not p.is_absolute() else p
        if not target.is_file():
            raise FileNotFoundError(
                f"Configuration file not found: {file_val} (resolved to {target})"
            )
        loaded = _load_source_file(target)
        return loaded
    elif isinstance(file_val, dict):
        return file_val
    return {}


_load_modular_normalization = _load_modular_file


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
    config.storage.data_dir = resolve_p(config.storage.data_dir)  # type: ignore[assignment]
    config.storage.work_index = resolve_p(config.storage.work_index)
    config.storage.manifestation_index = resolve_p(config.storage.manifestation_index)
    config.storage.agent_index = resolve_p(config.storage.agent_index)

    # Embedding template
    if config.embedding.template_path:
        t_path = config.embedding.template_path
        if not t_path.is_absolute():
            candidate = (base_dir / t_path).resolve()
            if candidate.exists():
                config.embedding.template_path = candidate
            else:
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
        if config.reconcile.reports_dir:
            config.reconcile.reports_dir = resolve_p(config.reconcile.reports_dir)
        if config.reconcile.agent_reports:
            config.reconcile.agent_reports.detailed_file = resolve_p(
                config.reconcile.agent_reports.detailed_file
            )  # type: ignore[assignment]
            config.reconcile.agent_reports.summary_file = resolve_p(
                config.reconcile.agent_reports.summary_file
            )  # type: ignore[assignment]
        if config.reconcile.subject_reports:
            config.reconcile.subject_reports.detailed_file = resolve_p(
                config.reconcile.subject_reports.detailed_file
            )  # type: ignore[assignment]
            config.reconcile.subject_reports.summary_file = resolve_p(
                config.reconcile.subject_reports.summary_file
            )  # type: ignore[assignment]
        if config.reconcile.work_reports:
            config.reconcile.work_reports.detailed_file = resolve_p(
                config.reconcile.work_reports.detailed_file
            )  # type: ignore[assignment]
            config.reconcile.work_reports.summary_file = resolve_p(
                config.reconcile.work_reports.summary_file
            )  # type: ignore[assignment]
        if config.reconcile.expression_reports:
            config.reconcile.expression_reports.detailed_file = resolve_p(
                config.reconcile.expression_reports.detailed_file
            )  # type: ignore[assignment]
            config.reconcile.expression_reports.summary_file = resolve_p(
                config.reconcile.expression_reports.summary_file
            )  # type: ignore[assignment]
        if config.reconcile.templates:
            config.reconcile.templates.work = resolve_p(config.reconcile.templates.work)  # type: ignore[assignment]
            config.reconcile.templates.expression = resolve_p(config.reconcile.templates.expression)  # type: ignore[assignment]
            config.reconcile.templates.manifestation = resolve_p(
                config.reconcile.templates.manifestation
            )  # type: ignore[assignment]
            config.reconcile.templates.agent = resolve_p(config.reconcile.templates.agent)  # type: ignore[assignment]

    # Logging path
    if config.logging and config.logging.log_file:
        config.logging.log_file = resolve_p(config.logging.log_file)

    return config


def configure_logging(logging_cfg: Optional[LoggingConfig | AppConfig] = None) -> None:
    """Configure standard logging handlers, formatters, and root level based on configuration."""
    if isinstance(logging_cfg, AppConfig):
        cfg = logging_cfg.logging
    elif isinstance(logging_cfg, LoggingConfig):
        cfg = logging_cfg
    else:
        cfg = LoggingConfig()

    root_logger = logging.getLogger()
    level_num = getattr(logging, cfg.level, logging.INFO)
    root_logger.setLevel(level_num)

    formatter = logging.Formatter(fmt=cfg.format, datefmt=cfg.date_format)

    # Remove existing handlers (preserving pytest LogCaptureHandler if present)
    for handler in list(root_logger.handlers):
        if handler.__class__.__name__ == "LogCaptureHandler":
            continue
        root_logger.removeHandler(handler)
        try:
            handler.close()
        except Exception:
            pass

    stream_handler = logging.StreamHandler(sys.stderr)
    stream_handler.setLevel(level_num)
    stream_handler.setFormatter(formatter)
    root_logger.addHandler(stream_handler)

    if cfg.log_file:
        log_path = Path(cfg.log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(str(log_path), encoding="utf-8")
        file_handler.setLevel(level_num)
        file_handler.setFormatter(formatter)
        root_logger.addHandler(file_handler)

    logging.captureWarnings(cfg.capture_warnings)


def load_config(config_path: Path | str, configure_log: bool = False) -> AppConfig:
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
            raw_data["ingest_vocab"]["normalization"] = _load_modular_file(
                raw_data["ingest_vocab"]["normalization"], base_dir
            )
    if "ingest_wikidata" in raw_data and isinstance(raw_data["ingest_wikidata"], dict):
        if (
            "normalization" in raw_data["ingest_wikidata"]
            and raw_data["ingest_wikidata"]["normalization"] is not None
        ):
            raw_data["ingest_wikidata"]["normalization"] = _load_modular_file(
                raw_data["ingest_wikidata"]["normalization"], base_dir
            )
    if "reconcile" in raw_data and isinstance(raw_data["reconcile"], dict):
        if (
            "normalization" in raw_data["reconcile"]
            and raw_data["reconcile"]["normalization"] is not None
        ):
            raw_data["reconcile"]["normalization"] = _load_modular_file(
                raw_data["reconcile"]["normalization"], base_dir
            )
    if "record_normalization" in raw_data and isinstance(
        raw_data["record_normalization"], (str, Path)
    ):
        raw_data["record_normalization"] = _load_modular_file(
            raw_data["record_normalization"], base_dir
        )
    if "vocabularies" in raw_data and isinstance(raw_data["vocabularies"], (str, Path)):
        raw_data["vocabularies"] = _load_modular_file(raw_data["vocabularies"], base_dir)
    if "linguistic" in raw_data and isinstance(raw_data["linguistic"], (str, Path)):
        raw_data["linguistic"] = _load_modular_file(raw_data["linguistic"], base_dir)
    if "normalization" in raw_data and raw_data["normalization"] is not None:
        raw_data["normalization"] = _load_modular_file(raw_data["normalization"], base_dir)

    config = AppConfig.model_validate(raw_data)
    resolved = _resolve_config_paths(config, base_dir)
    resolved.config_dir = base_dir.resolve()
    if resolved.reconcile is not None:
        resolved.reconcile.config_dir = resolved.config_dir
        if "funnels" in raw_data and isinstance(raw_data["funnels"], dict):
            resolved.reconcile.funnel_settings = resolved.funnels
    _validate_constituent_sources(resolved)
    if configure_log:
        configure_logging(resolved.logging)
    return resolved


def _validate_constituent_sources(config: AppConfig) -> None:
    """Resolve every source's constituent profile and extractor, once, here.

    So an unknown profile name or a missing extractor file is a startup error
    naming the source, rather than a source that quietly ingests no constituents
    for the rest of the corpus. Same reason `WorkMatcher` resolves funnel
    handlers in its constructor.
    """
    from reconciliator.constituents import resolve_for_source

    for name, source in (config.ingest_oai or {}).items():
        resolve_for_source(
            name,
            source.constituents,
            source.constituents_mode,
            default_profile=config.constituents.profile,
            default_mode=config.constituents.mode,
            config_dir=config.config_dir,
        )
