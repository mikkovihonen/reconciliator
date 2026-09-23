"""Agent authority, virtual authority, and constituents configuration models."""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from reconciliator.config.defaults import (
    get_bundled_agent_authority_defaults,
    get_bundled_section_defaults,
    get_bundled_virtual_authority_defaults,
)

_AGENT_AUTHORITY_DEFAULTS = get_bundled_agent_authority_defaults()
_VIRTUAL_AUTHORITY_DEFAULTS = get_bundled_virtual_authority_defaults()
_CONSTITUENTS_DEFAULTS = get_bundled_section_defaults("constituents")


class ConstituentsConfig(BaseModel):
    """Global default for constituent extraction, overridable per source."""

    profile: Optional[str] = Field(default_factory=lambda: _CONSTITUENTS_DEFAULTS.get("profile"))
    mode: Literal["off", "keys_only", "entities"] = Field(
        default_factory=lambda: _CONSTITUENTS_DEFAULTS.get("mode", "keys_only")
    )
    index_works: bool = Field(
        default_factory=lambda: bool(_CONSTITUENTS_DEFAULTS.get("index_works", False))
    )


class AuthorityLifecycleConfig(BaseModel):
    """Lifecycle, split/merge, and redirection configuration for virtual authority clusters."""

    model_config = ConfigDict(extra="ignore")

    auto_redirect_on_merge: bool = Field(
        default_factory=lambda: bool(
            _VIRTUAL_AUTHORITY_DEFAULTS.get("lifecycle", {}).get("auto_redirect_on_merge", True)
        )
    )
    redirect_status_code: int = Field(
        default_factory=lambda: int(
            _VIRTUAL_AUTHORITY_DEFAULTS.get("lifecycle", {}).get("redirect_status_code", 308)
        )
    )
    tombstone_dissolved_clusters: bool = Field(
        default_factory=lambda: bool(
            _VIRTUAL_AUTHORITY_DEFAULTS.get("lifecycle", {}).get(
                "tombstone_dissolved_clusters", True
            )
        )
    )
    split_policy: Literal["retire_and_mint_both", "preserve_primary"] = Field(
        default_factory=lambda: str(
            _VIRTUAL_AUTHORITY_DEFAULTS.get("lifecycle", {}).get(
                "split_policy", "retire_and_mint_both"
            )
        )
    )
    tombstone_retention_days: int = Field(
        default_factory=lambda: int(
            _VIRTUAL_AUTHORITY_DEFAULTS.get("lifecycle", {}).get("tombstone_retention_days", 365)
        )
    )


class AuthorityStatusPolicyConfig(BaseModel):
    """Status assignment rules (established vs provisional) for clusters."""

    model_config = ConfigDict(extra="ignore")

    authoritative_origins: list[str] = Field(
        default_factory=lambda: list(
            _VIRTUAL_AUTHORITY_DEFAULTS.get("status_policy", {}).get("authoritative_origins")
            or ["asteri", "kanto", "isni", "viaf", "wikidata"]
        )
    )
    established_status_name: str = Field(
        default_factory=lambda: str(
            _VIRTUAL_AUTHORITY_DEFAULTS.get("status_policy", {}).get(
                "established_status_name", "established"
            )
        )
    )
    provisional_status_name: str = Field(
        default_factory=lambda: str(
            _VIRTUAL_AUTHORITY_DEFAULTS.get("status_policy", {}).get(
                "provisional_status_name", "provisional"
            )
        )
    )


class MARC21AuthoritySerializationConfig(BaseModel):
    """MARC 21 Authority format output profile configuration."""

    model_config = ConfigDict(extra="ignore")

    organization_code: str = Field(
        default_factory=lambda: str(
            _VIRTUAL_AUTHORITY_DEFAULTS.get("serialization", {})
            .get("marc21", {})
            .get("organization_code", "FI-NL")
        )
    )
    rules_code: str = Field(
        default_factory=lambda: str(
            _VIRTUAL_AUTHORITY_DEFAULTS.get("serialization", {})
            .get("marc21", {})
            .get("rules_code", "rda")
        )
    )
    subject_system_code: str = Field(
        default_factory=lambda: str(
            _VIRTUAL_AUTHORITY_DEFAULTS.get("serialization", {})
            .get("marc21", {})
            .get("subject_system_code", "yso")
        )
    )
    default_indicator_1: str = Field(
        default_factory=lambda: str(
            _VIRTUAL_AUTHORITY_DEFAULTS.get("serialization", {})
            .get("marc21", {})
            .get("default_indicator_1", "1")
        )
    )
    standard_id_source_codes: dict[str, str] = Field(
        default_factory=lambda: dict(
            _VIRTUAL_AUTHORITY_DEFAULTS.get("serialization", {})
            .get("marc21", {})
            .get("standard_id_source_codes")
            or {
                "isni": "isni",
                "wikidata": "wikidata",
                "kanto": "fikan",
                "asteri": "fikan",
                "viaf": "viaf",
                "orcid": "orcid",
            }
        )
    )


