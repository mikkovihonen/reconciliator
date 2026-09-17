"""BIBFRAME and BFFI Work-Expression-Manifestation-Item (WEMI) decomposition engine.

Implements the 6-condition Graph Decomposition Decision Matrix:
- C-1: Finnish BFFI 1.0 4-tier direct extraction (Manifestation -> Expression -> Work)
- C-2: Finnish BFFI 1.0 missing expression node (Manifestation -> Work) with synthesized default expression
- C-3: LC BIBFRAME 3.0 Hub hoisting (Instance -> Work -> Hub) with Hub as canonical Work centroid
- C-4: LC BIBFRAME 2.0 / 3.0 translation decomposition (Instance -> Work -> translationOf)
- C-5: Performed Music Ontology (PMO) Performance realization (Performance -> performedWork)
- C-6: LC BIBFRAME 2.0 single-work partitioning (Instance -> Work) splitting attributes into W and E

Extracts PMO mediums of performance, bffi:marcKey provenance, structural inter-work relationships,
and physical/electronic item holdings.
"""

from __future__ import annotations

import hashlib
import logging
import re
from typing import Any, Iterable, Optional

from rdflib import RDF, RDFS, SKOS, BNode, Graph, IdentifiedNode, Literal, URIRef

from reconciliator.config import (
    get_category_default_field_map,
    get_subject_field_category_map,
)
from reconciliator.loaders.bibframe.base import (
    normalize_role_uri_or_label,
)
from reconciliator.loaders.bibframe.profiles import (
    BIBFRAMESchemaProfile,
    detect_profile,
)
from reconciliator.loaders.subjects import (
    fold_subjects,
    subject_footprint,
    vocabulary_hint_from_uri,
)
from reconciliator.models import (
    AgentEntity,
    CarrierType,
    ContributionEntity,
    EntityKey,
    EntitySubject,
    ExpressionContentType,
    ExpressionEntity,
    IdentifierEntity,
    ItemEntity,
    ManifestationEntity,
    ManifestationExpressionAssociation,
    MatchRecord,
    MediumOfPerformance,
    WorkEntity,
    WorkRelationship,
    WorkType,
)
from reconciliator.normalizers.blocking import (
    compute_author_key,
    compute_music_work_key,
    compute_title_stem,
    compute_work_blocking_key,
)
from reconciliator.template import WEMISerializationEngine
from reconciliator.utils import to_manifestation_entity_keys
from reconciliator.vocabularies import detect_vocabulary

logger = logging.getLogger(__name__)


def extract_marc_key_components(
    marc_key: str, profile: Optional[BIBFRAMESchemaProfile] = None
) -> Optional[tuple[str, str, str]]:
    """Extract (tag, indicators, subfields) from a provenance literal string.

    Example:
        '1001 $aWaltari, Mika,$d1908-1979,$ewriter' -> ('100', '1 ', '$aWaltari, Mika,$d1908-1979,$ewriter')
    """
    from reconciliator.loaders.bibframe.profiles import get_profile

    p = profile or get_profile()
    m = re.match(p.provenance_regex, marc_key.strip())
    if m:
        return m.group(1), m.group(2), m.group(3)
    return None


def _get_literals(g: Graph, subject: IdentifiedNode, predicates: list[str] | set[str]) -> list[str]:
    """Retrieve unique string values for given predicates on a subject."""
    results: list[str] = []
    for pred in predicates:
        pred_ref = URIRef(pred) if not isinstance(pred, URIRef) else pred
        for obj in g.objects(subject, pred_ref):
            if isinstance(obj, Literal):
                val = str(obj).strip()
                if val and val not in results:
                    results.append(val)
            elif isinstance(obj, IdentifiedNode):
                for label in g.objects(obj, RDFS.label):
                    val = str(label).strip()
                    if val and val not in results:
                        results.append(val)
                for label in g.objects(obj, SKOS.prefLabel):
                    val = str(label).strip()
                    if val and val not in results:
                        results.append(val)
    return results


def _get_uris(g: Graph, subject: IdentifiedNode, predicates: list[str] | set[str]) -> list[str]:
    """Retrieve unique URIRef strings for given predicates on a subject."""
    results: list[str] = []
    for pred in predicates:
        pred_ref = URIRef(pred) if not isinstance(pred, URIRef) else pred
        for obj in g.objects(subject, pred_ref):
            if isinstance(obj, URIRef):
                u = str(obj).strip()
                if u and u not in results:
                    results.append(u)
    return results


def _extract_title_elements(
    g: Graph, node: IdentifiedNode, profile: BIBFRAMESchemaProfile
) -> tuple[str, Optional[str], Optional[str], list[str]]:
    """Extract (main_title, subtitle, uniform_title, variant_titles) from a node."""
    main_title = ""
    subtitle: Optional[str] = None
    uniform_title: Optional[str] = None
    variant_titles: list[str] = []

    for pred in profile.expanded_title_predicates:
        for title_obj in g.objects(node, URIRef(pred)):
            if isinstance(title_obj, Literal):
                val = str(title_obj).strip()
                if not main_title:
                    main_title = val
                else:
                    variant_titles.append(val)
            elif isinstance(title_obj, IdentifiedNode):
                types = {str(t) for t in g.objects(title_obj, RDF.type)}
                is_variant = bool(profile.expanded_title_variant_classes.intersection(types))

                m_titles = _get_literals(g, title_obj, profile.expanded_main_title_predicates)
                s_titles = _get_literals(g, title_obj, profile.expanded_subtitle_predicates)
                p_names = _get_literals(g, title_obj, profile.expanded_part_name_predicates)
                p_numbers = _get_literals(g, title_obj, profile.expanded_part_number_predicates)

                mt = m_titles[0] if m_titles else ""
                st = s_titles[0] if s_titles else None
                parts = p_numbers + p_names
                if parts and mt:
                    mt = f"{mt} {' '.join(parts)}"

                if is_variant:
                    if mt and mt not in variant_titles:
                        variant_titles.append(mt)
                else:
                    if mt and not main_title:
                        main_title = mt
                        if st and not subtitle:
                            subtitle = st
                    elif mt and mt not in variant_titles:
                        variant_titles.append(mt)

    if not main_title:
        labels = _get_literals(g, node, [str(RDFS.label), str(SKOS.prefLabel)])
        if labels:
            main_title = labels[0]

    return main_title, subtitle, uniform_title, variant_titles


def _extract_language(
    g: Graph, node: IdentifiedNode, profile: BIBFRAMESchemaProfile
) -> Optional[str]:
    """Extract language code from node."""
    for l_pred in profile.expanded_language_predicates:
        for l_obj in g.objects(node, URIRef(l_pred)):
            if isinstance(l_obj, URIRef):
                return str(l_obj).rstrip("/").split("/")[-1].lower()
            elif isinstance(l_obj, Literal):
                return str(l_obj).strip().lower()
    return None


