"""Declarative schema profiles and autodetection for BIBFRAME (2.0, 3.0) and Finnish BFFI (1.0)."""

from __future__ import annotations

import functools
from pathlib import Path
from typing import Optional, Union

import yaml
from pydantic import BaseModel, Field

_BUILTIN_PROFILES_DIR = (
    Path(__file__).resolve().parent.parent.parent / "defaults" / "bibframe_profiles"
)


class BIBFRAMESchemaProfile(BaseModel):
    """Declarative specification for BIBFRAME / BFFI graph entity navigation and predicate mapping."""

    name: str
    description: str
    version: str
    namespaces: dict[str, str] = Field(default_factory=dict)

    work_classes: list[str] = Field(default_factory=list)
    expression_classes: list[str] = Field(default_factory=list)
    instance_classes: list[str] = Field(default_factory=list)

    instance_to_expression_predicates: list[str] = Field(default_factory=list)
    expression_to_work_predicates: list[str] = Field(default_factory=list)
    instance_to_work_predicates: list[str] = Field(default_factory=list)

    title_predicates: list[str] = Field(default_factory=list)
    main_title_predicates: list[str] = Field(default_factory=list)
    subtitle_predicates: list[str] = Field(default_factory=list)
    part_name_predicates: list[str] = Field(default_factory=list)
    part_number_predicates: list[str] = Field(default_factory=list)

    contribution_predicates: list[str] = Field(default_factory=list)
    agent_predicates: list[str] = Field(default_factory=list)
    role_predicates: list[str] = Field(default_factory=list)
    #: Where a role node keeps its human label. A role is usually a blank node
    #: with a label rather than a URI, so without these the role cannot be read
    #: at all. Defaulted rather than left empty: every profile needs them, and an
    #: empty list silently yields roleless contributions.
    role_label_predicates: list[str] = Field(
        default_factory=lambda: ["rdfs:label", "skos:prefLabel", "rdf:value"]
    )

    identifier_predicates: list[str] = Field(default_factory=list)
    subject_predicates: list[str] = Field(default_factory=list)
    source_predicates: list[str] = Field(default_factory=list)
    default_subject_field: Optional[str] = None
    subject_type_fields: dict[str, str] = Field(default_factory=dict)
    subject_label_predicates: list[str] = Field(default_factory=list)
    provision_activity_predicates: list[str] = Field(default_factory=list)
    language_predicates: list[str] = Field(default_factory=list)
    music_performance_predicates: list[str] = Field(default_factory=list)

    agent_personal_classes: list[str] = Field(default_factory=list)
    agent_corporate_classes: list[str] = Field(default_factory=list)
    agent_match_key_predicates: list[str] = Field(default_factory=list)
    agent_default_tags: dict[str, str] = Field(default_factory=dict)
    agent_primary_contribution_classes: list[str] = Field(default_factory=list)
    agent_label_predicates: list[str] = Field(default_factory=list)
    agent_simple_predicates: list[str] = Field(default_factory=list)

    work_creator_roles: list[str] = Field(default_factory=list)
    expression_realizer_roles: list[str] = Field(default_factory=list)
    role_uri_prefixes: list[str] = Field(default_factory=list)
    role_label_mappings: dict[str, str] = Field(default_factory=dict)

    #: Classes that mark the other end of a part/whole predicate as a *series*
    #: rather than a container. `bf:partOf` and `bf:hasPart` are the containment
    #: relation on a Work - an anthology and its stories - and series membership
    #: only when the other end is a series, which BIBFRAME 2 usually states with
    #: `bf:hasSeries` instead. All three profiles mapped them to series
    #: membership unconditionally, so BIBFRAME aggregates were recorded as series
    #: members.
    series_classes: list[str] = Field(default_factory=lambda: ["bf:Series"])

    title_variant_classes: list[str] = Field(default_factory=list)
    identifier_scheme_mappings: dict[str, list[str]] = Field(default_factory=dict)

    provision_date_predicates: list[str] = Field(default_factory=list)
    provision_publisher_predicates: list[str] = Field(default_factory=list)
    provision_place_predicates: list[str] = Field(default_factory=list)
    provision_edition_predicates: list[str] = Field(default_factory=list)
    provision_extent_predicates: list[str] = Field(default_factory=list)

    carrier_type_mappings: dict[str, list[str]] = Field(default_factory=dict)
    item_predicates: list[str] = Field(default_factory=list)
    item_barcode_predicates: list[str] = Field(default_factory=list)
    item_shelfmark_predicates: list[str] = Field(default_factory=list)
    item_holding_predicates: list[str] = Field(default_factory=list)

    relationship_predicates: dict[str, str] = Field(default_factory=dict)
    work_type_classes: dict[str, str] = Field(default_factory=dict)
    pmo_performance_classes: list[str] = Field(default_factory=list)
    pmo_performed_work_predicates: list[str] = Field(default_factory=list)

    same_as_predicate: str = "http://www.w3.org/2002/07/owl#sameAs"
    default_music_vocabulary: Optional[str] = None
    synthesize_missing_expression: bool = False
    provenance_regex: str = r"^(\d{3})([0-9\s]{2})(.*)$"

    provenance_predicate: Optional[str] = None

    def expand_curie(self, curie_or_uri: str) -> str:
        """Expand a CURIE (e.g., 'bf:Work') to full URI using registered namespaces."""
        if not curie_or_uri:
            return ""
        if "://" in curie_or_uri or curie_or_uri.startswith("urn:"):
            return curie_or_uri
        if ":" in curie_or_uri:
            prefix, local = curie_or_uri.split(":", 1)
            if prefix in self.namespaces:
                return f"{self.namespaces[prefix]}{local}"
        return curie_or_uri

    def compact_uri(self, uri: str) -> str:
        """Compact a full URI into a CURIE using registered namespaces."""
        if not uri:
            return ""
        for prefix, ns in self.namespaces.items():
            if uri.startswith(ns):
                return f"{prefix}:{uri[len(ns) :]}"
        return uri

    @functools.cached_property
    def expanded_work_classes(self) -> set[str]:
        return {self.expand_curie(c) for c in self.work_classes} | set(self.work_classes)

    @functools.cached_property
    def expanded_expression_classes(self) -> set[str]:
        return {self.expand_curie(c) for c in self.expression_classes} | set(
            self.expression_classes
        )

    @functools.cached_property
    def expanded_instance_classes(self) -> set[str]:
        return {self.expand_curie(c) for c in self.instance_classes} | set(self.instance_classes)

    @functools.cached_property
    def expanded_instance_to_expression_predicates(self) -> set[str]:
        return {self.expand_curie(p) for p in self.instance_to_expression_predicates} | set(
            self.instance_to_expression_predicates
        )

    @functools.cached_property
    def expanded_expression_to_work_predicates(self) -> set[str]:
        return {self.expand_curie(p) for p in self.expression_to_work_predicates} | set(
            self.expression_to_work_predicates
        )

    @functools.cached_property
    def expanded_instance_to_work_predicates(self) -> set[str]:
        return {self.expand_curie(p) for p in self.instance_to_work_predicates} | set(
            self.instance_to_work_predicates
        )

    @functools.cached_property
    def expanded_title_predicates(self) -> set[str]:
        return {self.expand_curie(p) for p in self.title_predicates} | set(self.title_predicates)

    @functools.cached_property
    def expanded_main_title_predicates(self) -> set[str]:
        return {self.expand_curie(p) for p in self.main_title_predicates} | set(
            self.main_title_predicates
        )

    @functools.cached_property
    def expanded_subtitle_predicates(self) -> set[str]:
        return {self.expand_curie(p) for p in self.subtitle_predicates} | set(
            self.subtitle_predicates
        )

    @functools.cached_property
    def expanded_part_name_predicates(self) -> set[str]:
        return {self.expand_curie(p) for p in self.part_name_predicates} | set(
            self.part_name_predicates
        )

    @functools.cached_property
    def expanded_part_number_predicates(self) -> set[str]:
        return {self.expand_curie(p) for p in self.part_number_predicates} | set(
            self.part_number_predicates
        )

    @functools.cached_property
    def expanded_contribution_predicates(self) -> set[str]:
        return {self.expand_curie(p) for p in self.contribution_predicates} | set(
            self.contribution_predicates
        )

    @functools.cached_property
    def expanded_agent_predicates(self) -> set[str]:
        return {self.expand_curie(p) for p in self.agent_predicates} | set(self.agent_predicates)

    @functools.cached_property
    def expanded_role_predicates(self) -> set[str]:
        return {self.expand_curie(p) for p in self.role_predicates} | set(self.role_predicates)

    @functools.cached_property
    def expanded_series_classes(self) -> set[str]:
        return {self.expand_curie(c) for c in self.series_classes} | set(self.series_classes)

    @functools.cached_property
    def expanded_role_label_predicates(self) -> set[str]:
        return {self.expand_curie(p) for p in self.role_label_predicates} | set(
            self.role_label_predicates
        )

    @functools.cached_property
    def expanded_identifier_predicates(self) -> set[str]:
        return {self.expand_curie(p) for p in self.identifier_predicates} | set(
            self.identifier_predicates
        )

    @functools.cached_property
    def expanded_subject_predicates(self) -> set[str]:
        return {self.expand_curie(p) for p in self.subject_predicates} | set(
            self.subject_predicates
        )

    @functools.cached_property
    def expanded_subject_label_predicates(self) -> set[str]:
        return {self.expand_curie(p) for p in self.subject_label_predicates} | set(
            self.subject_label_predicates
        )

    @functools.cached_property
    def expanded_source_predicates(self) -> set[str]:
        return {self.expand_curie(p) for p in self.source_predicates} | set(
            self.source_predicates
        )

    @functools.cached_property
    def expanded_provision_activity_predicates(self) -> set[str]:
        return {self.expand_curie(p) for p in self.provision_activity_predicates} | set(
            self.provision_activity_predicates
        )

    @functools.cached_property
    def expanded_language_predicates(self) -> set[str]:
        return {self.expand_curie(p) for p in self.language_predicates} | set(
            self.language_predicates
        )

    @functools.cached_property
    def expanded_music_performance_predicates(self) -> set[str]:
        return {self.expand_curie(p) for p in self.music_performance_predicates} | set(
            self.music_performance_predicates
        )

    @functools.cached_property
    def expanded_provenance_predicate(self) -> Optional[str]:
        if not self.provenance_predicate:
            return None
        return self.expand_curie(self.provenance_predicate)


    @functools.cached_property
    def expanded_agent_corporate_classes(self) -> set[str]:
        return {self.expand_curie(c) for c in self.agent_corporate_classes} | set(
            self.agent_corporate_classes
        )

    @functools.cached_property
    def expanded_agent_match_key_predicates(self) -> set[str]:
        return {self.expand_curie(p) for p in self.agent_match_key_predicates} | set(
            self.agent_match_key_predicates
        )

    @functools.cached_property
    def expanded_agent_primary_contribution_classes(self) -> set[str]:
        return {self.expand_curie(c) for c in self.agent_primary_contribution_classes} | set(
            self.agent_primary_contribution_classes
        )

    @functools.cached_property
    def expanded_agent_label_predicates(self) -> set[str]:
        return {self.expand_curie(p) for p in self.agent_label_predicates} | set(
            self.agent_label_predicates
        )

    @functools.cached_property
    def expanded_agent_simple_predicates(self) -> set[str]:
        return {self.expand_curie(p) for p in self.agent_simple_predicates} | set(
            self.agent_simple_predicates
        )

    @functools.cached_property
    def expanded_title_variant_classes(self) -> set[str]:
        return {self.expand_curie(c) for c in self.title_variant_classes} | set(
            self.title_variant_classes
        )

    @functools.cached_property
    def expanded_provision_date_predicates(self) -> set[str]:
        return {self.expand_curie(p) for p in self.provision_date_predicates} | set(
            self.provision_date_predicates
        )

    @functools.cached_property
    def expanded_provision_publisher_predicates(self) -> set[str]:
        return {self.expand_curie(p) for p in self.provision_publisher_predicates} | set(
            self.provision_publisher_predicates
        )

    @functools.cached_property
    def expanded_provision_place_predicates(self) -> set[str]:
        return {self.expand_curie(p) for p in self.provision_place_predicates} | set(
            self.provision_place_predicates
        )

    @functools.cached_property
    def expanded_provision_edition_predicates(self) -> set[str]:
        return {self.expand_curie(p) for p in self.provision_edition_predicates} | set(
            self.provision_edition_predicates
        )

    @functools.cached_property
    def expanded_provision_extent_predicates(self) -> set[str]:
        return {self.expand_curie(p) for p in self.provision_extent_predicates} | set(
            self.provision_extent_predicates
        )

    @functools.cached_property
    def expanded_item_predicates(self) -> set[str]:
        return {self.expand_curie(p) for p in self.item_predicates} | set(self.item_predicates)

    @functools.cached_property
    def expanded_item_barcode_predicates(self) -> set[str]:
        return {self.expand_curie(p) for p in self.item_barcode_predicates} | set(
            self.item_barcode_predicates
        )

    @functools.cached_property
    def expanded_item_shelfmark_predicates(self) -> set[str]:
        return {self.expand_curie(p) for p in self.item_shelfmark_predicates} | set(
            self.item_shelfmark_predicates
        )

    @functools.cached_property
    def expanded_item_holding_predicates(self) -> set[str]:
        return {self.expand_curie(p) for p in self.item_holding_predicates} | set(
            self.item_holding_predicates
        )

    @functools.cached_property
    def expanded_relationship_predicates(self) -> dict[str, str]:
        return {self.expand_curie(k): v for k, v in self.relationship_predicates.items()}

    @functools.cached_property
    def expanded_work_type_classes(self) -> dict[str, str]:
        return {k: self.expand_curie(v) for k, v in self.work_type_classes.items()}

    @functools.cached_property
    def expanded_pmo_performance_classes(self) -> set[str]:
        return {self.expand_curie(c) for c in self.pmo_performance_classes} | set(
            self.pmo_performance_classes
        )

    @functools.cached_property
    def expanded_pmo_performed_work_predicates(self) -> set[str]:
        return {self.expand_curie(p) for p in self.pmo_performed_work_predicates} | set(
            self.pmo_performed_work_predicates
        )

    @functools.cached_property
    def expanded_identifier_scheme_mappings(self) -> dict[str, set[str]]:
        return {
            scheme: {self.expand_curie(cand) for cand in candidates} | set(candidates)
            for scheme, candidates in self.identifier_scheme_mappings.items()
        }


