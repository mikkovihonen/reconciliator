"""Native format tree enrichment configuration models and helpers."""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, field_validator

from reconciliator.config.defaults import get_bundled_enrichment_defaults
from reconciliator.config.linguistic import DEFAULT_LANGUAGE
from reconciliator.config.vocabularies import SchemeDefinition

_ENRICH_DEFAULTS = get_bundled_enrichment_defaults()


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
