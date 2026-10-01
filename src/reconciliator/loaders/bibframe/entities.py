"""BIBFRAME sub-entity extractors: titles, agents, subjects, identifiers, items, and relations."""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any, Optional

from rdflib import RDF, RDFS, SKOS, Graph, IdentifiedNode, Literal, URIRef

from reconciliator.config import (
    get_category_default_field_map,
    get_subject_field_category_map,
)
from reconciliator.loaders.bibframe.base import (
    normalize_role_uri_or_label,
)
from reconciliator.loaders.bibframe.classification import _is_series
from reconciliator.loaders.bibframe.graph_utils import (
    _get_literals,
    _get_uris,
    bibframe_work_uri,
    derive_bibframe_node_id,
    extract_marc_key_components,
    is_blank_node,
)
from reconciliator.models import (
    AgentEntity,
    IdentifierEntity,
    ItemEntity,
    WorkRelationship,
)
from reconciliator.vocab import (
    detect_vocabulary,
    vocabulary_hint_from_uri,
)

if TYPE_CHECKING:
    from reconciliator.loaders.bibframe.profiles import BIBFRAMESchemaProfile

logger = logging.getLogger(__name__)

_SERIES_SENSE: dict[str, str] = {
    "constituent_of": "series_member_of",
    "has_constituent": "has_series_member",
}

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
) -> list[dict[str, Any]]:
    """Extract PMO / BIBFRAME mediums of performance."""
    mediums: list[dict[str, Any]] = []
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
                {
                    "uri": mop_uri,
                    "label": label,
                    "player_count": player_count,
                }
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


