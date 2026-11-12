"""Record normalization configuration models and helpers."""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field

from reconciliator.config.defaults import (
    get_bundled_record_defaults,
    get_bundled_sources_defaults,
)
from reconciliator.config.semantics import (
    LinguisticConfig,
    get_subject_field_category_map,
)

_REC_DEFAULTS = get_bundled_record_defaults()

DEFAULT_NON_FILING_ARTICLES: dict[str, list[str]] = dict(
    _REC_DEFAULTS.get("non_filing_articles") or {}
)
_DEFAULT_ISO_639_2_TO_1: dict[str, str] = {
    "fin": "fi",
    "swe": "sv",
    "eng": "en",
    "ger": "de",
    "deu": "de",
    "fre": "fr",
    "fra": "fr",
}
DEFAULT_ISO_639_2_TO_1: dict[str, str] = dict(
    _REC_DEFAULTS.get("iso_639_2_to_1") or _DEFAULT_ISO_639_2_TO_1
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


class RecordNormalizationConfig(BaseModel):
    """Configuration for bibliographic record parsing and normalization heuristics."""

    strip_isbd_punctuation: bool = Field(
        default_factory=lambda: bool(_REC_DEFAULTS.get("strip_isbd_punctuation", True))
    )
    isbd_punctuation: str = Field(
        default_factory=lambda: str(_REC_DEFAULTS.get("isbd_punctuation") or "")
    )
    non_filing_articles: dict[str, list[str]] = Field(
        default_factory=lambda: {k: list(v) for k, v in DEFAULT_NON_FILING_ARTICLES.items()}
    )
    iso_639_2_to_1: dict[str, str] = Field(default_factory=lambda: dict(DEFAULT_ISO_639_2_TO_1))
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
        default_factory=lambda: str(_REC_DEFAULTS.get("default_classification_scheme") or "ykl")
    )
    default_carrier_source: str = Field(
        default_factory=lambda: str(_REC_DEFAULTS.get("default_carrier_source") or "melinda")
    )
    genre_work_types: dict[str, dict[str, str]] = Field(
        default_factory=lambda: {
            k: dict(v) for k, v in _REC_DEFAULTS.get("genre_work_types", {}).items()
        }
    )
    genre_patterns: dict[str, list[str]] = Field(
        default_factory=lambda: {
            k: list(v) for k, v in _REC_DEFAULTS.get("genre_patterns", {}).items()
        }
    )
    rda_content_types: dict[str, str] = Field(
        default_factory=lambda: dict(_REC_DEFAULTS.get("rda_content_types", {}))
    )
    content_type_work_types: dict[str, str] = Field(
        default_factory=lambda: dict(_REC_DEFAULTS.get("content_type_work_types", {}))
    )
    tactile_content_codes: list[str] = Field(
        default_factory=lambda: list(_REC_DEFAULTS.get("tactile_content_codes", []))
    )
    carrier_type_mappings: dict[str, str] = Field(
        default_factory=lambda: dict(_REC_DEFAULTS.get("carrier_type_mappings", {}))
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
    custom_relator_terms: dict[str, str] = Field(default_factory=dict)
    subject_field_category_map: dict[str, str] = Field(
        default_factory=get_subject_field_category_map
    )
    default_subject_field: str = Field(default="650")
    linguistic: LinguisticConfig = Field(default_factory=LinguisticConfig)


def get_default_carrier_source(config: Optional[Any] = None) -> str:
    """Retrieve the configured default carrier source name."""
    if config is not None:
        if hasattr(config, "record_normalization") and getattr(
            config.record_normalization, "default_carrier_source", None
        ):
            return str(config.record_normalization.default_carrier_source)
        if isinstance(config, dict):
            rec = config.get("record_normalization", {})
            if "default_carrier_source" in rec:
                return str(rec["default_carrier_source"])
    val = _REC_DEFAULTS.get("default_carrier_source")
    if val:
        return str(val)
    for s_name, s_data in get_bundled_sources_defaults().items():
        if isinstance(s_data, dict) and s_data.get("entity_type") == "carrier":
            return s_name
    return "melinda"


def get_genre_work_types(config: Optional[Any] = None) -> dict[str, dict[str, str]]:
    """Retrieve mapping of controlled genre/form vocabularies to work types."""
    if config is not None:
        if hasattr(config, "record_normalization") and getattr(
            config.record_normalization, "genre_work_types", None
        ):
            return {k: dict(v) for k, v in config.record_normalization.genre_work_types.items()}
        if isinstance(config, dict):
            rec = config.get("record_normalization", {})
            if "genre_work_types" in rec:
                return {k: dict(v) for k, v in rec["genre_work_types"].items()}
    return {k: dict(v) for k, v in _REC_DEFAULTS.get("genre_work_types", {}).items()}


def get_genre_patterns(config: Optional[Any] = None) -> dict[str, list[str]]:
    """Retrieve mapping of work types to genre/form substring patterns."""
    if config is not None:
        if hasattr(config, "record_normalization") and getattr(
            config.record_normalization, "genre_patterns", None
        ):
            return {k: list(v) for k, v in config.record_normalization.genre_patterns.items()}
        if isinstance(config, dict):
            rec = config.get("record_normalization", {})
            if "genre_patterns" in rec:
                return {k: list(v) for k, v in rec["genre_patterns"].items()}
    return {k: list(v) for k, v in _REC_DEFAULTS.get("genre_patterns", {}).items()}


def get_rda_content_types(config: Optional[Any] = None) -> dict[str, str]:
    """Retrieve mapping of MARC 336 $b RDA content codes to expression content types."""
    if config is not None:
        if hasattr(config, "record_normalization") and getattr(
            config.record_normalization, "rda_content_types", None
        ):
            return dict(config.record_normalization.rda_content_types)
        if isinstance(config, dict):
            rec = config.get("record_normalization", {})
            if "rda_content_types" in rec:
                return dict(rec["rda_content_types"])
    return dict(_REC_DEFAULTS.get("rda_content_types", {}))


def get_content_type_work_types(config: Optional[Any] = None) -> dict[str, str]:
    """Retrieve mapping of MARC 336 $b RDA content codes to work types."""
    if config is not None:
        if hasattr(config, "record_normalization") and getattr(
            config.record_normalization, "content_type_work_types", None
        ):
            return dict(config.record_normalization.content_type_work_types)
        if isinstance(config, dict):
            rec = config.get("record_normalization", {})
            if "content_type_work_types" in rec:
                return dict(rec["content_type_work_types"])
    return dict(_REC_DEFAULTS.get("content_type_work_types", {}))


def get_tactile_content_codes(config: Optional[Any] = None) -> set[str]:
    """Retrieve set of RDA content codes designating tactile material."""
    if config is not None:
        if hasattr(config, "record_normalization") and getattr(
            config.record_normalization, "tactile_content_codes", None
        ):
            return set(config.record_normalization.tactile_content_codes)
        if isinstance(config, dict):
            rec = config.get("record_normalization", {})
            if "tactile_content_codes" in rec:
                return set(rec["tactile_content_codes"])
    return set(_REC_DEFAULTS.get("tactile_content_codes", []))


def get_carrier_type_mappings(config: Optional[Any] = None) -> dict[str, str]:
    """Retrieve mapping of MARC 338 $b RDA carrier codes to carrier types."""
    if config is not None:
        if hasattr(config, "record_normalization") and getattr(
            config.record_normalization, "carrier_type_mappings", None
        ):
            return dict(config.record_normalization.carrier_type_mappings)
        if isinstance(config, dict):
            rec = config.get("record_normalization", {})
            if "carrier_type_mappings" in rec:
                return dict(rec["carrier_type_mappings"])
    return dict(_REC_DEFAULTS.get("carrier_type_mappings", {}))