def _extract_node_agents(
    g: Graph,
    node: IdentifiedNode,
    profile: BIBFRAMESchemaProfile,
    source: str = "bibframe",
    relator_mappings: Optional[dict[str, str]] = None,
) -> list[tuple[AgentEntity, str, bool, Optional[str]]]:
    """Extract agents associated with a node.

    Returns list of (AgentEntity, role_code, is_primary, marc_key).
    """
    agents: list[tuple[AgentEntity, str, bool, Optional[str]]] = []

    for contrib_pred in profile.expanded_contribution_predicates:
        for contrib_obj in g.objects(node, URIRef(contrib_pred)):
            if not isinstance(contrib_obj, IdentifiedNode):
                continue

            # Provenance literal
            marc_key: Optional[str] = None
            if profile.expanded_provenance_predicate:
                mk_lits = _get_literals(g, contrib_obj, [profile.expanded_provenance_predicate])
                if mk_lits:
                    marc_key = mk_lits[0]

            # Primary contribution check
            contrib_types = {str(t) for t in g.objects(contrib_obj, RDF.type)}
            is_primary = bool(
                profile.expanded_agent_primary_contribution_classes.intersection(contrib_types)
            )
            if not is_primary and marc_key:
                parsed_mk = extract_marc_key_components(marc_key, profile=profile)
                if parsed_mk and parsed_mk[0] in (
                    profile.agent_default_tags.get("personal", "100"),
                    profile.agent_default_tags.get("corporate", "110"),
                    profile.agent_default_tags.get("meeting", "111"),
                ):
                    is_primary = True

            # Role extraction
            role = "aut" if is_primary else "ctb"
            for role_pred in profile.expanded_role_predicates:
                for role_obj in g.objects(contrib_obj, URIRef(role_pred)):
                    # A role is usually a blank node carrying a label -
                    # `bf:role [ rdfs:label "kääntäjä" ]` - and the label was
                    # never read here, so `str(role_obj)` handed the normaliser
                    # rdflib's parse-local node label. The stored role was that
                    # label: unusable as a role, and different on every parse.
                    role_label: Optional[str] = None
                    if isinstance(role_obj, IdentifiedNode):
                        role_labels = _get_literals(
                            g, role_obj, profile.expanded_role_label_predicates
                        )
                        role_label = role_labels[0] if role_labels else None
                    role_text = "" if is_blank_node(role_obj) else str(role_obj)
                    norm = normalize_role_uri_or_label(
                        role_text,
                        label=role_label,
                        profile=profile,
                        relator_mappings=relator_mappings,
                    )
                    if norm:
                        role = norm
                        break

            # Agent entity
            found_agent = False
            for agent_pred in profile.expanded_agent_predicates:
                for agent_obj in g.objects(contrib_obj, URIRef(agent_pred)):
                    if not isinstance(agent_obj, IdentifiedNode):
                        continue
                    agent_uri = str(agent_obj)
                    labels = _get_literals(
                        g,
                        agent_obj,
                        profile.expanded_agent_label_predicates,
                    )
                    agent_name = labels[0] if labels else agent_uri.rstrip("/").split("/")[-1]

                    # A blank-node agent - common in BFFI, where the agent sits
                    # inside the contribution rather than in an authority file -
                    # gets an id derived from its own triples. The guard here used
                    # to test `startswith("_:")`, which `str(BNode())` never
                    # produces, so the agent_uri was a parse-local label: a fresh
                    # agent row for the same person on every ingest.
                    agent_uri_clean = (
                        agent_uri
                        if not is_blank_node(agent_obj)
                        else f"urn:agent:{source}:{derive_bibframe_node_id(g, agent_obj)}"
                    )
                    agent = AgentEntity(
                        agent_uri=agent_uri_clean,
                        source=source,
                        name=agent_name,
                        name_normalized=agent_name.strip().lower(),
                    )
                    agents.append((agent, role, is_primary, marc_key))
                    found_agent = True

            # Literal agent fallback
            if not found_agent:
                simple_lits = _get_literals(
                    g, contrib_obj, profile.expanded_agent_simple_predicates
                )
                for lit_str in simple_lits:
                    raw_name = lit_str.strip()
                    if not raw_name:
                        continue
                    norm_name = raw_name.lower()
                    safe_slug = re.sub(r"[^a-zA-Z0-9_.-]+", "_", norm_name).strip("_")
                    clean_uri = f"urn:reconciliator:agent:{safe_slug or 'unknown'}"
                    agent = AgentEntity(
                        agent_uri=clean_uri,
                        source=source,
                        name=raw_name,
                        name_normalized=norm_name,
                    )
                    agents.append((agent, role, is_primary, marc_key))

    return agents


def extract_bibframe_subjects(
    g: Graph, nodes: list[IdentifiedNode], profile: BIBFRAMESchemaProfile
) -> dict[str, list[dict[str, Any]]]:
    """Extract categorised subjects from a BIBFRAME graph.

    Moved here from `rdflib_loader`, where it was private, so the decomposer can
    use it too: the target path read `bf:subject` and the ingest path did not, so
    a BIBFRAME work reached the datastore with no subjects at all - no terms in
    its serialised text, and an empty subject footprint on each of its agents.
    """
    field_category_map = get_subject_field_category_map()
    cat_default_field = get_category_default_field_map()
    default_target_field = profile.default_subject_field or cat_default_field["topical"]
    default_category = field_category_map[default_target_field]
    subjects: dict[str, list[dict[str, Any]]] = {cat: [] for cat in field_category_map.values()}
    seen_uris: set[str] = set()

    type_to_field = profile.subject_type_fields
    source_preds = profile.expanded_source_predicates

    for node in nodes:
        # Subject predicates
        for subj_pred in profile.expanded_subject_predicates:
            for subj_obj in g.objects(node, URIRef(subj_pred)):
                if not isinstance(subj_obj, IdentifiedNode):
                    continue

                uri: Optional[str] = str(subj_obj) if isinstance(subj_obj, URIRef) else None
                types = {str(t) for t in g.objects(subj_obj, RDF.type)}

                headings = _get_literals(g, subj_obj, profile.expanded_subject_label_predicates)
                heading = headings[0] if headings else (uri.split("/")[-1] if uri else "")
                if not heading:
                    continue

                # Determine target field from configured RDF types
                target_field = default_target_field
                for type_sub, f_tag in type_to_field.items():
                    if any(type_sub in t for t in types):
                        target_field = f_tag
                        break

                # Vocabulary detection from URI or configured source predicates
                vocab: Optional[str] = None
                if uri:
                    detected_v = detect_vocabulary(uri)
                    # The pattern table lists only the modern vocabularies, so a
                    # legacy URI - ysa, allars, musa, kaunokki - is not detected.
                    # It does name itself in its own path, and a BIBFRAME subject
                    # has no MARC $2 to declare it with, so without this a legacy
                    # heading reached the target record with no vocabulary and
                    # nothing to fold.
                    vocab = detected_v.lower() if detected_v else vocabulary_hint_from_uri(uri)

                for src_pred in source_preds:
                    for src_obj in g.objects(subj_obj, URIRef(src_pred)):
                        if isinstance(src_obj, Literal):
                            vocab = str(src_obj).strip()
                        elif isinstance(src_obj, IdentifiedNode):
                            src_labels = _get_literals(
                                g, src_obj, [str(RDFS.label), str(RDF.value), str(SKOS.prefLabel)]
                            )
                            if src_labels:
                                vocab = src_labels[0]

                item_key = (heading, vocab or "", target_field)
                if item_key in seen_uris:
                    continue
                seen_uris.add(item_key)

                item = {
                    "heading": heading,
                    "vocabulary": vocab,
                    "uri": uri,
                    "field": target_field,
                }
                category = field_category_map.get(target_field, default_category)
                subjects[category].append(item)

    # Medium of performance predicates
    mop_field = cat_default_field["medium_of_performance"]
    mop_category = field_category_map[mop_field]
    for node in nodes:
        for mop_pred in profile.expanded_music_performance_predicates:
            for mop_obj in g.objects(node, URIRef(mop_pred)):
                if not isinstance(mop_obj, IdentifiedNode):
                    continue
                uri = str(mop_obj) if isinstance(mop_obj, URIRef) else None
                headings = _get_literals(g, mop_obj, [str(RDFS.label), str(SKOS.prefLabel)])
                heading = headings[0] if headings else (uri.split("/")[-1] if uri else "")
                if not heading:
                    continue
                vocab = None
                if uri:
                    detected_v = detect_vocabulary(uri)
                    vocab = detected_v.lower() if detected_v else profile.default_music_vocabulary
                subjects[mop_category].append(
                    {
                        "heading": heading,
                        "field": mop_field,
                        "vocabulary": vocab,
                        "uri": uri,
                    }
                )

    return subjects


