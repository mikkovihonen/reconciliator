"""Semantic, linguistic, controlled vocabularies, and enrichment configuration models."""

from __future__ import annotations

from typing import Any, Literal, Optional, Self

from pydantic import BaseModel, Field, field_validator, model_validator

from reconciliator.config.defaults import (
    get_bundled_enrichment_defaults,
    get_bundled_linguistic_defaults,
    get_bundled_sources_defaults,
    get_bundled_vocabularies_defaults,
)

_LING_DEFAULTS = get_bundled_linguistic_defaults()
_VOCAB_DEFAULTS = get_bundled_vocabularies_defaults()
_ENRICH_DEFAULTS = get_bundled_enrichment_defaults()
_HIERARCHY_DEFAULTS = dict(_VOCAB_DEFAULTS.get("hierarchy") or {})

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
    vocabularies: list[str] = Field(
        default_factory=lambda: list(_VOCAB_DEFAULTS.get("vocabularies", []))
    )
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

    # Subject heading rewriting policy
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
    default_label_language: str = Field(
        default_factory=lambda: str(_VOCAB_DEFAULTS.get("default_label_language", "record"))
    )
    vocabulary_label_languages: dict[str, str] = Field(
        default_factory=lambda: dict(_VOCAB_DEFAULTS.get("vocabulary_label_languages", {}))
    )
    default_classification_scheme: str = Field(
        default_factory=lambda: str(_VOCAB_DEFAULTS.get("default_classification_scheme", "ykl"))
    )
    digital_repository_patterns: list[str] = Field(
        default_factory=lambda: list(_VOCAB_DEFAULTS.get("digital_repository_patterns") or [])
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


def get_default_inject_role_codes() -> bool:
    return bool(_ENRICH_DEFAULTS.get("inject_role_codes", True))


def get_default_normalize_role_terms() -> bool:
    return bool(_ENRICH_DEFAULTS.get("normalize_role_terms", True))


def get_default_role_format() -> str:
    return str(_ENRICH_DEFAULTS.get("role_format", "bridged_uri"))


def get_default_inject_missing_role_terms() -> bool:
    return bool(_ENRICH_DEFAULTS.get("inject_missing_role_terms", False))


class EnrichmentConfig(BaseModel):
    """Fine-grained toggles and profiles for native format tree enrichment."""

    inject_work_links: bool = True
    inject_expression_links: bool = True
    inject_hub_links: bool = True
    inject_same_as: bool = True
    inject_component_links: bool = True
    inject_role_codes: bool = Field(default_factory=get_default_inject_role_codes)
    normalize_role_terms: bool = Field(default_factory=get_default_normalize_role_terms)
    role_format: Literal["bridged_uri", "code", "loc_uri"] = Field(
        default_factory=get_default_role_format  # type: ignore[assignment]
    )
    inject_missing_role_terms: bool = Field(
        default_factory=get_default_inject_missing_role_terms
    )
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

