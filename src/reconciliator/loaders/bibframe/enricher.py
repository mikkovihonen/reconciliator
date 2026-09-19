"""In-place native enrichment of BIBFRAME RDF graphs and JSON-LD documents with resolved URIs."""

from __future__ import annotations

import logging
from typing import Any, Optional

from rdflib import RDF, RDFS, Graph, IdentifiedNode, URIRef

from reconciliator.config import EnrichmentTogglesConfig
from reconciliator.utils.curie import extract_wikidata_qid, format_wikidata_uri
from reconciliator.loaders.bibframe.profiles import BIBFRAMESchemaProfile, get_profile
from reconciliator.reconciliation import ReconciliationResult, WEMIMatchResult
from reconciliator.models import (
    BibliographicRecord,
    CorpusLinkage,
    ExpressionCluster,
    ManifestationCluster,
    WorkCluster,
)

logger = logging.getLogger(__name__)


def enrich_bibframe_graph(
    g: Graph,
    nodes: tuple[IdentifiedNode, Optional[IdentifiedNode], Optional[IdentifiedNode]],
    result: Any = None,
    profile: Optional[BIBFRAMESchemaProfile] = None,
    target_record: Optional[BibliographicRecord] = None,
    enrich: bool = True,
    fold_legacy: bool = True,
    wemi_result: Optional[WEMIMatchResult] = None,
    linkage: Optional[CorpusLinkage] = None,
    work_cluster: Optional[WorkCluster] = None,
    expression_cluster: Optional[ExpressionCluster] = None,
    manifestation_cluster: Optional[ManifestationCluster] = None,
    enrichment_config: Optional[EnrichmentTogglesConfig] = None,
) -> Graph:
    """Enrich an RDF graph in-place with resolved authority, vocabulary, and WEMI graph URIs.

    Adds ``owl:sameAs`` linkages, MARC relator predicate upgrades, FAST/YSO vocabulary
    mappings, and canonical WEMI graph clusters depending on which pipeline stages ran.
    """
    if not enrich:
        return g

    p = profile or get_profile()
    toggles = enrichment_config or EnrichmentTogglesConfig()
    instance_node, expression_node, work_node = nodes

    owl_same_as = URIRef(p.expand_curie(p.same_as_predicate))
    bf_subject = URIRef(
        p.expand_curie(p.subject_predicates[0])
        if p.subject_predicates
        else p.expand_curie("bf:subject")
    )
    bf_expression_of = URIRef(
        p.expand_curie(p.expression_to_work_predicates[0])
        if p.expression_to_work_predicates
        else p.expand_curie("bf:expressionOf")
    )
    bf_instance_of = URIRef(
        p.expand_curie(p.instance_to_work_predicates[0])
        if p.instance_to_work_predicates
        else p.expand_curie("bf:instanceOf")
    )
    bf_has_part = URIRef(p.expand_curie("bf:hasPart"))

    # 1. Legacy Agent & Subject Enrichment if ReconciliationResult is provided
    rec: Optional[BibliographicRecord] = None
    if isinstance(result, ReconciliationResult):
        rec = target_record or result.target_record
    elif target_record:
        rec = target_record

    if rec:
        agent_nodes: list[IdentifiedNode] = []
        entity_nodes: list[IdentifiedNode] = [
            n for n in [work_node, expression_node, instance_node] if n is not None
        ]

        for enode in entity_nodes:
            for contrib_pred in p.expanded_contribution_predicates:
                for contrib_obj in g.objects(enode, URIRef(contrib_pred)):
                    if isinstance(contrib_obj, IdentifiedNode):
                        for ag_pred in p.expanded_agent_predicates:
                            for ag_obj in g.objects(contrib_obj, URIRef(ag_pred)):
                                if isinstance(ag_obj, IdentifiedNode) and ag_obj not in agent_nodes:
                                    agent_nodes.append(ag_obj)

        if isinstance(result, ReconciliationResult):
            for idx, agent in enumerate(rec.agents):
                selected_uris = result.selected_agent_uris.get(idx, [])
                if not selected_uris:
                    continue

                target_ag_node: Optional[IdentifiedNode] = None
                if idx < len(agent_nodes):
                    target_ag_node = agent_nodes[idx]
                elif agent_nodes:
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
                        if (target_ag_node, owl_same_as, uri_ref) not in g:
                            g.add((target_ag_node, owl_same_as, uri_ref))

        # Subjects
        all_target_subjects = rec.subjects.all_subjects()

        for sub_item in all_target_subjects:
            if sub_item.uri:
                subj_target_ref = URIRef(sub_item.uri)
                for target_node in [n for n in (work_node, expression_node) if n is not None]:
                    g.add((target_node, bf_subject, subj_target_ref))

    # 2. WEMI Authority Graph & Hierarchy Linking
    wemi_res: Optional[WEMIMatchResult] = (
        result if isinstance(result, WEMIMatchResult) else wemi_result
    )
    cor_link: Optional[CorpusLinkage] = result if isinstance(result, CorpusLinkage) else linkage

    work_uri: Optional[str] = None
    expression_uri: Optional[str] = None
    manifestation_uri: Optional[str] = None
    wikidata_qid: Optional[str] = None

    if wemi_res:
        work_uri = wemi_res.work_uri
        expression_uri = wemi_res.expression_uri
        manifestation_uri = wemi_res.manifestation_uri
        if wemi_res.matched_work:
            if wemi_res.matched_work.source == "wikidata":
                wikidata_qid = wemi_res.matched_work.source_id
            elif getattr(wemi_res.matched_work, "wikidata_qid", None):
                wikidata_qid = wemi_res.matched_work.wikidata_qid
            elif wemi_res.matched_work.curie:
                wikidata_qid = extract_wikidata_qid(wemi_res.matched_work.curie)

    if cor_link:
        work_uri = cor_link.final_work_curie or work_uri
        expression_uri = cor_link.final_expression_curie or expression_uri

    if work_cluster:
        work_uri = work_cluster.canonical_work_curie or work_uri
        if not wikidata_qid and work_cluster.canonical_work_curie:
            wikidata_qid = extract_wikidata_qid(work_cluster.canonical_work_curie)

    if expression_cluster:
        expression_uri = expression_cluster.canonical_expression_curie or expression_uri

    if manifestation_cluster:
        pass

    # Inject Work-level authority owl:sameAs
    if toggles.inject_work_links and work_uri and work_node:
        w_ref = URIRef(work_uri)
        if toggles.inject_same_as and (work_node, owl_same_as, w_ref) not in g:
            g.add((work_node, owl_same_as, w_ref))

        if toggles.inject_same_as and wikidata_qid:
            q_ref = URIRef(format_wikidata_uri(wikidata_qid))
            if (work_node, owl_same_as, q_ref) not in g:
                g.add((work_node, owl_same_as, q_ref))

    # Inject Expression-level owl:sameAs & expressionOf
    if toggles.inject_expression_links and expression_uri and expression_node:
        e_ref = URIRef(expression_uri)
        if toggles.inject_same_as and (expression_node, owl_same_as, e_ref) not in g:
            g.add((expression_node, owl_same_as, e_ref))
        if work_node and (expression_node, bf_expression_of, work_node) not in g:
            g.add((expression_node, bf_expression_of, work_node))

    # Inject Manifestation-level owl:sameAs & instanceOf
    if manifestation_uri and instance_node:
        m_ref = URIRef(manifestation_uri)
        if toggles.inject_same_as and (instance_node, owl_same_as, m_ref) not in g:
            g.add((instance_node, owl_same_as, m_ref))
        if (
            toggles.inject_work_links
            and work_node
            and (instance_node, bf_instance_of, work_node) not in g
        ):
            g.add((instance_node, bf_instance_of, work_node))

    # Specialized RDF class typing
    if wemi_res and wemi_res.matched_work and work_node:
        wt = wemi_res.matched_work.work_type
        if wt in p.expanded_work_type_classes:
            g.add((work_node, RDF.type, URIRef(p.expanded_work_type_classes[wt])))

    # Constituent Work Parts
    if toggles.inject_component_links and wemi_res and wemi_res.constituent_matches and work_node:
        for cm in wemi_res.constituent_matches:
            c_ref = URIRef(cm.work_uri)
            g.add((work_node, bf_has_part, c_ref))

    return g