def _extract_identifiers(
    g: Graph, node: IdentifiedNode, profile: BIBFRAMESchemaProfile
) -> list[IdentifierEntity]:
    """Extract standard identifiers from an instance or work node."""
    identifiers: list[IdentifierEntity] = []
    scheme_map = profile.expanded_identifier_scheme_mappings

    for id_pred in profile.expanded_identifier_predicates:
        for id_obj in g.objects(node, URIRef(id_pred)):
            val: Optional[str] = None
            scheme: Optional[str] = None

            if isinstance(id_obj, Literal):
                val = str(id_obj).strip()
                scheme = "other"
            elif isinstance(id_obj, IdentifiedNode):
                vals = _get_literals(
                    g,
                    id_obj,
                    [
                        str(RDF.value),
                        profile.expand_curie("bf:value"),
                        profile.expand_curie("bffi:value"),
                    ],
                )
                val = vals[0] if vals else None

                sources = _get_literals(
                    g,
                    id_obj,
                    profile.expanded_source_predicates | {str(RDFS.label)},
                )
                if sources:
                    s_low = sources[0].lower()
                    for cand_scheme, cands in scheme_map.items():
                        if any(c.lower() in s_low for c in cands):
                            scheme = cand_scheme
                            break

                if not scheme:
                    id_types = {str(t) for t in g.objects(id_obj, RDF.type)}
                    for cand_scheme, cands in scheme_map.items():
                        if id_types.intersection(cands):
                            scheme = cand_scheme
                            break

            if val and scheme:
                norm_val = re.sub(r"[^0-9a-zA-Z]", "", val).lower()
                identifiers.append(
                    IdentifierEntity(
                        entity_type="manifestation",
                        entity_id=0,
                        scheme=scheme,
                        value=val,
                        value_normalized=norm_val,
                    )
                )

    return identifiers


def _extract_mediums_of_performance(
    g: Graph, node: IdentifiedNode, profile: BIBFRAMESchemaProfile
) -> list[MediumOfPerformance]:
    """Extract PMO / BIBFRAME mediums of performance."""
    mediums: list[MediumOfPerformance] = []
    pmo_preds = profile.expanded_music_performance_predicates

    for pred in pmo_preds:
        for mop_obj in g.objects(node, URIRef(pred)):
            mop_uri = str(mop_obj) if isinstance(mop_obj, URIRef) else None
            labels = _get_literals(
                g,
                mop_obj,
                profile.expanded_agent_label_predicates
                | {profile.expand_curie("pmo:mediumOfPerformance")},
            )
            label = labels[0] if labels else (mop_uri.split("/")[-1] if mop_uri else "medium")

            counts = _get_literals(
                g,
                mop_obj,
                [profile.expand_curie("pmo:playerCount"), profile.expand_curie("bf:count")],
            )
            player_count = int(counts[0]) if counts and counts[0].isdigit() else 1

            mediums.append(
                MediumOfPerformance(
                    work_id=0,
                    medium_uri=mop_uri or "",
                    label=label,
                    player_count=player_count,
                    is_soloist=False,
                )
            )

    return mediums


def _extract_items(
    g: Graph,
    instance_node: IdentifiedNode,
    manifestation_id: str,
    source: str,
    profile: BIBFRAMESchemaProfile,
) -> list[ItemEntity]:
    """Extract Item entities linked to an Instance."""
    items: list[ItemEntity] = []

    for pred in profile.expanded_item_predicates:
        for item_obj in g.objects(instance_node, URIRef(pred)):
            if not isinstance(item_obj, IdentifiedNode):
                continue
            item_uri = str(item_obj)
            barcodes = _get_literals(g, item_obj, profile.expanded_item_barcode_predicates)
            shelfmarks = _get_literals(g, item_obj, profile.expanded_item_shelfmark_predicates)
            orgs = _get_uris(g, item_obj, profile.expanded_item_holding_predicates)

            items.append(
                ItemEntity(
                    manifestation_id=0,
                    item_uri=item_uri,
                    source=source,
                    source_id=item_uri,
                    holding_organization=orgs[0] if orgs else None,
                    barcode=barcodes[0] if barcodes else None,
                    shelfmark=shelfmarks[0] if shelfmarks else None,
                )
            )

    return items


#: Part/whole predicates whose meaning depends on what is at the other end, and
#: the relationship type to use when that end is a series.
_SERIES_SENSE: dict[str, str] = {
    "constituent_of": "series_member_of",
    "has_constituent": "has_series_member",
}


def _extract_work_relationships(
    g: Graph,
    work_node: IdentifiedNode,
    work_id: str,
    profile: BIBFRAMESchemaProfile,
    source: str = "bibframe",
) -> list[WorkRelationship]:
    """Extract structural and derivative relationships from a work node.

    Both endpoints are named by URI. They used to be left at id 0 with no URI at
    all, which made every edge unstorable: the target violated the foreign key,
    so a graph stating any of these predicates would have failed its batch. An
    edge whose target is not a work this ingest creates is still dropped by
    `insert_wemi_batch`, with a warning - a relationship to a row we do not have
    cannot be stored - but one pointing at a constituent now binds.
    """
    relationships: list[WorkRelationship] = []
    source_uri = bibframe_work_uri(g, work_node, source)

    for pred_uri, rel_type in profile.expanded_relationship_predicates.items():
        for target_obj in g.objects(work_node, URIRef(pred_uri)):
            if not isinstance(target_obj, IdentifiedNode):
                continue
            resolved = rel_type
            if rel_type in _SERIES_SENSE and _is_series(g, target_obj, profile):
                # `bf:partOf` pointing at a series *is* series membership; the
                # same predicate pointing at a work is containment.
                resolved = _SERIES_SENSE[rel_type]
            relationships.append(
                WorkRelationship(
                    source_work_id=0,
                    target_work_id=0,
                    source_work_uri=source_uri,
                    target_work_uri=bibframe_work_uri(g, target_obj, source),
                    relationship_type=resolved,
                )
            )

    return relationships


def _is_series(g: Graph, node: IdentifiedNode, profile: BIBFRAMESchemaProfile) -> bool:
    types = {str(t) for t in g.objects(node, RDF.type)}
    return bool(types & profile.expanded_series_classes)


