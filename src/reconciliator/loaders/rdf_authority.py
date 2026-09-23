"""RDF/XML and Turtle Agent Authority loader (RDA & SKOS agent authorities).

Extracts AgentEntity, qualifiers, variant names, affiliations, identifiers,
and relationships from Finto (finaf / Asteri) and RDA agent authority graphs.
All qualifiers and relationship mappings are resolved dynamically via configuration.
"""

from __future__ import annotations

import re
from typing import Any, Optional

from rdflib import BNode, Graph, Literal, Namespace, URIRef
from rdflib.namespace import FOAF, OWL, RDF, RDFS, SKOS

from reconciliator.config import AgentAuthorityConfig, AppConfig
from reconciliator.loaders.base import DecomposedRecord
from reconciliator.loaders.marcxml import extract_agent_qualifiers
from reconciliator.models import (
    AgentEntity,
    AgentRelationshipEntity,
    EntitySubject,
    IdentifierEntity,
    RecordOrigin,
)
from reconciliator.utils.curie import parse_curie

# Standard Namespaces
RDAA = Namespace("http://rdaregistry.info/Elements/a/")
RDAC = Namespace("http://rdaregistry.info/Elements/c/")
RDAU = Namespace("http://rdaregistry.info/Elements/u/")
SCHEMA = Namespace("http://schema.org/")


def _normalize_predicate_key(uri: str) -> str:
    """Normalize a predicate URI into candidate keys for lookup."""
    s = str(uri)
    keys = [s]
    if "#" in s:
        keys.append(s.rsplit("#", 1)[-1])
    if "/" in s:
        keys.append(s.rsplit("/", 1)[-1])
    return keys[0]


def _get_label_for_node(graph: Graph, node: Any) -> Optional[str]:
    """Retrieve best display label for an RDF node."""
    if isinstance(node, Literal):
        return str(node).strip()
    for pred in (
        SKOS.prefLabel,
        RDFS.label,
        FOAF.name,
        RDAA.P50411,
        RDAA.P50407,
        RDAA.P50410,
        SCHEMA.name,
    ):
        for obj in graph.objects(node, pred):
            if isinstance(obj, Literal) and str(obj).strip():
                return str(obj).strip()
    return None


def is_rdf_agent_authority(graph: Graph) -> bool:
    """Check if an RDF graph represents an agent authority entity."""
    agent_classes = {
        RDAC.C10004,  # Person
        RDAC.C10005,  # Corporate body
        RDAC.C10006,  # Family
        RDAC.C10008,  # Family
        FOAF.Person,
        FOAF.Organization,
        FOAF.Group,
        SCHEMA.Person,
        SCHEMA.Organization,
    }
    for _, _, obj in graph.triples((None, RDF.type, None)):
        if obj in agent_classes:
            return True
        if str(obj).endswith(("/C10004", "/C10005", "/C10006", "/C10008")):
            return True

    # Check for RDA Agent predicates or finaf URIs
    for s, p, _ in graph:
        p_str = str(p)
        if "rdaregistry.info/Elements/a/" in p_str:
            return True
        if "finaf" in str(s).lower():
            return True
    return False


def _extract_int_year(val: str) -> Optional[int]:
    """Extract 4-digit integer year from ISO/EDTF/string date."""
    if not val:
        return None
    m = re.search(r"\b(\d{4})\b", val)
    return int(m.group(1)) if m else None