class SKOSAuthoritySerializationConfig(BaseModel):
    """SKOS / Linked Data RDF serialization profile configuration."""

    model_config = ConfigDict(extra="ignore")

    default_namespace: str = Field(
        default_factory=lambda: str(
            _VIRTUAL_AUTHORITY_DEFAULTS.get("serialization", {})
            .get("skos", {})
            .get("default_namespace", "http://www.w3.org/2004/02/skos/core#")
        )
    )
    schema_namespace: str = Field(
        default_factory=lambda: str(
            _VIRTUAL_AUTHORITY_DEFAULTS.get("serialization", {})
            .get("skos", {})
            .get("schema_namespace", "http://schema.org/")
        )
    )
    context_url: str = Field(
        default_factory=lambda: str(
            _VIRTUAL_AUTHORITY_DEFAULTS.get("serialization", {})
            .get("skos", {})
            .get("context_url", "https://schema.org/docs/jsonldcontext.json")
        )
    )


class AuthoritySerializationConfig(BaseModel):
    """Authority serialization engine configurations."""

    model_config = ConfigDict(extra="ignore")

    marc21: MARC21AuthoritySerializationConfig = Field(
        default_factory=lambda: MARC21AuthoritySerializationConfig(
            **_VIRTUAL_AUTHORITY_DEFAULTS.get("serialization", {}).get("marc21", {})
        )
    )
    skos: SKOSAuthoritySerializationConfig = Field(
        default_factory=lambda: SKOSAuthoritySerializationConfig(
            **_VIRTUAL_AUTHORITY_DEFAULTS.get("serialization", {}).get("skos", {})
        )
    )


class OpenRefineTypeConfig(BaseModel):
    """Entity type definition for OpenRefine reconciliation manifest."""

    model_config = ConfigDict(extra="ignore")

    id: str
    name: str


class OpenRefineConfig(BaseModel):
    """OpenRefine / W3C Entity Reconciliation API configuration."""

    model_config = ConfigDict(extra="ignore")

    service_name: str = Field(
        default_factory=lambda: str(
            _VIRTUAL_AUTHORITY_DEFAULTS.get("openrefine", {}).get(
                "service_name", "Reconciliator Virtual Authority File"
            )
        )
    )
    identifier_space: str = Field(
        default_factory=lambda: str(
            _VIRTUAL_AUTHORITY_DEFAULTS.get("openrefine", {}).get(
                "identifier_space", "urn:reconciliator:"
            )
        )
    )
    schema_space: str = Field(
        default_factory=lambda: str(
            _VIRTUAL_AUTHORITY_DEFAULTS.get("openrefine", {}).get(
                "schema_space", "http://www.w3.org/2004/02/skos/core#"
            )
        )
    )
    preview_width: int = Field(
        default_factory=lambda: int(
            _VIRTUAL_AUTHORITY_DEFAULTS.get("openrefine", {}).get("preview_width", 430)
        )
    )
    preview_height: int = Field(
        default_factory=lambda: int(
            _VIRTUAL_AUTHORITY_DEFAULTS.get("openrefine", {}).get("preview_height", 300)
        )
    )
    max_candidates: int = Field(
        default_factory=lambda: int(
            _VIRTUAL_AUTHORITY_DEFAULTS.get("openrefine", {}).get("max_candidates", 10)
        )
    )
    default_types: list[OpenRefineTypeConfig] = Field(
        default_factory=lambda: [
            OpenRefineTypeConfig(**t)
            for t in (
                _VIRTUAL_AUTHORITY_DEFAULTS.get("openrefine", {}).get("default_types")
                or [
                    {"id": "agent", "name": "Agent / Person / Organization"},
                    {"id": "work", "name": "Work / Intellectual Entity"},
                ]
            )
        ]
    )