def decompose_bibframe_work(
    g: Graph,
    instance_node: Optional[IdentifiedNode] = None,
    profile: Optional[BIBFRAMESchemaProfile] = None,
    source: str = "bibframe",
    serialization_engine: Optional[WEMISerializationEngine] = None,
    relator_mappings: Optional[dict[str, str]] = None,
    supported_subject_vocabularies: Optional[Iterable[str]] = None,
    legacy_to_modern: Optional[dict[str, str]] = None,
    vocab_store: Optional[Any] = None,
    constituent_profile: Optional[Any] = None,
    constituent_sink: Optional[list[Any]] = None,
    subject_sink: Optional[list[EntitySubject]] = None,
) -> tuple[
    WorkEntity,
    ExpressionEntity,
    ManifestationEntity,
    list[ItemEntity],
    list[AgentEntity],
    list[WorkRelationship],
    list[IdentifierEntity],
    list[ManifestationExpressionAssociation],
    list[MediumOfPerformance],
    list[ContributionEntity],
]:
    """
    Algorithmic Work-Expression-Manifestation decomposition evaluating the
    Decision Matrix (C-1 through C-6) on a BIBFRAME / BFFI graph node.
    """
    if profile is None:
        namespaces = {prefix: str(ns) for prefix, ns in g.namespaces()}
        root_types = {str(obj) for obj in g.objects(None, RDF.type)}
        profile = detect_profile(namespaces, root_types)

    engine = serialization_engine or WEMISerializationEngine()

    # Discover instance if not supplied
    if instance_node is None:
        for inst_cls in profile.expanded_instance_classes:
            for s in g.subjects(RDF.type, URIRef(inst_cls)):
                if isinstance(s, IdentifiedNode):
                    instance_node = s
                    break
            if instance_node:
                break

    if instance_node is None:
        # Fallback to Work or Expression subject
        for s in g.subjects(RDF.type, None):
            if isinstance(s, IdentifiedNode):
                instance_node = s
                break

    if instance_node is None:
        raise ValueError("Could not find any instance or work node in the graph to decompose.")

    # -------------------------------------------------------------------------
    # Decision Matrix Evaluation: Determine Topologies & Conditions
    # -------------------------------------------------------------------------
    work_node: Optional[IdentifiedNode] = None
    expression_node: Optional[IdentifiedNode] = None
    hub_node: Optional[IdentifiedNode] = None
    translation_orig_node: Optional[IdentifiedNode] = None
    pmo_perf_node: Optional[IdentifiedNode] = None

    # Check Condition C-5: PMO (Performance -> performedWork / recordedIn)
    for pmo_cls in profile.expanded_pmo_performance_classes:
        for s in g.subjects(RDF.type, URIRef(pmo_cls)):
            if isinstance(s, IdentifiedNode):
                pmo_perf_node = s
                for w_pred in profile.expanded_pmo_performed_work_predicates:
                    for w in g.objects(s, URIRef(w_pred)):
                        if isinstance(w, IdentifiedNode):
                            work_node = w
                            break
                    if work_node:
                        break
                break
        if pmo_perf_node:
            break

    # Resolve expression and work links from instance
    if not pmo_perf_node:
        # Check BFFI expressionManifested (C-1 / C-2)
        for pred in profile.expanded_instance_to_expression_predicates:
            for obj in g.objects(instance_node, URIRef(pred)):
                if isinstance(obj, IdentifiedNode):
                    expression_node = obj
                    break
            if expression_node:
                break

        if expression_node:
            node_types = {str(t) for t in g.objects(expression_node, RDF.type)}
            if any(t in profile.expanded_work_classes for t in node_types) or any(
                "Work" in t for t in node_types
            ):
                work_node = expression_node
                expression_node = None
            else:
                for pred in profile.expanded_expression_to_work_predicates:
                    for obj in g.objects(expression_node, URIRef(pred)):
                        if isinstance(obj, IdentifiedNode):
                            work_node = obj
                            break
                    if work_node:
                        break

        # Check instance -> work directly (C-2 shortcut or LC C-3/C-4/C-6)
        shortcut_work_node: Optional[IdentifiedNode] = None
        for pred in profile.expanded_instance_to_work_predicates:
            for obj in g.objects(instance_node, URIRef(pred)):
                if isinstance(obj, IdentifiedNode):
                    shortcut_work_node = obj
                    break
            if shortcut_work_node:
                break

        if not work_node:
            work_node = shortcut_work_node
        elif shortcut_work_node and str(shortcut_work_node) != str(work_node):
            logger.warning(
                "Dual-path work discrepancy detected for instance %s: "
                "expression-mediated Work (%s) differs from shortcut Work (%s). "
                "Adhering to canonical expression-mediated Work.",
                str(instance_node),
                str(work_node),
                str(shortcut_work_node),
            )

    # If work_node discovered, check for bflc:hasHub (C-3) or bf:translationOf (C-4)
    hub_pred = profile.expand_curie("bflc:hasHub")
    translation_pred = profile.expand_curie("bf:translationOf")
    if work_node:
        for h in g.objects(work_node, URIRef(hub_pred)):
            if isinstance(h, IdentifiedNode):
                hub_node = h
                break
        for orig in g.objects(work_node, URIRef(translation_pred)):
            if isinstance(orig, IdentifiedNode):
                translation_orig_node = orig
                break

    # -------------------------------------------------------------------------
    # Apply Decomposition Actions
    # -------------------------------------------------------------------------
    condition = "C-6"
    if pmo_perf_node:
        condition = "C-5"
    elif hub_node:
        condition = "C-3"
    elif translation_orig_node:
        condition = "C-4"
    elif expression_node and work_node:
        condition = "C-1"
    elif profile.synthesize_missing_expression and not expression_node and work_node:
        condition = "C-2"

    logger.debug("Decomposing BIBFRAME node %s under condition %s", instance_node, condition)

    # 1. Manifestation Entity Extraction
    manif_uri = None if is_blank_node(instance_node) else str(instance_node)
    manif_id = derive_bibframe_node_id(g, instance_node)
    manif_title, manif_sub, _, manif_variants = _extract_title_elements(g, instance_node, profile)

    # Provision activity
    pub_year_int: Optional[int] = None
    publisher_str: Optional[str] = None
    pub_place_str: Optional[str] = None
    edition_str: Optional[str] = None
    extent_str: Optional[str] = None

    for prov_pred in profile.expanded_provision_activity_predicates:
        for prov_obj in g.objects(instance_node, URIRef(prov_pred)):
            if not isinstance(prov_obj, IdentifiedNode):
                continue
            dates = _get_literals(g, prov_obj, profile.expanded_provision_date_predicates)
            if dates and not pub_year_int:
                m = re.search(r"(\d{4})", dates[0])
                if m:
                    pub_year_int = int(m.group(1))

            agents_prov = _get_literals(
                g,
                prov_obj,
                profile.expanded_provision_publisher_predicates | profile.expanded_agent_predicates,
            )
            if agents_prov and not publisher_str:
                publisher_str = agents_prov[0]

            places = _get_literals(g, prov_obj, profile.expanded_provision_place_predicates)
            if places and not pub_place_str:
                pub_place_str = places[0]

    ed_lits = _get_literals(g, instance_node, profile.expanded_provision_edition_predicates)
    if ed_lits:
        edition_str = ed_lits[0]

    ext_lits = _get_literals(g, instance_node, profile.expanded_provision_extent_predicates)
    if ext_lits:
        extent_str = ext_lits[0]

    # Carrier Type
    carrier: CarrierType = "volume"
    carrier_preds = [profile.expand_curie("bf:carrier"), profile.expand_curie("bffi:carrier")]
    carrier_uris = _get_uris(g, instance_node, carrier_preds)
    for c_uri in carrier_uris:
        c_low = c_uri.lower()
        for cand_type, tokens in profile.carrier_type_mappings.items():
            if any(tok.lower() in c_low for tok in tokens):
                carrier = cand_type  # type: ignore[assignment]
                break

    # 2. Work & Expression Entity Extraction by Condition
    work_id: str
    work_uri: Optional[str] = None
    work_title = ""
    work_type: WorkType = "text"
    work_lang: Optional[str] = None

    expr_id: str
    expr_uri: Optional[str] = None
    expr_title = ""
    expr_content_type: ExpressionContentType = "text"
    expr_lang: Optional[str] = None

    work_relationships: list[WorkRelationship] = []
    mediums_of_perf: list[MediumOfPerformance] = []

    if condition == "C-3":
        # Hub Hoisting: hub_node is canonical Work centroid
        assert hub_node is not None
        work_id = derive_bibframe_node_id(g, hub_node)
        work_uri = None if is_blank_node(hub_node) else str(hub_node)
        work_title, _, _, _ = _extract_title_elements(g, hub_node, profile)
        if not work_title and work_node:
            work_title, _, _, _ = _extract_title_elements(g, work_node, profile)
        work_type = "hub"

        # The work_node acts as an Expression realizing the Hub
        assert work_node is not None
        expr_id = derive_bibframe_node_id(g, work_node)
        expr_uri = None if is_blank_node(work_node) else str(work_node)
        expr_title, _, _, _ = _extract_title_elements(g, work_node, profile)
        expr_lang = _extract_language(g, work_node, profile)

    elif condition == "C-4":
        # Translation Expression Decomposition
        assert translation_orig_node is not None
        work_id = derive_bibframe_node_id(g, translation_orig_node)
        work_uri = None if is_blank_node(translation_orig_node) else str(translation_orig_node)
        work_title, _, _, _ = _extract_title_elements(g, translation_orig_node, profile)
        if not work_title and work_node:
            work_title, _, _, _ = _extract_title_elements(g, work_node, profile)
        work_lang = _extract_language(g, translation_orig_node, profile)

        assert work_node is not None
        expr_id = derive_bibframe_node_id(g, work_node)
        expr_uri = None if is_blank_node(work_node) else str(work_node)
        expr_title, _, _, _ = _extract_title_elements(g, work_node, profile)
        expr_lang = _extract_language(g, work_node, profile)

        # Named, like every other edge: the translation *is* the work node and
        # the original is the node it was reached through. Without the URIs this
        # edge could never bind, and it also read as a second, different fact
        # from the one the predicate map derives for the same pair.
        work_relationships.append(
            WorkRelationship(
                source_work_id=0,
                target_work_id=0,
                source_work_uri=bibframe_work_uri(g, work_node, source),
                target_work_uri=bibframe_work_uri(g, translation_orig_node, source),
                relationship_type="translation_of",
            )
        )

    elif condition == "C-5":
        # Music Performance Realization
        assert work_node is not None
        assert pmo_perf_node is not None
        work_id = derive_bibframe_node_id(g, work_node)
        work_uri = None if is_blank_node(work_node) else str(work_node)
        work_title, _, _, _ = _extract_title_elements(g, work_node, profile)
        work_type = "music"
        mediums_of_perf.extend(_extract_mediums_of_performance(g, work_node, profile=profile))

        expr_id = derive_bibframe_node_id(g, pmo_perf_node)
        expr_uri = None if is_blank_node(pmo_perf_node) else str(pmo_perf_node)
        expr_title, _, _, _ = _extract_title_elements(g, pmo_perf_node, profile)
        if not expr_title:
            expr_title = work_title
        expr_content_type = "audio_music"
        expr_lang = _extract_language(g, pmo_perf_node, profile)

    elif condition == "C-1":
        # Direct 1:1 Extraction (BFFI)
        assert work_node is not None
        assert expression_node is not None
        work_id = derive_bibframe_node_id(g, work_node)
        work_uri = None if is_blank_node(work_node) else str(work_node)
        work_title, _, _, _ = _extract_title_elements(g, work_node, profile)
        work_lang = _extract_language(g, work_node, profile)

        expr_id = derive_bibframe_node_id(g, expression_node)
        expr_uri = None if is_blank_node(expression_node) else str(expression_node)
        expr_title, _, _, _ = _extract_title_elements(g, expression_node, profile)
        expr_lang = _extract_language(g, expression_node, profile)

    elif condition == "C-2":
        # Synthesize Default Expression (BFFI)
        assert work_node is not None
        work_id = derive_bibframe_node_id(g, work_node)
        work_uri = None if is_blank_node(work_node) else str(work_node)
        work_title, _, _, _ = _extract_title_elements(g, work_node, profile)
        work_lang = _extract_language(g, work_node, profile)

        expr_id = f"{work_id}#expr-orig"
        expr_uri = f"{work_uri}#expr-orig" if work_uri else None
        expr_title = work_title
        expr_lang = work_lang

    else:
        # C-6: Single-Work Partitioning (LC BIBFRAME 2.0)
        target_w = work_node if work_node else instance_node
        work_id = derive_bibframe_node_id(g, target_w)
        work_uri = None if is_blank_node(target_w) else str(target_w)
        work_title, _, _, _ = _extract_title_elements(g, target_w, profile)
        work_lang = _extract_language(g, target_w, profile)

        expr_id = f"{work_id}#expr-1"
        expr_uri = f"{work_uri}#expr-1" if work_uri else None
        expr_title = work_title
        expr_lang = work_lang

    if not work_title:
        work_title = manif_title or "Untitled Work"
    if not expr_title:
        expr_title = manif_title or work_title

    # Extract Agents & Contributions partitioned across Work and Expression
    all_agents_map: dict[str, AgentEntity] = {}
    contributions: list[ContributionEntity] = []

    # Nodes to scan for contributors
    nodes_to_scan = [instance_node]
    if work_node:
        nodes_to_scan.append(work_node)
    if expression_node:
        nodes_to_scan.append(expression_node)
    if hub_node:
        nodes_to_scan.append(hub_node)
    if pmo_perf_node:
        nodes_to_scan.append(pmo_perf_node)

    extracted_agents_with_role: list[tuple[AgentEntity, str]] = []
    for n in nodes_to_scan:
        extracted = _extract_node_agents(
            g, n, profile, source=source, relator_mappings=relator_mappings
        )
        for agent_ent, role, is_primary, marc_key in extracted:
            existing = all_agents_map.get(agent_ent.agent_uri)
            if existing is not None:
                agent_ent = existing
            all_agents_map[agent_ent.agent_uri] = agent_ent
            # The MARC path stores a role histogram per agent and the enricher
            # reads it (`next(iter(agent.role_counts))` for the export role), so
            # leaving it empty here made every BibFrame agent roleless in the
            # output while the same agent from Melinda carried {"aut": 1}.
            agent_ent.role_counts[role] = agent_ent.role_counts.get(role, 0) + 1
            extracted_agents_with_role.append((agent_ent, role))
            # Determine target entity partition: Work vs Expression
            target_ent = (
                "work" if (role in profile.work_creator_roles or is_primary) else "expression"
            )
            field_tag: Optional[str] = None
            if marc_key:
                parsed_mk = extract_marc_key_components(marc_key, profile=profile)
                if parsed_mk:
                    field_tag = parsed_mk[0]
            contributions.append(
                ContributionEntity(
                    entity_type=target_ent,
                    entity_id=0,
                    agent_id=0,
                    role=role,
                    is_primary=is_primary,
                    field_tag=field_tag,
                )
            )

    # Identifiers
    identifiers = _extract_identifiers(g, instance_node, profile)

    # Inter-work relationships
    if work_node and constituent_profile is not None and constituent_sink is not None:
        # An out-parameter, like `key_sink` on the MARC side and for the same
        # reason: the node the parts hang off is resolved by seventy lines of
        # decision matrix here, and the caller has no way to find it again.
        constituent_sink.extend(
            extract_bibframe_constituents(
                g,
                work_node,
                constituent_profile,
                profile,
                source=source,
                record_id=f"{source}/{work_id}",
            )
        )

    if work_node:
        work_relationships.extend(
            _extract_work_relationships(g, work_node, work_id, profile=profile, source=source)
        )
    # By URI, because the ids are all 0 until `insert_wemi_batch` binds them.
    # Keyed on the ids alone - as this was - two edges to *different* works
    # collapsed into one, so a graph with two `bf:hasPart` objects kept a single
    # part. That was invisible while these edges named no target at all.
    seen_rels = set()
    deduped_rels = []
    for r in work_relationships:
        key = (
            r.relationship_type,
            r.source_work_uri or r.source_work_id,
            r.target_work_uri or r.target_work_id,
        )
        if key not in seen_rels:
            seen_rels.add(key)
            deduped_rels.append(r)
    work_relationships = deduped_rels

    # Mediums of performance
    if work_node:
        mediums_of_perf.extend(_extract_mediums_of_performance(g, work_node, profile=profile))
    if expression_node:
        mediums_of_perf.extend(_extract_mediums_of_performance(g, expression_node, profile=profile))
    seen_mops = set()
    deduped_mops = []
    for m in mediums_of_perf:
        key = (m.label, m.medium_uri)
        if key not in seen_mops:
            seen_mops.add(key)
            deduped_mops.append(m)
    mediums_of_perf = deduped_mops
    expr_mop_str = ", ".join(m.label for m in mediums_of_perf if m.label) or None

    # Items
    items = _extract_items(g, instance_node, manif_id, source=source, profile=profile)

    # Manifestation - Expression Junction
    manifestation_expressions = [
        ManifestationExpressionAssociation(
            manifestation_id=0,
            expression_id=0,
            is_primary=True,
        )
    ]

    # Primary creator for serialization
    primary_creator_name: Optional[str] = None
    for c, ag_ent in zip(contributions, all_agents_map.values()):
        if c.is_primary:
            primary_creator_name = ag_ent.name
            break
    if not primary_creator_name and all_agents_map:
        primary_creator_name = next(iter(all_agents_map.values())).name

    orig_title: Optional[str] = None
    if translation_orig_node:
        t_title, _, _, _ = _extract_title_elements(g, translation_orig_node, profile)
        if t_title and t_title != work_title:
            orig_title = t_title

    # Classify from the graph's own types. The condition branches above only set
    # work_type for two cases (C-3 hub, C-5 music) and default to "text", so
    # without this a BibFrame cartographic resource, thesis or dataset all
    # arrived as text and reached the monograph funnel. A condition that already
    # decided is left alone; everything else is looked up.
    if work_type == "text":
        work_type = determine_bibframe_work_type(  # type: ignore[assignment]
            g, work_node, instance_node, hub_node
        )
    if expr_content_type == "text":
        expr_content_type = determine_bibframe_content_type(  # type: ignore[assignment]
            g, work_node, instance_node, hub_node, default=expr_content_type
        )

    # Subjects, through the same folder the MARC path uses. Nothing read
    # `bf:subject` here at all, so a BIBFRAME work was stored with no subjects:
    # no subject terms in the serialised text that BM25 and the dense index
    # read, and an empty subject footprint on every agent - while the same
    # record harvested as MARC carried both.
    raw_subjects = [
        subject
        for group in extract_bibframe_subjects(g, nodes_to_scan, profile).values()
        for subject in group
    ]
    folded_subjects = fold_subjects(
        raw_subjects,
        supported_vocabularies=supported_subject_vocabularies,
        legacy_to_modern=legacy_to_modern,
        vocab_store=vocab_store,
        language=work_lang,
    )
    record_subject_counts = subject_footprint(folded_subjects)
    for agent_ent in all_agents_map.values():
        agent_ent.subject_counts = dict(record_subject_counts)

    # Build WEMI entities with serialization
    authors_list = [{"name": primary_creator_name}] if primary_creator_name else []
    work = WorkEntity(
        work_uri=work_uri or f"urn:work:{source}:{work_id}",
        title=work_title,
        title_normalized=compute_title_stem(work_title, lang=work_lang),
        original_title=orig_title,
        original_language=work_lang,
        work_type=work_type,
        source=source,
        source_id=work_id,
        subjects_json=[subject.as_dict() for subject in folded_subjects],
    )
    work.serialized_text = engine.render_work(work, authors=authors_list)

    if subject_sink is not None:
        for s in folded_subjects:
            field = str(s.field or "")
            relation_type = "topic"
            if field == "651":
                relation_type = "geographic"
            elif field == "655":
                relation_type = "genre_form"
            elif field == "648":
                relation_type = "temporal"
            subject_sink.append(
                EntitySubject(
                    entity_type="work",
                    entity_id=0,
                    entity_uri=work.work_uri,
                    source=source,
                    source_record_id=str(work_id),
                    relation_type=relation_type,
                    vocabulary=s.vocabulary,
                    concept_id=s.concept_id,
                    raw_label=s.heading,
                    is_resolved=bool(s.vocabulary and s.concept_id),
                    is_primary=False,
                )
            )

    translators_list = [{"name": ag.name} for ag, r in extracted_agents_with_role if r == "trl"]
    narrators_list = [
        {"name": ag.name}
        for ag, r in extracted_agents_with_role
        if r in ("nrt", "lbr", "narrator", "lukija")
    ]

    expression = ExpressionEntity(
        work_id=0,
        work_uri=work.work_uri,
        expression_uri=expr_uri or f"urn:exp:{source}:{expr_id}",
        title=expr_title,
        content_type=expr_content_type,
        language=expr_lang or "und",
        medium_of_performance=expr_mop_str,
        source=source,
        source_id=expr_id,
    )
    expression.serialized_text = engine.render_expression(
        expression,
        work_title=work_title,
        translators=translators_list,
        narrators=narrators_list,
    )

    manifestation = ManifestationEntity(
        work_uri=work.work_uri,
        work_id=0,
        manifestation_uri=manif_uri or f"urn:manif:{source}:{manif_id}",
        title=manif_title,
        title_normalized=compute_title_stem(manif_title, lang=expr_lang),
        carrier_type=carrier,
        edition_statement=edition_str,
        publication_year=pub_year_int,
        publisher=publisher_str,
        publication_place=pub_place_str,
        extent=extent_str,
        source=source,
        source_id=manif_id,
    )
    manifestation.serialized_text = engine.render_manifestation(manifestation)

    return (
        work,
        expression,
        manifestation,
        items,
        list(all_agents_map.values()),
        work_relationships,
        identifiers,
        manifestation_expressions,
        mediums_of_perf,
        contributions,
    )