@functools.lru_cache(maxsize=32)
def get_profile(name_or_path: Optional[str] = None) -> BIBFRAMESchemaProfile:
    """Load a BIBFRAME schema profile by name ('bffi_1', 'bibframe_3', 'bibframe_2') or from a file path."""
    target_name = name_or_path or "bibframe_3"
    target_path = Path(target_name)

    # 1. If pointing directly to an existing YAML file
    if target_path.exists() and target_path.is_file():
        with open(target_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        return BIBFRAMESchemaProfile.model_validate(data)

    # 2. Check alias normalizations
    clean_name = target_name.lower().replace("-", "_")
    alias_map = {
        "bffi": "bffi_1",
        "bffi_1": "bffi_1",
        "bffi_1_0": "bffi_1",
        "bibframe": "bibframe_3",
        "bibframe_3": "bibframe_3",
        "bibframe_3_0": "bibframe_3",
        "bibframe_2": "bibframe_2",
        "bibframe_2_0": "bibframe_2",
        "bf": "bibframe_3",
        "bf2": "bibframe_2",
        "bf3": "bibframe_3",
    }
    canonical = alias_map.get(clean_name, clean_name)

    builtin_file = _BUILTIN_PROFILES_DIR / f"{canonical}.yaml"
    if builtin_file.exists():
        with open(builtin_file, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        return BIBFRAMESchemaProfile.model_validate(data)

    raise ValueError(
        f"Unknown BIBFRAME schema profile '{target_name}'. Available built-in profiles: 'bffi_1', 'bibframe_3', 'bibframe_2', or provide a path to a custom YAML profile."
    )


def detect_profile(
    namespaces: Optional[Union[dict[str, str], set[str], list[str]]] = None,
    classes_or_predicates: Optional[Union[set[str], list[str]]] = None,
) -> BIBFRAMESchemaProfile:
    """Detect appropriate schema profile based on namespace URIs and type / predicate signatures."""
    ns_values: set[str] = set()
    if namespaces:
        if isinstance(namespaces, dict):
            ns_values = set(namespaces.values())
        else:
            ns_values = set(namespaces)

    terms: set[str] = set()
    if classes_or_predicates:
        terms = set(classes_or_predicates)

    # Check for Finnish BFFI signature (supports both colon and slash variants)
    bffi_uri = "http://urn.fi/URN:NBN:fi:schema:bffi"
    if any(bffi_uri in ns for ns in ns_values) or any(
        bffi_uri in t or t.startswith("bffi:") for t in terms
    ):
        return get_profile("bffi_1")

    # Check for BIBFRAME 3.0 (PMO / Hub / bflc)
    pmo_terms = {
        "http://id.loc.gov/ontologies/bibframe/Hub",
        "http://id.loc.gov/ontologies/bibframe/mediumOfPerformance",
        "http://id.loc.gov/ontologies/bibframe/musicMedium",
        "bf:Hub",
        "bf:mediumOfPerformance",
        "bf:musicMedium",
    }
    if any(t in terms for t in pmo_terms):
        return get_profile("bibframe_3")

    # Default to standard BIBFRAME 3.0 (modern standard)
    return get_profile("bibframe_3")
