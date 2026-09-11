"""In-place native enrichment of BIBFRAME RDF graphs and JSON-LD documents with resolved URIs."""

from __future__ import annotations

import logging
from typing import Any, Optional

from rdflib import RDFS, Graph, IdentifiedNode, URIRef

from reconciliator.loaders.bibframe.profiles import BIBFRAMESchemaProfile, get_profile
from reconciliator.matcher import ReconciliationResult
from reconciliator.models import BibliographicRecord

logger = logging.getLogger(__name__)

OWL_SAME_AS = URIRef("http://www.w3.org/2002/07/owl#sameAs")
BF_IDENTIFIED_BY = URIRef("http://id.loc.gov/ontologies/bibframe/identifiedBy")


def enrich_bibframe_graph(
    g: Graph,
    nodes: tuple[IdentifiedNode, Optional[IdentifiedNode], Optional[IdentifiedNode]],
    result: ReconciliationResult,
    profile: Optional[BIBFRAMESchemaProfile] = None,
    target_record: Optional[BibliographicRecord] = None,
    enrich: bool = True,
    fold_legacy: bool = True,
) -> Graph:
    """
    Enrich an RDF graph in-place with resolved authority and vocabulary URIs.
    Maintains all unmapped triples and namespaces without degradation.
    """
    if not enrich:
        return g

    p = profile or get_profile()
    instance_node, expression_node, work_node = nodes
    rec = target_record or result.target_record

    # 1. Enrich Agents
    # Collect all agent nodes linked via contributions
    agent_nodes: list[IdentifiedNode] = []
    entity_nodes: list[IdentifiedNode] = [n for n in [work_node, expression_node, instance_node] if n is not None]

    for enode in entity_nodes:
        for contrib_pred in p.expanded_contribution_predicates:
            for contrib_obj in g.objects(enode, URIRef(contrib_pred)):
                if isinstance(contrib_obj, IdentifiedNode):
                    for ag_pred in p.expanded_agent_predicates:
                        for ag_obj in g.objects(contrib_obj, URIRef(ag_pred)):
                            if isinstance(ag_obj, IdentifiedNode) and ag_obj not in agent_nodes:
                                agent_nodes.append(ag_obj)

    for idx, agent in enumerate(rec.agents):
        selected_uris = result.selected_agent_uris.get(idx, [])
        if not selected_uris:
            continue

        # Match corresponding agent node by index or name
        target_ag_node: Optional[IdentifiedNode] = None
        if idx < len(agent_nodes):
            target_ag_node = agent_nodes[idx]
        elif agent_nodes:
            # Fallback search by label
            for an in agent_nodes:
                for lbl in g.objects(an, RDFS.label):
                    if str(lbl).strip().lower() == agent.name.strip().lower():
                        target_ag_node = an
                        break
                if target_ag_node:
                    break

        if target_ag_node:
            for uri in selected_uris:
                uri_ref = URIRef(uri)
                # Check if triple already exists
                if (target_ag_node, OWL_SAME_AS, uri_ref) not in g:
                    g.add((target_ag_node, OWL_SAME_AS, uri_ref))

    # 2. Enrich Subjects
    all_target_subjects = (
        list(rec.subjects.topical)
        + list(rec.subjects.genre_form)
        + list(rec.subjects.geographic)
        + list(rec.subjects.medium_of_performance)
    )

    for sub_item in all_target_subjects:
        if sub_item.uri:
            # Inject target URI to work / expression subject
            subj_target_ref = URIRef(sub_item.uri)
            for target_node in [n for n in (work_node, expression_node) if n is not None]:
                g.add((target_node, URIRef("http://id.loc.gov/ontologies/bibframe/subject"), subj_target_ref))

    return g


def enrich_bibframe_jsonld(
    doc: dict[str, Any],
    result: ReconciliationResult,
    profile: Optional[BIBFRAMESchemaProfile] = None,
    target_record: Optional[BibliographicRecord] = None,
    enrich: bool = True,
    fold_legacy: bool = True,
) -> dict[str, Any]:
    """
    Enrich a JSON-LD record dictionary in-place with resolved authority URIs and reconciliation metadata.
    """
    rec = target_record or result.target_record

    # Find nodes in graph or root
    nodes: list[dict[str, Any]] = []
    if isinstance(doc.get("@graph"), list):
        nodes = [n for n in doc["@graph"] if isinstance(n, dict)]
    else:
        nodes = [doc]

    if enrich:
        p = profile or get_profile()
        agent_keys = set(p.agent_predicates) | {"bf:agent", "agent"}

        # Find agents in nodes
        discovered_agents: list[dict[str, Any]] = []
        for n in nodes:
            for k, v in n.items():
                if k in agent_keys:
                    if isinstance(v, dict):
                        discovered_agents.append(v)
                    elif isinstance(v, list):
                        discovered_agents.extend([item for item in v if isinstance(item, dict)])

        for idx, agent in enumerate(rec.agents):
            selected = result.selected_agent_uris.get(idx, [])
            if not selected:
                continue

            target_node: Optional[dict[str, Any]] = None
            if idx < len(discovered_agents):
                target_node = discovered_agents[idx]

            if target_node is not None:
                same_as_list = target_node.get("sameAs", [])
                if isinstance(same_as_list, str):
                    same_as_list = [same_as_list]
                elif not isinstance(same_as_list, list):
                    same_as_list = []

                for u in selected:
                    if u not in same_as_list:
                        same_as_list.append(u)
                target_node["sameAs"] = same_as_list

    # Add audit reconciliation block
    best_match = result.matches[0] if result.matches else None
    agent_reports: list[dict[str, Any]] = []
    for idx, agent in enumerate(rec.agents):
        agent_reports.append(
            {
                "name": agent.name,
                "role": agent.role,
                "selected_uris": result.selected_agent_uris.get(idx, []),
            }
        )

    doc["reconciliation"] = {
        "status": "matched" if result.accepted_candidates else "unmatched",
        "match_tier": best_match.match_tier if best_match else "unmatched",
        "match_score": best_match.match_score if best_match else 0.0,
        "matched_authority_id": result.accepted_candidates[0].source_id if result.accepted_candidates else None,
        "matched_authority_source": result.accepted_candidates[0].source if result.accepted_candidates else None,
        "agents": agent_reports,
    }

    return doc
