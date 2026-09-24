"""Controlled vocabularies configuration models and helpers."""

from __future__ import annotations

from typing import Any, Literal, Optional, Self

from pydantic import BaseModel, Field, model_validator

from reconciliator.config.defaults import (
    get_bundled_linguistic_defaults,
    get_bundled_section_defaults,
    get_bundled_sources_defaults,
    get_bundled_vocabularies_defaults,
)
from reconciliator.config.linguistic import (
    DEFAULT_BIOGRAPHICAL_CENTURY_SUFFIXES,
    DEFAULT_BIOGRAPHICAL_DATE_PREFIXES,
    DEFAULT_BIOGRAPHICAL_ERA_AD,
    DEFAULT_BIOGRAPHICAL_ERA_BC,
    DEFAULT_PRESERVED_DIACRITICS,
)

_VOCAB_DEFAULTS = get_bundled_vocabularies_defaults()
_LING_DEFAULTS = get_bundled_linguistic_defaults()
_HIERARCHY_DEFAULTS = dict(_VOCAB_DEFAULTS.get("hierarchy") or {})
_CLASSIFICATION_DEFAULTS = get_bundled_section_defaults("classification")
_DIGITAL_REPO_DEFAULTS = get_bundled_section_defaults("digital_repositories")

DEFAULT_SUBJECT_FIELDS: dict[str, dict[str, Any]] = dict(
    _VOCAB_DEFAULTS.get("subject_fields") or {}
)
DEFAULT_SUBJECT_FIELD_CATEGORY_MAP: dict[str, str] = {
    str(k): str(v.get("category", "")) for k, v in DEFAULT_SUBJECT_FIELDS.items()
}


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


class HierarchyConfig(BaseModel):
    """Configuration for hierarchical vocabulary traversal, reasoning, and semantic similarity."""

    enabled: bool = Field(default_factory=lambda: bool(_HIERARCHY_DEFAULTS.get("enabled", True)))
    max_traversal_depth: int = Field(
        default_factory=lambda: int(_HIERARCHY_DEFAULTS.get("max_traversal_depth", 10))
    )
    max_expansion_depth: int = Field(
        default_factory=lambda: int(_HIERARCHY_DEFAULTS.get("max_expansion_depth", 2))
    )
    path_decay_factor: float = Field(
        default_factory=lambda: float(_HIERARCHY_DEFAULTS.get("path_decay_factor", 0.85))
    )
    similarity_algorithm: Literal["path_decay", "wu_palmer"] = Field(
        default_factory=lambda: str(_HIERARCHY_DEFAULTS.get("similarity_algorithm", "path_decay"))  # type: ignore[arg-type]
    )
    min_similarity_threshold: float = Field(
        default_factory=lambda: float(_HIERARCHY_DEFAULTS.get("min_similarity_threshold", 0.60))
    )
    cache_graphs: bool = Field(
        default_factory=lambda: bool(_HIERARCHY_DEFAULTS.get("cache_graphs", True))
    )
    hierarchical_vocabularies: list[str] = Field(
        default_factory=lambda: list(
            _HIERARCHY_DEFAULTS.get(
                "hierarchical_vocabularies",
                ["yso", "yso-aika", "yso-paikat", "kauno", "slm", "seko"],
            )
        )
    )


class VocabulariesConfig(BaseModel):
    """Centralized controlled vocabulary definitions, priorities, and matching policies."""

    hierarchy: HierarchyConfig = Field(default_factory=HierarchyConfig)
    agent_priority: list[str] = Field(
        default_factory=lambda: list(_VOCAB_DEFAULTS.get("agent_priority", []))
    )
    subject_priority: list[str] = Field(
        default_factory=lambda: list(_VOCAB_DEFAULTS.get("subject_priority", []))
    )
    entity_vocabularies: list[str] = Field(
        default_factory=lambda: list(_VOCAB_DEFAULTS.get("entity_vocabularies", []))
    )
    work_authority_sources: list[str] = Field(
        default_factory=lambda: list(_VOCAB_DEFAULTS.get("work_authority_sources", ["asteri_work"]))
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
    vocabulary_code_map: dict[str, str] = Field(
        default_factory=lambda: dict(_VOCAB_DEFAULTS.get("vocabulary_code_map", {}))
    )

    # --- Subject heading rewriting policy -------------------------------------
    apply_language_subcode: bool = True
    replace_heading_with_pref_label: bool = True
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
        default_factory=lambda: str(_CLASSIFICATION_DEFAULTS.get("default_scheme") or "")
    )
    digital_repository_patterns: list[str] = Field(
        default_factory=lambda: list(_DIGITAL_REPO_DEFAULTS.get("uri_patterns") or [])
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


def get_subject_field_category_map() -> dict[str, str]:
    """Load default MARC subject field to category name mapping."""
    return dict(DEFAULT_SUBJECT_FIELD_CATEGORY_MAP)


def get_category_default_field_map() -> dict[str, str]:
    """Load default category name to MARC subject field mapping."""
    res = {cat: tag for tag, cat in DEFAULT_SUBJECT_FIELD_CATEGORY_MAP.items()}
    if "chronological" in res and "temporal" not in res:
        res["temporal"] = res["chronological"]
    return res


def get_wikidata_author_properties() -> dict[str, str]:
    """Load default mapping of identifier schemes to Wikidata authority property IDs."""
    return dict(_VOCAB_DEFAULTS.get("wikidata_author_properties", {}))


def get_work_authority_sources(config: Optional[Any] = None) -> list[str]:
    """Retrieve the configured list of work-level authority source names."""
    if config is not None:
        if hasattr(config, "vocabularies") and getattr(
            config.vocabularies, "work_authority_sources", None
        ):
            return list(config.vocabularies.work_authority_sources)
        if isinstance(config, dict):
            vocab = config.get("vocabularies", {})
            if "work_authority_sources" in vocab:
                return list(vocab["work_authority_sources"])
    sources = list(_VOCAB_DEFAULTS.get("work_authority_sources") or [])
    if not sources:
        for s_name, s_data in get_bundled_sources_defaults().items():
            if isinstance(s_data, dict) and s_data.get("entity_type") == "work":
                sources.append(s_name)
    return sources