def decompose_rdf_authority(
    graph: Graph,
    source: str = "finaf",
    source_id: Optional[str] = None,
    record_origin: RecordOrigin = "authority",
    config: Optional[Any] = None,
    agent_relationship_sink: Optional[list[AgentRelationshipEntity]] = None,
    subject_sink: Optional[list[EntitySubject]] = None,
) -> DecomposedRecord:
    """Decompose an RDF Graph containing agent authority data into WEMI domain entities."""
    app_cfg: Optional[AppConfig] = None
    if config is None or not hasattr(config, "agent_authority"):
        try:
            app_cfg = AppConfig()
            authority_cfg = app_cfg.agent_authority
        except Exception:
            authority_cfg = AgentAuthorityConfig()
    else:
        authority_cfg = config.agent_authority

    qualifier_cfg = authority_cfg.qualifiers
    rda_cfg = authority_cfg.rda_relationships

    predicate_mappings: dict[str, str] = dict(getattr(rda_cfg, "predicate_mappings", {}) or {})
    note_patterns = getattr(rda_cfg, "note_semantic_patterns", []) or []
    corporate_classes = set(getattr(rda_cfg, "corporate_classes", []) or [])
    person_classes = set(getattr(rda_cfg, "person_classes", []) or [])
    family_classes = set(getattr(rda_cfg, "family_classes", []) or [])

    # Find the primary agent subject node(s)
    candidates: list[URIRef] = []
    for s, _, o in graph.triples((None, RDF.type, None)):
        if isinstance(s, URIRef):
            o_str = str(o)
            if (
                o
                in (
                    RDAC.C10004,
                    RDAC.C10005,
                    RDAC.C10006,
                    RDAC.C10008,
                    FOAF.Person,
                    FOAF.Organization,
                )
                or any(cls in o_str for cls in corporate_classes | person_classes | family_classes)
                or o_str.endswith(("/C10004", "/C10005", "/C10006", "/C10008"))
            ):
                if s not in candidates:
                    candidates.append(s)

    # Fallback to SKOS Concept or any subject with rdaa: authorized access point
    if not candidates:
        for s in graph.subjects(RDF.type, SKOS.Concept):
            if isinstance(s, URIRef) and s not in candidates:
                candidates.append(s)

    if not candidates:
        for p in (RDAA.P50411, RDAA.P50407, RDAA.P50410, SKOS.prefLabel):
            for s in graph.subjects(p, None):
                if isinstance(s, URIRef) and s not in candidates:
                    candidates.append(s)

    if not candidates:
        return DecomposedRecord()

    agents: list[AgentEntity] = []
    agent_relationships: list[AgentRelationshipEntity] = []
    all_identifiers: list[IdentifierEntity] = []

    for agent_node in candidates:
        agent_uri = str(agent_node)
        sid = source_id
        if not sid:
            sid = agent_uri.rsplit("/", 1)[-1].rsplit(":", 1)[-1]

        # Determine agent type (personal vs corporate vs family)
        type_nodes = {str(o) for o in graph.objects(agent_node, RDF.type)}
        is_corp = False
        is_fam = False
        is_pers = False

        for t in type_nodes:
            if (
                t in (str(RDAC.C10005), str(FOAF.Organization), str(SCHEMA.Organization))
                or t.endswith("/C10005")
                or any(cls == t or cls in t for cls in corporate_classes)
            ):
                is_corp = True
            elif (
                t in (str(RDAC.C10006), str(RDAC.C10008))
                or t.endswith(("/C10006", "/C10008"))
                or any(cls == t or cls in t for cls in family_classes)
            ):
                is_fam = True
            elif (
                t in (str(RDAC.C10004), str(FOAF.Person), str(SCHEMA.Person))
                or t.endswith("/C10004")
                or any(cls == t or cls in t for cls in person_classes)
            ):
                is_pers = True

        if is_corp or is_fam:
            is_personal = False
        elif is_pers:
            is_personal = True
        else:
            is_personal = True

        # Extract preferred name / authorized access point
        pref_names: list[str] = []
        for p in (
            RDAA.P50411,  # Authorized access point for person
            RDAA.P50407,  # Authorized access point for corporate body
            RDAA.P50410,  # Authorized access point for family
            SKOS.prefLabel,
            RDFS.label,
            FOAF.name,
        ):
            for o in graph.objects(agent_node, p):
                if isinstance(o, Literal) and str(o).strip():
                    pref_names.append(str(o).strip())

        raw_name = pref_names[0] if pref_names else "Unknown Agent"

        # Qualifiers
        clean_name, parsed_qualifiers = extract_agent_qualifiers(
            raw_name, sub_c_values=(), qualifier_config=qualifier_cfg
        )

        corp_cat = getattr(qualifier_cfg, "corporate_category_as_qualifier", True)
        if corp_cat and not is_personal:
            # Check corporate category properties (e.g. RDAA.P50004 or 368 type of corp)
            for p_uri in (RDAA.P50004, RDAA.P50022):
                for cat_obj in graph.objects(agent_node, p_uri):
                    lbl = _get_label_for_node(graph, cat_obj)
                    if lbl and lbl not in parsed_qualifiers:
                        parsed_qualifiers.append(lbl)

        # Dates & Period of activity
        birth_year: Optional[int] = None
        death_year: Optional[int] = None
        dates_str: Optional[str] = None

        for b_pred in (RDAA.P50121, RDAA.P50037):  # Birth or establishment
            for b_obj in graph.objects(agent_node, b_pred):
                by = _extract_int_year(str(b_obj))
                if by is not None and birth_year is None:
                    birth_year = by

        for d_pred in (RDAA.P50120, RDAA.P50038):  # Death or termination
            for d_obj in graph.objects(agent_node, d_pred):
                dy = _extract_int_year(str(d_obj))
                if dy is not None and death_year is None:
                    death_year = dy

        # Check EDTF / period of activity (e.g. P50236 "1989/2008")
        for act_obj in graph.objects(agent_node, RDAA.P50236):
            act_str = str(act_obj).strip()
            dates_str = act_str
            if "/" in act_str:
                parts = act_str.split("/", 1)
                if birth_year is None:
                    birth_year = _extract_int_year(parts[0])
                if death_year is None:
                    death_year = _extract_int_year(parts[1])
            elif birth_year is None:
                birth_year = _extract_int_year(act_str)

        if not dates_str:
            if birth_year and death_year:
                dates_str = f"{birth_year}-{death_year}"
            elif birth_year:
                dates_str = f"{birth_year}-"
            elif death_year:
                dates_str = f"-{death_year}"

        # Variant names
        variant_names: list[str] = []
        for v_pred in (
            SKOS.altLabel,
            RDAA.P50103,  # Variant name for person
            RDAA.P50025,  # Variant name for corporate body
            RDAA.P50080,  # Variant name for family
        ):
            for v_obj in graph.objects(agent_node, v_pred):
                v_str = str(v_obj).strip()
                if v_str and v_str != raw_name and v_str not in variant_names:
                    v_clean, _ = extract_agent_qualifiers(
                        v_str, sub_c_values=(), qualifier_config=qualifier_cfg
                    )
                    variant_names.append(v_clean)

        # Affiliations
        affiliations: list[str] = []
        for aff_pred in (RDAU.P60447, RDAA.P50095):
            for aff_obj in graph.objects(agent_node, aff_pred):
                aff_label = _get_label_for_node(graph, aff_obj)
                if aff_label and aff_label not in affiliations:
                    affiliations.append(aff_label)

        # Identifiers
        identifiers: list[IdentifierEntity] = []
        for m_pred in (
            SKOS.exactMatch,
            SKOS.closeMatch,
            OWL.sameAs,
            RDAA.P50436,  # ISNI
            RDAA.P50438,  # Identifier
            RDAU.P60052,  # Identifier for agent
        ):
            for match_obj in graph.objects(agent_node, m_pred):
                val = str(match_obj).strip()
                if not val:
                    continue
                scheme, s_val = parse_curie(val, default_source="uri")
                if not s_val:
                    s_val = val
                norm = s_val.replace("-", "").strip().lower()
                identifiers.append(
                    IdentifierEntity(
                        entity_type="agent",
                        entity_id=0,
                        entity_uri=agent_uri,
                        scheme=scheme,
                        value=s_val,
                        value_normalized=norm,
                    )
                )

        # Subjects / Occupations / Places
        if subject_sink is not None:
            for occ_obj in graph.objects(agent_node, RDAA.P50104):
                lbl = _get_label_for_node(graph, occ_obj)
                if lbl:
                    subject_sink.append(
                        EntitySubject(
                            entity_type="agent",
                            entity_id=0,
                            entity_uri=agent_uri,
                            source=source,
                            source_record_id=sid,
                            relation_type="occupation",
                            raw_label=lbl,
                        )
                    )
            for act_obj in graph.objects(agent_node, RDAA.P50102):
                lbl = _get_label_for_node(graph, act_obj)
                if lbl:
                    subject_sink.append(
                        EntitySubject(
                            entity_type="agent",
                            entity_id=0,
                            entity_uri=agent_uri,
                            source=source,
                            source_record_id=sid,
                            relation_type="field_of_activity",
                            raw_label=lbl,
                        )
                    )
            for pl_pred, r_type in (
                (RDAA.P50118, "place_of_birth"),
                (RDAA.P50119, "place_of_death"),
                (RDAA.P50097, "place_of_residence"),
            ):
                for pl_obj in graph.objects(agent_node, pl_pred):
                    lbl = _get_label_for_node(graph, pl_obj)
                    if lbl:
                        subject_sink.append(
                            EntitySubject(
                                entity_type="agent",
                                entity_id=0,
                                entity_uri=agent_uri,
                                source=source,
                                source_record_id=sid,
                                relation_type=r_type,
                                raw_label=lbl,
                            )
                        )

        # Build AgentEntity
        agent = AgentEntity(
            agent_uri=agent_uri,
            source=source,
            source_id=sid,
            record_origin=record_origin,
            name=clean_name,
            name_normalized=clean_name.strip().lower(),
            surname=clean_name.split(",")[0].strip() if "," in clean_name else None,
            dates=dates_str,
            birth_year=birth_year,
            death_year=death_year,
            is_personal=is_personal,
            variant_names=variant_names,
            qualifiers=parsed_qualifiers,
            affiliations=affiliations,
            identifiers=identifiers,
        )
        agents.append(agent)
        all_identifiers.extend(identifiers)

        # Inspect note fields for relationship semantics (e.g. RDAA.P50395 note on person)
        note_rel_override: Optional[str] = None
        for n_pred in (RDAA.P50395, RDAA.P50035, SKOS.scopeNote, SKOS.note):
            for n_obj in graph.objects(agent_node, n_pred):
                n_text = str(n_obj).strip()
                for n_entry in note_patterns:
                    pat = (
                        n_entry.get("pattern", "")
                        if isinstance(n_entry, dict)
                        else getattr(n_entry, "pattern", "")
                    )
                    r_type = (
                        n_entry.get("rel_type", "")
                        if isinstance(n_entry, dict)
                        else getattr(n_entry, "rel_type", "")
                    )
                    if pat and r_type and re.search(pat, n_text, re.IGNORECASE):
                        note_rel_override = r_type
                        break
                if note_rel_override:
                    break
            if note_rel_override:
                break

        # Extract relationships via predicate_mappings
        for p, o in graph.predicate_objects(agent_node):
            p_uri = str(p)
            rel_type = (
                predicate_mappings.get(p_uri)
                or predicate_mappings.get(_normalize_predicate_key(p_uri))
                or predicate_mappings.get(f"rdaa:{_normalize_predicate_key(p_uri)}")
                or predicate_mappings.get(f"rdau:{_normalize_predicate_key(p_uri)}")
            )
            if not rel_type:
                continue

            # If generic related_person and note specifies a more specific relationship, refine it
            if rel_type in ("related_person", "related_agent") and note_rel_override:
                rel_type = note_rel_override

            t_curie: Optional[str] = str(o) if isinstance(o, (URIRef, BNode)) else None
            t_name = _get_label_for_node(graph, o) or (str(o) if isinstance(o, Literal) else None)

            agent_rel = AgentRelationshipEntity(
                source_agent_id=0,
                target_agent_id=0,
                source_agent_curie=agent_uri,
                target_agent_curie=t_curie,
                relationship_type=rel_type,
                target_name=t_name,
            )
            agent_relationships.append(agent_rel)
            if agent_relationship_sink is not None:
                agent_relationship_sink.append(agent_rel)

        # Affiliations as relationships
        for aff in affiliations:
            if not any(
                r.source_agent_curie == agent_uri
                and r.relationship_type in ("affiliation", "affiliated_with", "member_of")
                and r.target_name == aff
                for r in agent_relationships
            ):
                aff_rel = AgentRelationshipEntity(
                    source_agent_id=0,
                    target_agent_id=0,
                    source_agent_curie=agent_uri,
                    relationship_type="affiliation",
                    target_name=aff,
                )
                agent_relationships.append(aff_rel)
                if agent_relationship_sink is not None:
                    agent_relationship_sink.append(aff_rel)

    return DecomposedRecord(
        agents=agents,
        agent_relationships=agent_relationships,
        identifiers=all_identifiers,
        entity_subjects=subject_sink or [],
    )