def _identifiers_as_bag(identifiers: list[IdentifierEntity]) -> dict[str, list[str]]:
    """Shape IdentifierEntity rows as the scheme -> values bag MatchRecord carries."""
    bag: dict[str, list[str]] = {}
    for ident in identifiers:
        bucket = bag.setdefault(ident.scheme.lower().strip(), [])
        if ident.value not in bucket:
            bucket.append(ident.value)
    return bag


# BIBFRAME Work subclasses and bf:content values, mapped to WorkType. Unlike the
# MARC side - where classification comes from leader/007/008 positions and Finnish
# genre headings - the BIBFRAME vocabulary names these types explicitly, so the
# mapping is a lookup rather than a heuristic.
#
# Without it every BibFrame record defaulted to "text" (or "hub"/"music" under two
# graph conditions), so a BibFrame cartographic resource, thesis or dataset reached
# the monograph funnel: only 5 of 42 funnels were reachable from a BibFrame record.
BIBFRAME_WORK_TYPES: dict[str, str] = {
    "Cartography": "cartography",
    "Dataset": "dataset",
    "MovingImage": "moving_image",
    "StillImage": "still_image",
    "NotatedMusic": "music",
    "NotatedMovement": "choreography",
    "Audio": "music",
    "Object": "realia",
    "Multimedia": "multimedia",
    "Manuscript": "manuscript",
    "Archival": "archive_fonds",
    "Serial": "serial",
    "Integrating": "integrating",
    "Print": "text",
    "Text": "text",
}

