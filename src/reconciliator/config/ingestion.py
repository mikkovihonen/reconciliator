"""Ingestion subsystems configuration models (filter, OAI, Wikidata, vocabularies, Skosmos)."""

from __future__ import annotations

from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel, Field, model_validator

from reconciliator.config.defaults import (
    get_bundled_ingestion_defaults,
    get_bundled_oai_ingest_defaults,
)
from reconciliator.config.normalization import RecordNormalizationConfig
from reconciliator.config.semantics import VocabulariesConfig

_INGESTION_DEFAULTS = get_bundled_ingestion_defaults()
_INGEST_FILTER_DEFAULTS = dict(_INGESTION_DEFAULTS.get("filter") or {})
_OAI_INGEST_DEFAULTS = get_bundled_oai_ingest_defaults()
_WIKIDATA_INGEST_DEFAULTS = dict(_INGESTION_DEFAULTS.get("wikidata") or {})
_VOCAB_INGEST_DEFAULTS = dict(_INGESTION_DEFAULTS.get("vocab") or {})


class FieldTruncationConfig(BaseModel):
    """Configuration for truncating overlong text field values."""

    truncate_overlong_values: bool = True
    truncation_marker: str = ""
    default_max_field_length: Optional[int] = None
    field_length_overrides: dict[str, int] = Field(default_factory=dict)


class IngestFilterConfig(BaseModel):
    """Universal publication and media type filter configuration."""

    target_work_types: list[str] = Field(
        default_factory=lambda: list(_INGEST_FILTER_DEFAULTS.get("target_work_types") or [])
    )
    exclude_work_types: list[str] = Field(
        default_factory=lambda: list(_INGEST_FILTER_DEFAULTS.get("exclude_work_types") or [])
    )
    target_languages: list[str] = Field(
        default_factory=lambda: list(_INGEST_FILTER_DEFAULTS.get("target_languages") or [])
    )
    from_year: Optional[int] = Field(
        default_factory=lambda: _INGEST_FILTER_DEFAULTS.get("from_year")
    )
    until_year: Optional[int] = Field(
        default_factory=lambda: _INGEST_FILTER_DEFAULTS.get("until_year")
    )

    def accepts(
        self,
        work_type: Optional[str] = None,
        language: Optional[str] = None,
        year: Optional[int] = None,
    ) -> bool:
        """Evaluate whether a work meets configured ingestion/reconciliation criteria."""
        if self.target_work_types and work_type and work_type not in self.target_work_types:
            return False
        if self.exclude_work_types and work_type and work_type in self.exclude_work_types:
            return False
        if self.target_languages and language and language not in self.target_languages:
            return False
        if self.from_year is not None and year is not None and year < self.from_year:
            return False
        if self.until_year is not None and year is not None and year > self.until_year:
            return False
        return True


class OAIDefaultsConfig(FieldTruncationConfig):
    """Shared defaults for OAI-PMH and ResourceSync harvesting."""

    workers: int = Field(default_factory=lambda: int(_OAI_INGEST_DEFAULTS.get("workers", 4)))
    max_concurrent_requests: int = Field(
        default_factory=lambda: int(_OAI_INGEST_DEFAULTS.get("max_concurrent_requests", 16))
    )
    slice_days: int = Field(default_factory=lambda: int(_OAI_INGEST_DEFAULTS.get("slice_days", 7)))
    batch_size: int = Field(
        default_factory=lambda: int(_OAI_INGEST_DEFAULTS.get("batch_size", 1000))
    )
    batch_flush_interval_seconds: float = Field(
        default_factory=lambda: float(_OAI_INGEST_DEFAULTS.get("batch_flush_interval_seconds", 2.0))
    )
    batch_queue_size: int = Field(
        default_factory=lambda: int(_OAI_INGEST_DEFAULTS.get("batch_queue_size", 32))
    )
    request_delay: float = Field(
        default_factory=lambda: float(_OAI_INGEST_DEFAULTS.get("request_delay", 0.0))
    )
    timeout: float = Field(default_factory=lambda: float(_OAI_INGEST_DEFAULTS.get("timeout", 60.0)))
    max_retries: int = Field(
        default_factory=lambda: int(_OAI_INGEST_DEFAULTS.get("max_retries", 5))
    )
    base_delay: float = Field(
        default_factory=lambda: float(_OAI_INGEST_DEFAULTS.get("base_delay", 1.0))
    )
    http2: bool = Field(default_factory=lambda: bool(_OAI_INGEST_DEFAULTS.get("http2", True)))
    prefetch: bool = Field(default_factory=lambda: bool(_OAI_INGEST_DEFAULTS.get("prefetch", True)))
    checkpoint_interval_records: int = Field(
        default_factory=lambda: int(_OAI_INGEST_DEFAULTS.get("checkpoint_interval_records", 10000))
    )
    checkpoint_interval_seconds: float = Field(
        default_factory=lambda: float(_OAI_INGEST_DEFAULTS.get("checkpoint_interval_seconds", 30.0))
    )
    use_synchronous_commit_off: bool = Field(
        default_factory=lambda: bool(_OAI_INGEST_DEFAULTS.get("use_synchronous_commit_off", True))
    )
    drop_indexes_during_replace: bool = Field(
        default_factory=lambda: bool(_OAI_INGEST_DEFAULTS.get("drop_indexes_during_replace", False))
    )
    adopt_previous_task_id: bool = Field(
        default_factory=lambda: bool(_OAI_INGEST_DEFAULTS.get("adopt_previous_task_id", True))
    )


