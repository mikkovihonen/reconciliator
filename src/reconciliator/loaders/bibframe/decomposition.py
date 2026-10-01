"""High-level BIBFRAME and BFFI WEMI decomposition orchestrator."""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any, Optional

from rdflib import RDF, Graph, IdentifiedNode, URIRef

from reconciliator.embedding.template import WEMISerializationEngine
from reconciliator.loaders.bibframe.classification import (
    _BF_CLASSIFYING_PREDICATES,
    BIBFRAME_CONTENT_TYPES,
    BIBFRAME_CONTENT_WORK_TYPES,
    BIBFRAME_WORK_TYPES,
    _is_series,
    determine_bibframe_content_type,
    determine_bibframe_work_type,
)
from reconciliator.loaders.bibframe.constituents import (
    _expand_both,
    _part_creator,
    _part_title,
    extract_bibframe_constituents,
)
from reconciliator.loaders.bibframe.entities import (
    _extract_identifiers,
    _extract_items,
    _extract_language,
    _extract_mediums_of_performance,
    _extract_node_agents,
    _extract_title_elements,
    _extract_work_relationships,
    extract_bibframe_subjects,
)
from reconciliator.loaders.bibframe.graph_utils import (
    _get_literals,
    _get_uris,
    _identifiers_as_bag,
    bibframe_work_uri,
    derive_bibframe_node_id,
    extract_marc_key_components,
    is_blank_node,
)
from reconciliator.loaders.bibframe.profiles import (
    BIBFRAMESchemaProfile,
    detect_profile,
)
from reconciliator.models import (
    AgentEntity,
    CarrierType,
    ContributionEntity,
    EntitySubject,
    ExpressionContentType,
    ExpressionEntity,
    IdentifierEntity,
    ItemEntity,
    ManifestationEntity,
    ManifestationExpressionAssociation,
    MatchRecord,
    WorkEntity,
    WorkRelationship,
    WorkType,
)
from reconciliator.normalizers.blocking import compute_author_key, compute_title_stem
from reconciliator.normalizers.keys import (
    derive_bibframe_keys,
)
from reconciliator.vocab import (
    detect_vocabulary,
    extract_concept_id,
    fold_subjects,
)

if TYPE_CHECKING:
    from reconciliator.loaders.bibframe.profiles import BIBFRAMESchemaProfile

logger = logging.getLogger(__name__)

__all__ = [
    "BIBFRAME_CONTENT_TYPES",
    "BIBFRAME_CONTENT_WORK_TYPES",
    "BIBFRAME_WORK_TYPES",
    "_BF_CLASSIFYING_PREDICATES",
    "_expand_both",
    "_extract_identifiers",
    "_extract_items",
    "_extract_language",
    "_extract_mediums_of_performance",
    "_extract_node_agents",
    "_extract_title_elements",
    "_extract_work_relationships",
    "_get_literals",
    "_get_uris",
    "_identifiers_as_bag",
    "_is_series",
    "_part_creator",
    "_part_title",
    "bibframe_work_uri",
    "build_target_record_from_bibframe",
    "decompose_bibframe_work",
    "derive_bibframe_node_id",
    "determine_bibframe_content_type",
    "determine_bibframe_work_type",
    "extract_bibframe_constituents",
    "extract_bibframe_subjects",
    "extract_marc_key_components",
    "is_blank_node",
]

def decompose_bibframe_work(
    g: Graph,
    instance_node: Optional[IdentifiedNode] = None,
    profile: Optional[BIBFRAMESchemaProfile] = None,
    source: str = "bibframe",
    serialization_engine: Optional[WEMISerializationEngine] = None,
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
    mediums_of_perf: list[dict[str, Any]] = []

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
        extracted = _extract_node_agents(g, n, profile, source=source)
        for agent_ent, role, is_primary, marc_key in extracted:
            existing = all_agents_map.get(agent_ent.agent_uri)
            if existing is not None:
                agent_ent = existing
            all_agents_map[agent_ent.agent_uri] = agent_ent
            # Role tracking is handled through contributions_table, not agent fields.
            # The role histogram is no longer stored on AgentEntity.
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
        key = (m.get("label"), m.get("uri"))
        if key not in seen_mops:
            seen_mops.add(key)
            deduped_mops.append(m)
    mediums_of_perf = deduped_mops
    expr_mop_str = ", ".join(m.get("label") for m in mediums_of_perf if m.get("label")) or None

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
    )
    work.serialized_text = engine.render_work(
        work,
        authors=authors_list,
        subjects=[subject.as_dict() for subject in folded_subjects],
    )
    if subject_sink is not None:
        for m in mediums_of_perf:
            m_uri = m.get("uri")
            m_label = m.get("label")
            vocab = detect_vocabulary(m_uri) if m_uri else "pmo"
            cid = extract_concept_id(m_uri) if m_uri else None
            subject_sink.append(
                EntitySubject(
                    entity_type="work",
                    entity_id=0,
                    entity_uri=work.work_uri,
                    source=source,
                    source_record_id=str(work_id),
                    relation_type="medium_of_performance",
                    vocabulary=vocab,
                    concept_id=cid or m_uri,
                    raw_label=m_label,
                    is_resolved=bool(vocab and (cid or m_uri)),
                    is_primary=False,
                )
            )
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
        contributions,
    )



def build_target_record_from_bibframe(
    g: Graph,
    instance_node: Optional[IdentifiedNode] = None,
    profile: Optional[BIBFRAMESchemaProfile] = None,
    source: str = "bibframe",
    serialization_engine: Optional[WEMISerializationEngine] = None,
    constituent_profile: Optional[Any] = None,
) -> MatchRecord:
    """Decompose a BIBFRAME graph instance and construct a MatchRecord for staging."""
    constituent_sink: list[Any] = []
    (
        work,
        expression,
        manifestation,
        items,
        agents,
        relationships,
        identifiers,
        associations,
        contributions,
    ) = decompose_bibframe_work(
        g=g,
        instance_node=instance_node,
        profile=profile,
        source=source,
        serialization_engine=serialization_engine,
        constituent_profile=constituent_profile,
        constituent_sink=constituent_sink if constituent_profile else None,
    )

    # Primary author for keys
    author_name = agents[0].name if agents else None
    author_key = compute_author_key(author_name)
    title_stem = compute_title_stem(work.title, lang=work.original_language)

    # One shared derivation, so the staged record and the persisted entity_keys
    # rows cannot drift apart.
    keys = derive_bibframe_keys(
        work, agents, identifiers, author_key, title_stem, constituents=constituent_sink
    )

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