# bf:content / rdaco values, checked after the class, since a Work may carry a
# generic class with a specific content type.
BIBFRAME_CONTENT_WORK_TYPES: dict[str, str] = {
    "cartographic": "cartography",
    "performed": "music",
    "notated-music": "music",
    "notated music": "music",
    "computer-dataset": "dataset",
    "computer-program": "software",
    "two-dimensional-moving-image": "moving_image",
    "three-dimensional-moving-image": "moving_image",
    "still-image": "still_image",
    "three-dimensional-form": "realia",
    "spoken-word": "audio_nonmusic",
}


_BIBFRAME = "http://id.loc.gov/ontologies/bibframe/"
_BF_CLASSIFYING_PREDICATES = (
    URIRef(_BIBFRAME + "content"),
    URIRef(_BIBFRAME + "genreForm"),
)


# The expression content type implied by a BIBFRAME class. Needed because the
# funnel split between notated music and recorded sound keys on content_type -
# both are work_type "music" - so without this every BibFrame music record, score
# or recording, reached the recorded-sound funnel.
BIBFRAME_CONTENT_TYPES: dict[str, str] = {
    "NotatedMusic": "notated_music",
    "Audio": "audio_music",
    "NotatedMovement": "choreographic_movement",
    "Cartography": "cartographic_image",
    "StillImage": "still_image",
    "Dataset": "dataset",
    "MovingImage": "moving_image",
    "Object": "three_dimensional",
}