def enrich_bibframe_jsonld(
    doc: dict[str, Any],
    result: Any = None,
    profile: Optional[BIBFRAMESchemaProfile] = None,
    target_record: Optional[BibliographicRecord] = None,
    enrich: bool = True,
    fold_legacy: bool = True,
    wemi_result: Optional[WEMIMatchResult] = None,
    linkage: Optional[CorpusLinkage] = None,
    work_cluster: Optional[WorkCluster] = None,
    expression_cluster: Optional[ExpressionCluster] = None,
    manifestation_cluster: Optional[ManifestationCluster] = None,
    enrichment_config: Optional[EnrichmentTogglesConfig] = None,
) -> dict[str, Any]:
    """Enrich a JSON-LD record dictionary in-place with resolved authority URIs and reconciliation metadata."""
    toggles = enrichment_config or EnrichmentTogglesConfig()
    rec: Optional[BibliographicRecord] = None
    if isinstance(result, ReconciliationResult):
        rec = target_record or result.target_record
    elif target_record:
        rec = target_record

    # Find nodes in graph or root
    nodes: list[dict[str, Any]] = []
    if isinstance(doc.get("@graph"), list):
        nodes = [n for n in doc["@graph"] if isinstance(n, dict)]
    else:
        nodes = [doc]

    if enrich and rec and isinstance(result, ReconciliationResult):
        p = profile or get_profile()
        agent_keys = (
            set(p.expanded_agent_predicates)
            | set(p.agent_predicates)
            | {k.split(":")[-1] for k in p.agent_predicates}
        )

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

            if target_node is not None and toggles.inject_same_as:
                same_as_list = target_node.get("sameAs", [])
                if isinstance(same_as_list, str):
                    same_as_list = [same_as_list]
                elif not isinstance(same_as_list, list):
                    same_as_list = []

                for u in selected:
                    if u not in same_as_list:
                        same_as_list.append(u)
                target_node["sameAs"] = same_as_list

    # WEMI Resolution Metadata Block
    wemi_res: Optional[WEMIMatchResult] = (
        result if isinstance(result, WEMIMatchResult) else wemi_result
    )
    cor_link: Optional[CorpusLinkage] = result if isinstance(result, CorpusLinkage) else linkage

    if wemi_res:
        doc["wemi_reconciliation"] = {
            "status": "matched" if wemi_res.matched_work else "unmatched",
            "match_tier": wemi_res.match_tier,
            "funnel_used": wemi_res.funnel_used,
            "confidence_score": wemi_res.confidence_score,
            "work_uri": wemi_res.work_uri if toggles.inject_work_links else None,
            "expression_uri": wemi_res.expression_uri if toggles.inject_expression_links else None,
            "manifestation_uri": wemi_res.manifestation_uri,
        }
    elif cor_link:
        doc["wemi_reconciliation"] = {
            "status": "clustered",
            "work_cluster_id": cor_link.work_cluster_id,
            "expression_cluster_id": cor_link.expression_cluster_id,
            "final_work_uri": cor_link.final_work_uri if toggles.inject_work_links else None,
            "final_expression_uri": cor_link.final_expression_uri
            if toggles.inject_expression_links
            else None,
        }

    if isinstance(result, ReconciliationResult) and rec:
        best_match = result.matches[0] if result.matches else None
        agent_reports: list[dict[str, Any]] = []
        for idx, agent in enumerate(rec.agents):
            agent_reports.append(
                {
                    "name": agent.name,
                    "role": getattr(agent, "role", None),
                    "selected_uris": result.selected_agent_uris.get(idx, []),
                }
            )

        doc["reconciliation"] = {
            "status": "matched" if result.accepted_candidates else "unmatched",
            "match_tier": best_match.match_tier if best_match else "unmatched",
            "match_score": best_match.match_score if best_match else 0.0,
            "matched_authority_id": result.accepted_candidates[0].source_id
            if result.accepted_candidates
            else None,
            "matched_authority_source": result.accepted_candidates[0].source
            if result.accepted_candidates
            else None,
            "agents": agent_reports,
        }

    return doc