class OAIPMHServerConfig(BaseModel):
    """OAI-PMH 2.0 Authority Provider configuration."""

    model_config = ConfigDict(extra="ignore")

    repository_name: str = Field(
        default_factory=lambda: str(
            _VIRTUAL_AUTHORITY_DEFAULTS.get("oai_pmh_server", {}).get(
                "repository_name", "Reconciliator Virtual Authority OAI Provider"
            )
        )
    )
    admin_email: str = Field(
        default_factory=lambda: str(
            _VIRTUAL_AUTHORITY_DEFAULTS.get("oai_pmh_server", {}).get(
                "admin_email", "admin@reconciliator.local"
            )
        )
    )
    records_per_page: int = Field(
        default_factory=lambda: int(
            _VIRTUAL_AUTHORITY_DEFAULTS.get("oai_pmh_server", {}).get("records_per_page", 100)
        )
    )
    earliest_datestamp: str = Field(
        default_factory=lambda: str(
            _VIRTUAL_AUTHORITY_DEFAULTS.get("oai_pmh_server", {}).get(
                "earliest_datestamp", "2000-01-01T00:00:00Z"
            )
        )
    )
    supported_metadata_prefixes: list[str] = Field(
        default_factory=lambda: list(
            _VIRTUAL_AUTHORITY_DEFAULTS.get("oai_pmh_server", {}).get("supported_metadata_prefixes")
            or ["marc21_authority", "oai_dc"]
        )
    )


class VirtualAuthorityConfig(BaseModel):
    """Root configuration for Reconciliator Virtual Authority File (VAF) layer."""

    model_config = ConfigDict(extra="ignore")

    enabled: bool = Field(
        default_factory=lambda: bool(_VIRTUAL_AUTHORITY_DEFAULTS.get("enabled", True))
    )
    base_uri: str = Field(
        default_factory=lambda: str(
            _VIRTUAL_AUTHORITY_DEFAULTS.get("base_uri", "urn:reconciliator:")
        )
    )
    uri_templates: dict[str, str] = Field(
        default_factory=lambda: dict(
            _VIRTUAL_AUTHORITY_DEFAULTS.get("uri_templates")
            or {
                "agent": "urn:reconciliator:agent:{id}",
                "work": "urn:reconciliator:work:{id}",
                "expression": "urn:reconciliator:expression:{id}",
                "concept": "urn:reconciliator:concept:{id}",
            }
        )
    )
    pid_generator: Literal["uuidv7", "monotonic_int", "opaque_hash"] = Field(
        default_factory=lambda: str(_VIRTUAL_AUTHORITY_DEFAULTS.get("pid_generator", "uuidv7"))
    )
    provenance_sources_priority: list[str] = Field(
        default_factory=lambda: list(
            _VIRTUAL_AUTHORITY_DEFAULTS.get("provenance_sources_priority")
            or ["asteri", "kanto", "isni", "viaf", "wikidata"]
        )
    )
    lifecycle: AuthorityLifecycleConfig = Field(
        default_factory=lambda: AuthorityLifecycleConfig(
            **_VIRTUAL_AUTHORITY_DEFAULTS.get("lifecycle", {})
        )
    )
    status_policy: AuthorityStatusPolicyConfig = Field(
        default_factory=lambda: AuthorityStatusPolicyConfig(
            **_VIRTUAL_AUTHORITY_DEFAULTS.get("status_policy", {})
        )
    )
    serialization: AuthoritySerializationConfig = Field(
        default_factory=lambda: AuthoritySerializationConfig(
            **_VIRTUAL_AUTHORITY_DEFAULTS.get("serialization", {})
        )
    )
    openrefine: OpenRefineConfig = Field(
        default_factory=lambda: OpenRefineConfig(
            **_VIRTUAL_AUTHORITY_DEFAULTS.get("openrefine", {})
        )
    )
    oai_pmh_server: OAIPMHServerConfig = Field(
        default_factory=lambda: OAIPMHServerConfig(
            **_VIRTUAL_AUTHORITY_DEFAULTS.get("oai_pmh_server", {})
        )
    )