def determine_bibframe_content_type(
    g: Graph,
    *nodes: Optional[IdentifiedNode],
    default: Optional[str] = None,
) -> Optional[str]:
    """The content type a BIBFRAME class implies, or `default` when unrecognised."""
    for node in nodes:
        if node is None:
            continue
        for type_uri in g.objects(node, RDF.type):
            local = str(type_uri).rstrip("/").split("/")[-1].split("#")[-1]
            if local in BIBFRAME_CONTENT_TYPES:
                return BIBFRAME_CONTENT_TYPES[local]
    return default


def determine_bibframe_work_type(
    g: Graph,
    *nodes: Optional[IdentifiedNode],
    default: str = "text",
) -> str:
    """Classify a BIBFRAME work from its rdf:type and bf:content.

    Checks each given node in order - typically the Work then the Instance - and
    returns the first recognised classification. Local-name matching, because
    profiles differ on whether they emit `bf:` or the expanded LoC namespace.
    """
    for node in nodes:
        if node is None:
            continue
        for type_uri in g.objects(node, RDF.type):
            local = str(type_uri).rstrip("/").split("/")[-1].split("#")[-1]
            if local in BIBFRAME_WORK_TYPES:
                return BIBFRAME_WORK_TYPES[local]

    for node in nodes:
        if node is None:
            continue
        for pred in _BF_CLASSIFYING_PREDICATES:
            for obj in g.objects(node, pred):
                text = str(obj).rstrip("/").split("/")[-1].split("#")[-1].lower()
                for needle, work_type in BIBFRAME_CONTENT_WORK_TYPES.items():
                    if needle in text:
                        return work_type

    return default


def is_blank_node(node: Any) -> bool:
    """Whether an RDF term is a blank node.

    `str(BNode())` returns a *bare* label - "Nc5b5385e..." - with no "_:"
    prefix; only `.n3()` adds one. Every `startswith("_:")` guard in this module
    was therefore dead code that never fired, which is how parse-local labels
    came to be used as work, expression and manifestation URIs.
    """
    return isinstance(node, BNode)


def derive_bibframe_node_id(g: Graph, node: Optional[IdentifiedNode]) -> str:
    """A stable identifier for a BIBFRAME node.

    A node with a real URI is identified by it. A blank node is not: rdflib mints
    a fresh label on every parse, so identity taken from the label changes each
    time the same file is ingested - new work, expression and manifestation URIs,
    no upsert match, duplicate rows, and an `ingest_mode: replace` that cannot
    help because the ids differ between runs.

    This mirrors `derive_content_id` on the MARC side: identity from content, not
    from position or parse order. The digest covers the node's own
    predicate/object pairs, with a blank-node object replaced by a digest of
    *its* pairs - one level of recursion, which is what BIBFRAME titles need,
    since `bf:title [ a bf:Title ; bf:mainTitle "X" ]` puts the title itself
    behind a blank node.
    """
    if node is None:
        return ""
    if not is_blank_node(node):
        return str(node)

    def pairs(subject: Any, depth: int) -> list[str]:
        out: list[str] = []
        for predicate, obj in sorted(g.predicate_objects(subject), key=lambda po: str(po[0])):
            obj_text = str(obj)
            if is_blank_node(obj):
                if depth <= 0:
                    continue
                inner = hashlib.sha1("\n".join(pairs(obj, depth - 1)).encode("utf-8")).hexdigest()[
                    :16
                ]
                obj_text = f"_:{inner}"
            out.append(f"{predicate}\t{obj_text}")
        return out

    digest = hashlib.sha1("\n".join(pairs(node, 1)).encode("utf-8")).hexdigest()[:16]
    return f"sha1:{digest}"


