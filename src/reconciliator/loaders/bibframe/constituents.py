"""Constituent parts extraction from BIBFRAME / BFFI graphs."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, Optional

from rdflib import RDF, RDFS, Graph, IdentifiedNode, Literal, URIRef

from reconciliator.constituents.record import Constituent
from reconciliator.constituents.record import finalise as finalise_constituents
from reconciliator.loaders.bibframe.entities import (
    _extract_node_agents,
    _extract_title_elements,
)
from reconciliator.loaders.bibframe.graph_utils import (
    _get_literals,
    derive_bibframe_node_id,
    extract_marc_key_components,
    is_blank_node,
)

if TYPE_CHECKING:
    from reconciliator.loaders.bibframe.profiles import BIBFRAMESchemaProfile

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
                creator = _part_creator(g, part, profile)

                prov_pred = getattr(field, "provenance_predicate", None)
                if prov_pred and (not title or not creator):
                    for p_uri in _expand_both(profile, prov_pred):
                        mk_lits = _get_literals(g, part, [p_uri])
                        if mk_lits:
                            parsed = extract_marc_key_components(mk_lits[0], profile=profile)
                            if parsed:
                                _tag, _inds, sf_str = parsed
                                sf_map = dict(re.findall(r"[\$\x1f]([a-z0-9])([^\$\x1f]+)", sf_str))
                                if not title:
                                    title = sf_map.get("a") or sf_map.get("t")
                                if not creator:
                                    creator = sf_map.get("g") or sf_map.get("c")

                if not title:
                    continue

                title = re.sub(r"[\s/.,;:]+$", "", title).strip()
                if creator:
                    creator = re.sub(r"[\s/.,;:]+$", "", creator).strip()

                seq_num: Optional[int] = None
                for seq_pred in getattr(field, "sequence_predicates", []):
                    for seq_uri in _expand_both(profile, seq_pred):
                        for seq_obj in g.objects(part, URIRef(seq_uri)):
                            try:
                                seq_num = int(str(seq_obj).strip())
                                break
                            except (ValueError, TypeError):
                                pass
                        if seq_num is not None:
                            break

                found.append(
                    Constituent(
                        title=title,
                        creator=creator,
                        ordinal=seq_num,
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