class QualifierConfig(BaseModel):
    """Configuration for parsing and normalizing agent qualifiers."""

    model_config = ConfigDict(extra="ignore")

    qualifier_pattern: str = Field(
        default_factory=lambda: str(
            _AGENT_AUTHORITY_DEFAULTS.get("qualifiers", {}).get(
                "qualifier_pattern", r"\(([^)]+)\)$"
            )
        )
    )
    qualifier_subfields: list[str] = Field(
        default_factory=lambda: list(
            _AGENT_AUTHORITY_DEFAULTS.get("qualifiers", {}).get("qualifier_subfields") or ["c"]
        )
    )
    strip_enclosing_brackets: bool = Field(
        default_factory=lambda: bool(
            _AGENT_AUTHORITY_DEFAULTS.get("qualifiers", {}).get("strip_enclosing_brackets", True)
        )
    )
    bracket_pairs: list[list[str]] = Field(
        default_factory=lambda: list(
            _AGENT_AUTHORITY_DEFAULTS.get("qualifiers", {}).get("bracket_pairs")
            or [["(", ")"], ["[", "]"]]
        )
    )
    corporate_category_as_qualifier: bool = Field(
        default_factory=lambda: bool(
            _AGENT_AUTHORITY_DEFAULTS.get("qualifiers", {}).get(
                "corporate_category_as_qualifier", True
            )
        )
    )


class MarcRelationshipConfig(BaseModel):
    """Configuration for mapping MARC 21 Authority 5xx fields to relationship types."""

    model_config = ConfigDict(extra="ignore")

    code_mappings: dict[str, str] = Field(
        default_factory=lambda: dict(
            _AGENT_AUTHORITY_DEFAULTS.get("marc_relationships", {}).get("code_mappings")
            or {
                "a": "predecessor_of",
                "b": "successor_of",
                "t": "hierarchical_superior_of",
                "n": "related_corporate_body",
            }
        )
    )
    designation_patterns: list[dict[str, str]] = Field(
        default_factory=lambda: list(
            _AGENT_AUTHORITY_DEFAULTS.get("marc_relationships", {}).get("designation_patterns")
            or []
        )
    )
    default_relationship_type: str = Field(
        default_factory=lambda: str(
            _AGENT_AUTHORITY_DEFAULTS.get("marc_relationships", {}).get(
                "default_relationship_type", "related_to"
            )
        )
    )


class RDARelationshipConfig(BaseModel):
    """Configuration for mapping RDA/SKOS graph predicates to relationship types."""

    model_config = ConfigDict(extra="ignore")

    predicate_mappings: dict[str, str] = Field(
        default_factory=lambda: dict(
            _AGENT_AUTHORITY_DEFAULTS.get("rda_relationships", {}).get("predicate_mappings") or {}
        )
    )
    note_semantic_patterns: list[dict[str, str]] = Field(
        default_factory=lambda: list(
            _AGENT_AUTHORITY_DEFAULTS.get("rda_relationships", {}).get("note_semantic_patterns")
            or []
        )
    )
    corporate_classes: list[str] = Field(
        default_factory=lambda: list(
            _AGENT_AUTHORITY_DEFAULTS.get("rda_relationships", {}).get("corporate_classes") or []
        )
    )
    person_classes: list[str] = Field(
        default_factory=lambda: list(
            _AGENT_AUTHORITY_DEFAULTS.get("rda_relationships", {}).get("person_classes") or []
        )
    )
    family_classes: list[str] = Field(
        default_factory=lambda: list(
            _AGENT_AUTHORITY_DEFAULTS.get("rda_relationships", {}).get("family_classes") or []
        )
    )


class AgentAuthorityConfig(BaseModel):
    """Master configuration for agent authority parsing, qualifiers, and relationships."""

    model_config = ConfigDict(extra="ignore")

    qualifiers: QualifierConfig = Field(
        default_factory=lambda: QualifierConfig(**_AGENT_AUTHORITY_DEFAULTS.get("qualifiers", {}))
    )
    marc_relationships: MarcRelationshipConfig = Field(
        default_factory=lambda: MarcRelationshipConfig(
            **_AGENT_AUTHORITY_DEFAULTS.get("marc_relationships", {})
        )
    )
    rda_relationships: RDARelationshipConfig = Field(
        default_factory=lambda: RDARelationshipConfig(
            **_AGENT_AUTHORITY_DEFAULTS.get("rda_relationships", {})
        )
    )