def extract_bibframe_constituents(
    g: Graph,
    work_node: Optional[IdentifiedNode],
    constituent_profile: Any,
    profile: BIBFRAMESchemaProfile,
    *,
    source: str = "bibframe",
    record_id: str = "<unknown>",
) -> list[Any]:
    """Read the parts a source's constituent profile points at.

    The constituent profile says *where* the parts are - `predicate: bf:hasPart`
    - and the schema profile says how this dialect writes a title and an agent,
    because that is what it is for. Neither duplicates the other.

    A part keeps its own node URI as its identity when it has one: the graph
    stating it is better evidence than a digest of its title. A blank-node part
    falls back to the content digest, like MARC.
    """
    from reconciliator.constituents.record import Constituent
    from reconciliator.constituents.record import finalise as finalise_constituents

    if work_node is None or constituent_profile is None:
        return []

    found: list[Constituent] = []
    for precedence, field in enumerate(getattr(constituent_profile, "fields", [])):
        if not field.predicate:
            continue  # a MARC entry; not this format's business
        for pred_uri in _expand_both(profile, field.predicate):
            for part in g.objects(work_node, URIRef(pred_uri)):
                if not isinstance(part, IdentifiedNode):
                    continue
                title = _part_title(g, part, field, profile)
                if not title:
                    continue
                found.append(
                    Constituent(
                        title=title,
                        creator=_part_creator(g, part, profile),
                        shape=constituent_profile.shape_of(field),
                        component_type=field.component_type,
                        contributes_to_container=field.contributes_to_container,
                        source_field=field.predicate,
                        precedence=precedence,
                        identity_ref=(
                            None if is_blank_node(part) else derive_bibframe_node_id(g, part)
                        ),
                    )
                )

    return finalise_constituents(found, constituent_profile, record_id=record_id)


def _expand_both(profile: BIBFRAMESchemaProfile, curie: str) -> set[str]:
    """A predicate as written and as expanded, since profiles use both forms."""
    return {curie, profile.expand_curie(curie)}


def _part_title(
    g: Graph, part: IdentifiedNode, field: Any, profile: BIBFRAMESchemaProfile
) -> Optional[str]:
    """A part's title, through the field's predicate and then the dialect's.

    `bf:title` points at a Title *node* whose string sits behind `bf:mainTitle`,
    so reading the predicate alone yields a node rather than a title.
    """
    for pred_uri in _expand_both(profile, field.title or "bf:title"):
        for obj in g.objects(part, URIRef(pred_uri)):
            if isinstance(obj, Literal):
                text = str(obj).strip()
                if text:
                    return text
            elif isinstance(obj, IdentifiedNode):
                literals = _get_literals(
                    g,
                    obj,
                    profile.expanded_main_title_predicates | {str(RDFS.label), str(RDF.value)},
                )
                if literals:
                    return literals[0]
    # Then the dialect's own title machinery, which handles the shapes a profile
    # does not spell out.
    title, _subtitle, _uniform, _variants = _extract_title_elements(g, part, profile)
    return title or None


def _part_creator(g: Graph, part: IdentifiedNode, profile: BIBFRAMESchemaProfile) -> Optional[str]:
    """The first agent named on a part, by the dialect's own rules."""
    for agent, _role, _is_primary, _marc_key in _extract_node_agents(g, part, profile):
        if agent.name:
            return agent.name
    return None


def bibframe_work_uri(g: Graph, node: Optional[IdentifiedNode], source: str) -> str:
    """The work URI (CURIE) a node will be stored under.

    Mirrors how `decompose_bibframe_work` builds it: every work is stored under
    the canonical CURIE `source:node_id`.
    """
    if node is None:
        return ""
    node_id = derive_bibframe_node_id(g, node)
    return f"{source}:{node_id}"


def derive_bibframe_keys(
    work: WorkEntity,
    agents: list[AgentEntity],
    identifiers: list[IdentifierEntity],
    author_key: Optional[str] = None,
    title_stem: Optional[str] = None,
) -> dict[str, list[str]]:
    """Derive domain blocking keys for a BIBFRAME record.

    Unlike the MARC side these derive from the decomposed entities rather than
    the source graph, so no second parse is needed and no sink parameter either.
    """
    if author_key is None:
        author_key = compute_author_key(agents[0].name if agents else None)
    if title_stem is None:
        title_stem = compute_title_stem(work.title, lang=work.original_language)

    keys: dict[str, list[str]] = {}

    def _add(key_type: str, val: Optional[str]) -> None:
        if val and str(val).strip():
            keys.setdefault(key_type, [])
            clean = str(val).strip()
            if clean not in keys[key_type]:
                keys[key_type].append(clean)

    _add("author", author_key)
    _add("title_stem", title_stem)
    _add("work", compute_work_blocking_key(author_key, title_stem))
    if work.work_type == "music":
        _add("music_work", compute_music_work_key(author_key, work.title))

    first_isbn = next((i.value for i in identifiers if i.scheme == "isbn"), None)
    first_issn = next((i.value for i in identifiers if i.scheme == "issn"), None)
    _add("standard", first_isbn or first_issn)

    # Translators, computed exactly as on the MARC side. This was declared
    # impossible here because roles were not normalised, so no BibFrame agent
    # ever carried "trl"; now that they do, the key that funnel HH dispatches on
    # and the clusterer scores with is derivable from BIBFRAME too.
    translator_names = [a.name for a in agents if "trl" in (a.role_counts or {})]
    if translator_names:
        _add(
            "translators_signature",
            " ".join(sorted(compute_author_key(n, None) for n in translator_names if n)).strip(),
        )

    return keys


def bibframe_entity_keys(
    manifestation: ManifestationEntity,
    work: WorkEntity,
    agents: list[AgentEntity],
    identifiers: list[IdentifierEntity],
) -> list[EntityKey]:
    """Shape `derive_bibframe_keys` output as EntityKey rows bound to the manifestation."""
    return to_manifestation_entity_keys(
        manifestation.manifestation_uri,
        derive_bibframe_keys(work, agents, identifiers),
    )


def build_target_record_from_bibframe(
    g: Graph,
    instance_node: Optional[IdentifiedNode] = None,
    profile: Optional[BIBFRAMESchemaProfile] = None,
    source: str = "bibframe",
    serialization_engine: Optional[WEMISerializationEngine] = None,
) -> MatchRecord:
    """Decompose a BIBFRAME graph instance and construct a MatchRecord for staging."""
    (
        work,
        expression,
        manifestation,
        items,
        agents,
        relationships,
        identifiers,
        associations,
        mediums,
        contributions,
    ) = decompose_bibframe_work(
        g=g,
        instance_node=instance_node,
        profile=profile,
        source=source,
        serialization_engine=serialization_engine,
    )

    # Primary author for keys
    author_name = agents[0].name if agents else None
    author_key = compute_author_key(author_name)
    title_stem = compute_title_stem(work.title, lang=work.original_language)

    # One shared derivation, so the staged record and the persisted entity_keys
    # rows cannot drift apart.
    keys = derive_bibframe_keys(work, agents, identifiers, author_key, title_stem)

    # Serialize raw RDF representation for lossless payload staging
    raw_payload = g.serialize(format="turtle").encode("utf-8")

    return MatchRecord(
        record_id=manifestation.source_id,
        format="bibframe",
        work_type=work.work_type,
        title=manifestation.title,
        uniform_title=work.title,
        language=expression.language,
        author_key=author_key,
        title_stem=title_stem,
        keys=keys,
        identifiers=_identifiers_as_bag(identifiers),
        content_type=expression.content_type,
        work_uri=work.work_uri,
        expression_uri=expression.expression_uri,
        manifestation_uri=manifestation.manifestation_uri,
        year=manifestation.publication_year,
        volume_designation=None,
        is_component=False,
        serialized_work=work.serialized_text,
        serialized_expression=expression.serialized_text,
        serialized_manifestation=manifestation.serialized_text,
        raw_payload=raw_payload,
    )