class OAIIngestConfig(IngestFilterConfig, FieldTruncationConfig):
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
    authoritative: bool = True
    ingest_mode: Literal["accumulate", "replace"] = "accumulate"
    id_source: Literal["marc_001", "oai_identifier", "auto"] = "marc_001"
    control_number_prefix: Optional[str] = None
    control_number_padding_digits: Optional[int] = None
    control_number_pattern: Optional[str] = None
    workers: int = Field(default_factory=lambda: int(_OAI_INGEST_DEFAULTS.get("workers", 4)))
    max_concurrent_requests: int = Field(
        default_factory=lambda: int(_OAI_INGEST_DEFAULTS.get("max_concurrent_requests", 16))
    )
    from_date: Optional[str] = None
    until_date: Optional[str] = None
    slice_days: int = Field(default_factory=lambda: int(_OAI_INGEST_DEFAULTS.get("slice_days", 7)))
    batch_size: int = Field(
        default_factory=lambda: int(_OAI_INGEST_DEFAULTS.get("batch_size", 1000))
    )
    batch_flush_interval_seconds: float = Field(
        default_factory=lambda: float(_OAI_INGEST_DEFAULTS.get("batch_flush_interval_seconds", 2.0))
    )
    batch_queue_size: int = Field(
        default_factory=lambda: int(_OAI_INGEST_DEFAULTS.get("batch_queue_size", 32))
    )
    request_delay: float = Field(
        default_factory=lambda: float(_OAI_INGEST_DEFAULTS.get("request_delay", 0.0))
    )
    timeout: float = Field(default_factory=lambda: float(_OAI_INGEST_DEFAULTS.get("timeout", 60.0)))
    max_retries: int = Field(
        default_factory=lambda: int(_OAI_INGEST_DEFAULTS.get("max_retries", 5))
    )
    base_delay: float = Field(
        default_factory=lambda: float(_OAI_INGEST_DEFAULTS.get("base_delay", 1.0))
    )
    http2: bool = Field(default_factory=lambda: bool(_OAI_INGEST_DEFAULTS.get("http2", True)))
    prefetch: bool = Field(default_factory=lambda: bool(_OAI_INGEST_DEFAULTS.get("prefetch", True)))
    checkpoint_interval_records: int = Field(
        default_factory=lambda: int(_OAI_INGEST_DEFAULTS.get("checkpoint_interval_records", 10000))
    )
    checkpoint_interval_seconds: float = Field(
        default_factory=lambda: float(_OAI_INGEST_DEFAULTS.get("checkpoint_interval_seconds", 30.0))
    )
    use_synchronous_commit_off: bool = Field(
        default_factory=lambda: bool(_OAI_INGEST_DEFAULTS.get("use_synchronous_commit_off", True))
    )
    drop_indexes_during_replace: bool = Field(
        default_factory=lambda: bool(_OAI_INGEST_DEFAULTS.get("drop_indexes_during_replace", False))
    )
    include_items: bool = False
    normalization: Optional[RecordNormalizationConfig] = None
    constituents: Optional[str] = None
    constituents_mode: Optional[Literal["off", "keys_only", "entities"]] = None
    adopt_previous_task_id: bool = Field(
        default_factory=lambda: bool(_OAI_INGEST_DEFAULTS.get("adopt_previous_task_id", True))
    )

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
        if self.batch_flush_interval_seconds <= 0:
            raise ValueError(
                f"batch_flush_interval_seconds must be positive, got {self.batch_flush_interval_seconds}"
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


class WikidataIngestConfig(IngestFilterConfig, FieldTruncationConfig):
    """Configuration for streaming Wikidata JSON ingestion."""

    enabled: bool = Field(
        default_factory=lambda: bool(_WIKIDATA_INGEST_DEFAULTS.get("enabled", True))
    )
    creative_work_types: dict[str, str] = Field(
        default_factory=lambda: dict(_WIKIDATA_INGEST_DEFAULTS.get("creative_work_types") or {})
    )
    human_and_entity_types: list[str] = Field(
        default_factory=lambda: list(_WIKIDATA_INGEST_DEFAULTS.get("human_and_entity_types") or [])
    )
    dump_path: Path = Field(
        default_factory=lambda: Path(
            _WIKIDATA_INGEST_DEFAULTS.get("dump_path", "dumps/latest-all.json.gz")
        )
    )
    batch_size: int = Field(
        default_factory=lambda: int(_WIKIDATA_INGEST_DEFAULTS.get("batch_size", 5000))
    )
    pass1_only: bool = Field(
        default_factory=lambda: bool(_WIKIDATA_INGEST_DEFAULTS.get("pass1_only", False))
    )
    extract_works: bool = Field(
        default_factory=lambda: bool(_WIKIDATA_INGEST_DEFAULTS.get("extract_works", True))
    )
    normalization: Optional[RecordNormalizationConfig] = None
    require_author_authority_id: bool = Field(
        default_factory=lambda: bool(
            _WIKIDATA_INGEST_DEFAULTS.get("require_author_authority_id", True)
        )
    )
    author_authority_properties: list[str] = Field(
        default_factory=lambda: list(
            _WIKIDATA_INGEST_DEFAULTS.get("author_authority_properties") or []
        )
    )
    use_pigz: bool = Field(
        default_factory=lambda: bool(_WIKIDATA_INGEST_DEFAULTS.get("use_pigz", True))
    )
    pigz_path: Optional[str] = Field(
        default_factory=lambda: _WIKIDATA_INGEST_DEFAULTS.get("pigz_path")
    )
    pigz_threads: int = Field(
        default_factory=lambda: int(_WIKIDATA_INGEST_DEFAULTS.get("pigz_threads", 0))
    )
    workers: int = Field(default_factory=lambda: int(_WIKIDATA_INGEST_DEFAULTS.get("workers", 0)))
    lru_cache_size: int = Field(
        default_factory=lambda: int(_WIKIDATA_INGEST_DEFAULTS.get("lru_cache_size", 500000))
    )
    progress_throttle_interval: int = Field(
        default_factory=lambda: int(
            _WIKIDATA_INGEST_DEFAULTS.get("progress_throttle_interval", 32768)
        )
    )
    use_synchronous_commit_off: bool = Field(
        default_factory=lambda: bool(
            _WIKIDATA_INGEST_DEFAULTS.get("use_synchronous_commit_off", True)
        )
    )
    languages: Optional[dict[str, str]] = Field(
        default_factory=lambda: _WIKIDATA_INGEST_DEFAULTS.get("languages")
    )
    subject_mappings: dict[str, str] = Field(
        default_factory=lambda: dict(_WIKIDATA_INGEST_DEFAULTS.get("subject_mappings") or {})
    )


class VocabIngestConfig(FieldTruncationConfig):
    """Configuration for value vocabulary and migration thesauri ingestion."""

    enabled: bool = Field(default_factory=lambda: bool(_VOCAB_INGEST_DEFAULTS.get("enabled", True)))
    input_path: Path = Field(
        default_factory=lambda: Path(_VOCAB_INGEST_DEFAULTS.get("input_path", "data/vocabularies"))
    )
    vocabularies: Optional[list[str]] = Field(
        default_factory=lambda: _VOCAB_INGEST_DEFAULTS.get("vocabularies")
    )
    legacy_thesauri: Optional[list[str]] = Field(
        default_factory=lambda: _VOCAB_INGEST_DEFAULTS.get("legacy_thesauri")
    )
    pref_label_languages: Optional[list[str] | str] = None
    alt_label_languages: Optional[list[str] | str] = None
    normalization: Optional[VocabulariesConfig] = None
    relators_bridge_file: Optional[Path] = Field(
        default_factory=lambda: (
            Path(_VOCAB_INGEST_DEFAULTS["relators_bridge_file"])
            if _VOCAB_INGEST_DEFAULTS.get("relators_bridge_file")
            else None
        )
    )
    bridge_files: Optional[list[Path]] = Field(
        default_factory=lambda: (
            [Path(p) for p in _VOCAB_INGEST_DEFAULTS["bridge_files"]]
            if _VOCAB_INGEST_DEFAULTS.get("bridge_files")
            else None
        )
    )
    bridge_dir: Optional[Path] = Field(
        default_factory=lambda: (
            Path(_VOCAB_INGEST_DEFAULTS["bridge_dir"])
            if _VOCAB_INGEST_DEFAULTS.get("bridge_dir")
            else None
        )
    )
    enable_bridges: bool = Field(
        default_factory=lambda: bool(_VOCAB_INGEST_DEFAULTS.get("enable_bridges", True))
    )


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


class IngestionConfig(BaseModel):
    """Unified ingestion configuration combining filters, harvester defaults, endpoints, and datasets."""

    filter: IngestFilterConfig = Field(default_factory=IngestFilterConfig)
    oai_defaults: OAIDefaultsConfig = Field(default_factory=OAIDefaultsConfig)
    oai: dict[str, OAIIngestConfig] = Field(default_factory=dict)
    wikidata: Optional[WikidataIngestConfig] = None
    vocab: Optional[VocabIngestConfig] = None
    skosmos: dict[str, SkosmosDownloadConfig] = Field(
        default_factory=lambda: {
            k: SkosmosDownloadConfig(**v) if isinstance(v, dict) else v
            for k, v in (_INGESTION_DEFAULTS.get("skosmos") or {}).items()
        }
    )
