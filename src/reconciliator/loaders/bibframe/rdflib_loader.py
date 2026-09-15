"""High-fidelity RDF graph loader for BIBFRAME 2.0, BIBFRAME 3.0, and Finnish BFFI 1.0."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Iterator, Optional, Union

from rdflib import RDF, RDFS, SKOS, Graph, IdentifiedNode, Literal, URIRef

from reconciliator.loaders.bibframe.base import (
    bibframe_to_raw_record,
    extract_provenance_tag,
    normalize_role_uri_or_label,
    parse_agent_dict,
)
from reconciliator.loaders.bibframe.decomposition import (
    derive_bibframe_node_id,
    extract_bibframe_subjects,
    is_blank_node,
)
from reconciliator.loaders.bibframe.profiles import (
    BIBFRAMESchemaProfile,
    detect_profile,
)

logger = logging.getLogger(__name__)


def _guess_rdf_format(file_path: Path) -> str:
    """Guess rdflib parser format from file extension."""
    suffix = file_path.suffix.lower()
    if suffix in (".ttl", ".turtle"):
        return "turtle"
    elif suffix in (".jsonld", ".json"):
        return "json-ld"
    elif suffix in (".nt", ".ntriples"):
        return "nt"
    elif suffix in (".n3",):
        return "n3"
    elif suffix in (".rdf", ".xml", ".owl"):
        return "xml"
    return "turtle"


def _get_literals(g: Graph, subject: IdentifiedNode, predicates: set[str] | list[str]) -> list[str]:
    """Retrieve string values for given predicates on a subject."""
    results: list[str] = []
    for pred in predicates:
        pred_ref = URIRef(pred) if not isinstance(pred, URIRef) else pred
        for obj in g.objects(subject, pred_ref):
            if isinstance(obj, Literal):
                val = str(obj).strip()
                if val and val not in results:
                    results.append(val)
            elif isinstance(obj, IdentifiedNode):
                # Fallback to rdfs:label or skos:prefLabel of obj
                for label in g.objects(obj, RDFS.label):
                    val = str(label).strip()
                    if val and val not in results:
                        results.append(val)
                for label in g.objects(obj, SKOS.prefLabel):
                    val = str(label).strip()
                    if val and val not in results:
                        results.append(val)
    return results


def _get_uris(g: Graph, subject: IdentifiedNode, predicates: set[str] | list[str]) -> list[str]:
    """Retrieve URIRef strings for given predicates on a subject."""
    results: list[str] = []
    for pred in predicates:
        pred_ref = URIRef(pred) if not isinstance(pred, URIRef) else pred
        for obj in g.objects(subject, pred_ref):
            if isinstance(obj, URIRef):
                u = str(obj).strip()
                if u and u not in results:
                    results.append(u)
    return results


def _extract_title(
    g: Graph, node: IdentifiedNode, profile: BIBFRAMESchemaProfile
) -> tuple[str, Optional[str], Optional[str], list[str]]:
    """Extract (main_title, subtitle, uniform_title, variant_titles) from an Instance, Expression, or Work node."""
    main_title = ""
    subtitle: Optional[str] = None
    uniform_title: Optional[str] = None
    variant_titles: list[str] = []

    # Look for title nodes
    for pred in profile.expanded_title_predicates:
        for title_obj in g.objects(node, URIRef(pred)):
            if isinstance(title_obj, Literal):
                if not main_title:
                    main_title = str(title_obj).strip()
                else:
                    variant_titles.append(str(title_obj).strip())
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
                    if mt:
                        variant_titles.append(mt)
                else:
                    if mt and not main_title:
                        main_title = mt
                        if st and not subtitle:
                            subtitle = st
                    elif mt:
                        variant_titles.append(mt)

    # Fallback to rdfs:label on the node itself if title still empty
    if not main_title:
        labels = _get_literals(g, node, [str(RDFS.label), str(SKOS.prefLabel)])
        if labels:
            main_title = labels[0]

    return main_title, subtitle, uniform_title, variant_titles


def _extract_agents(
    g: Graph, nodes: list[IdentifiedNode], profile: BIBFRAMESchemaProfile
) -> list[dict[str, Any]]:
    """Extract agents (creators, contributors, editors) across Work, Expression, and Instance nodes."""
    agents: list[dict[str, Any]] = []
    seen_names: set[str] = set()

    for node in nodes:
        for contrib_pred in profile.expanded_contribution_predicates:
            for contrib_obj in g.objects(node, URIRef(contrib_pred)):
                if not isinstance(contrib_obj, IdentifiedNode):
                    continue

                # Check for provenance literal on contribution node
                marc_key = None
                if profile.expanded_provenance_predicate:
                    mk_lits = _get_literals(g, contrib_obj, [profile.expanded_provenance_predicate])
                    if mk_lits:
                        marc_key = mk_lits[0]
                tag = extract_provenance_tag(marc_key, profile=profile)

                # Extract role
                role: Optional[str] = None
                for role_pred in profile.expanded_role_predicates:
                    for role_obj in g.objects(contrib_obj, URIRef(role_pred)):
                        r_label = None
                        if isinstance(role_obj, IdentifiedNode):
                            r_labels = _get_literals(
                                g, role_obj, [str(RDFS.label), str(SKOS.prefLabel), str(RDF.value)]
                            )
                            if r_labels:
                                r_label = r_labels[0]
                        # Every branch goes through the normaliser. A literal
                        # role and a bare label used to be stored verbatim, so a
                        # BFFI record's "kääntäjä" reached the matcher as its own
                        # role while the MARC path called the same thing "trl".
                        if isinstance(role_obj, URIRef):
                            role = normalize_role_uri_or_label(str(role_obj), label=r_label, profile=profile)
                        elif isinstance(role_obj, Literal):
                            role = normalize_role_uri_or_label(str(role_obj).strip(), profile=profile)
                        elif r_label:
                            role = normalize_role_uri_or_label(r_label, profile=profile)

                # Extract agents within contribution
                for agent_pred in profile.expanded_agent_predicates:
                    for agent_obj in g.objects(contrib_obj, URIRef(agent_pred)):
                        if not isinstance(agent_obj, IdentifiedNode):
                            continue

                        # Determine if personal or organization/meeting from configuration
                        agent_types = {str(t) for t in g.objects(agent_obj, RDF.type)}
                        is_personal = not bool(profile.expanded_agent_corporate_classes.intersection(agent_types))

                        # Agent name: prefer label predicates and match keys from profile
                        name_preds = (
                            profile.expanded_agent_label_predicates
                            | profile.expanded_agent_match_key_predicates
                        )
                        names = _get_literals(g, agent_obj, name_preds)
                        if not names:
                            continue
                        name = names[0]
                        if name in seen_names:
                            continue
                        seen_names.add(name)

                        # Existing URIs
                        agent_uris: list[str] = []
                        if isinstance(agent_obj, URIRef) and not str(agent_obj).startswith("_:"):
                            agent_uris.append(str(agent_obj))
                        # owl:sameAs or configured same_as_predicate
                        same_pred = profile.expand_curie(profile.same_as_predicate)
                        for same in g.objects(agent_obj, URIRef(same_pred)):
                            if isinstance(same, URIRef):
                                agent_uris.append(str(same))

                        for id_pred in profile.expanded_identifier_predicates:
                            for id_obj in g.objects(agent_obj, URIRef(id_pred)):
                                if isinstance(id_obj, URIRef):
                                    agent_uris.append(str(id_obj))
                                elif isinstance(id_obj, IdentifiedNode):
                                    vals = _get_literals(g, id_obj, [str(RDF.value)])
                                    agent_uris.extend(vals)

                        agents.append(
                            parse_agent_dict(
                                name=name,
                                field_tag=tag,
                                is_personal=is_personal,
                                role=role,
                                uri_subfield_0=agent_uris,
                                uri_subfield_1=[],
                                profile=profile,
                            )
                        )
    return agents


def _extract_identifiers(
    g: Graph, instance_node: IdentifiedNode, profile: BIBFRAMESchemaProfile
) -> tuple[list[str], list[str], list[str], list[str]]:
    """Extract (isbns, issns, urns, other_ids) from an Instance node using profile-driven schemes."""
    isbns: list[str] = []
    issns: list[str] = []
    urns: list[str] = []
    other_ids: list[str] = []

    scheme_map = profile.expanded_identifier_scheme_mappings
    isbn_cands = scheme_map.get("isbn", set())
    issn_cands = scheme_map.get("issn", set())
    urn_cands = scheme_map.get("urn", set())

    for id_pred in profile.expanded_identifier_predicates:
        for id_obj in g.objects(instance_node, URIRef(id_pred)):
            if not isinstance(id_obj, IdentifiedNode):
                continue

            types = {str(t) for t in g.objects(id_obj, RDF.type)}
            vals = _get_literals(g, id_obj, [str(RDF.value), str(RDFS.label)])
            src_vals = _get_literals(g, id_obj, profile.expanded_source_predicates)
            for s_pred in profile.expanded_source_predicates:
                for s_obj in g.objects(id_obj, URIRef(s_pred)):
                    if isinstance(s_obj, IdentifiedNode):
                        src_vals.extend(
                            _get_literals(
                                g,
                                s_obj,
                                [str(RDFS.label), str(SKOS.prefLabel), str(RDF.value)],
                            )
                        )

            is_isbn = bool(types.intersection(isbn_cands)) or any(
                cand.lower() in s.lower() for cand in isbn_cands for s in src_vals
            )
            is_issn = bool(types.intersection(issn_cands)) or any(
                cand.lower() in s.lower() for cand in issn_cands for s in src_vals
            )
            is_urn = bool(types.intersection(urn_cands)) or any(
                cand.lower() in s.lower() for cand in urn_cands for s in src_vals
            )

            for val in vals:
                digits_only = val.replace("-", "").replace(" ", "").upper()
                is_standard_isbn = (len(digits_only) == 10 or len(digits_only) == 13) and (
                    digits_only[:-1].isdigit()
                    and (digits_only[-1].isdigit() or digits_only[-1] == "X")
                )

                if is_isbn or is_standard_isbn:
                    if val not in isbns:
                        isbns.append(val)
                elif is_issn:
                    if val not in issns:
                        issns.append(val)
                elif is_urn or val.lower().startswith("urn:"):
                    if val not in urns:
                        urns.append(val)
                else:
                    if val not in other_ids:
                        other_ids.append(val)

    # Check instance URI itself for URN
    if isinstance(instance_node, URIRef):
        u = str(instance_node)
        if any(cand.lower() in u.lower() for cand in urn_cands) or u.lower().startswith("urn:"):
            if u not in urns:
                urns.append(u)

    return isbns, issns, urns, other_ids


def _extract_provision_activity(
    g: Graph, instance_node: IdentifiedNode, profile: BIBFRAMESchemaProfile
) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """Extract (publication_year, publisher, publication_place) from an Instance node."""
    year: Optional[str] = None
    publisher: Optional[str] = None
    place: Optional[str] = None

    for prov_pred in profile.expanded_provision_activity_predicates:
        for prov_obj in g.objects(instance_node, URIRef(prov_pred)):
            if not isinstance(prov_obj, IdentifiedNode):
                continue

            # Date / year
            dates = _get_literals(g, prov_obj, profile.expanded_provision_date_predicates)
            if dates and not year:
                year = dates[0]

            # Agent / publisher
            if not publisher:
                agent_preds = (
                    profile.expanded_provision_publisher_predicates
                    | profile.expanded_agent_predicates
                )
                for agent_pred in agent_preds:
                    for ag_obj in g.objects(prov_obj, URIRef(agent_pred)):
                        if isinstance(ag_obj, Literal):
                            publisher = str(ag_obj).strip()
                            break
                        elif isinstance(ag_obj, IdentifiedNode):
                            name_preds = (
                                profile.expanded_agent_label_predicates
                                | profile.expanded_agent_match_key_predicates
                            )
                            ag_labels = _get_literals(g, ag_obj, name_preds)
                            if ag_labels:
                                publisher = ag_labels[0]
                                break
                    if publisher:
                        break

            # Fallback direct literal on prov_obj
            if not publisher:
                name_preds = (
                    profile.expanded_provision_publisher_predicates
                    | profile.expanded_agent_label_predicates
                )
                publishers = _get_literals(g, prov_obj, name_preds)
                if publishers:
                    publisher = publishers[0]

            # Place
            if not place:
                for pl_pred in profile.expanded_provision_place_predicates:
                    for pl_obj in g.objects(prov_obj, URIRef(pl_pred)):
                        if isinstance(pl_obj, Literal):
                            place = str(pl_obj).strip()
                            break
                        elif isinstance(pl_obj, IdentifiedNode):
                            pl_labels = _get_literals(g, pl_obj, profile.expanded_agent_label_predicates)
                            if pl_labels:
                                place = pl_labels[0]
                                break
                    if place:
                        break

            if not place:
                places = _get_literals(
                    g,
                    prov_obj,
                    profile.expanded_provision_place_predicates | profile.expanded_agent_label_predicates,
                )
                if places:
                    place = places[0]

    return year, publisher, place


def parse_bibframe_graph(
    g: Graph,
    profile: Optional[BIBFRAMESchemaProfile] = None,
    source: str = "target",
) -> Iterator[
    tuple[dict[str, Any], tuple[IdentifiedNode, Optional[IdentifiedNode], Optional[IdentifiedNode]]]
]:
    """
    Parse an RDFLib Graph according to a BIBFRAME Schema Profile.
    Yields (raw_record, (instance_node, expression_node, work_node)).
    """
    # 1. Profile autodetection if not explicitly given
    if profile is None:
        namespaces = {prefix: str(ns) for prefix, ns in g.namespaces()}
        root_types = {str(obj) for obj in g.objects(None, RDF.type)}
        profile = detect_profile(namespaces, root_types)

    # 2. Discover instances
    instances: list[IdentifiedNode] = []
    for inst_cls in profile.expanded_instance_classes:
        for s in g.subjects(RDF.type, URIRef(inst_cls)):
            if isinstance(s, IdentifiedNode) and s not in instances:
                instances.append(s)

    # If no explicitly typed instances, discover works directly
    if not instances:
        for work_cls in profile.expanded_work_classes:
            for s in g.subjects(RDF.type, URIRef(work_cls)):
                if isinstance(s, IdentifiedNode) and s not in instances:
                    instances.append(s)

    for instance_node in instances:
        # Resolve expression and work nodes
        expression_node: Optional[IdentifiedNode] = None
        work_node: Optional[IdentifiedNode] = None

        # Try navigating instance -> expression (BFFI pattern)
        for pred in profile.expanded_instance_to_expression_predicates:
            for obj in g.objects(instance_node, URIRef(pred)):
                if isinstance(obj, IdentifiedNode):
                    obj_types = {str(t) for t in g.objects(obj, RDF.type)}
                    if (
                        any(t in profile.expanded_expression_classes for t in obj_types)
                        or not expression_node
                    ):
                        expression_node = obj
                        break
            if expression_node:
                break

        # Try navigating expression -> work
        if expression_node:
            for pred in profile.expanded_expression_to_work_predicates:
                for obj in g.objects(expression_node, URIRef(pred)):
                    if isinstance(obj, IdentifiedNode):
                        work_node = obj
                        break
                if work_node:
                    break

        # Try navigating instance -> work directly (standard BIBFRAME 2/3 pattern)
        if not work_node:
            for pred in profile.expanded_instance_to_work_predicates:
                for obj in g.objects(instance_node, URIRef(pred)):
                    if isinstance(obj, IdentifiedNode):
                        obj_types = {str(t) for t in g.objects(obj, RDF.type)}
                        if (
                            any(t in profile.expanded_work_classes for t in obj_types)
                            or not work_node
                        ):
                            work_node = obj
                            break
                if work_node:
                    break

        # Collect relevant nodes in hierarchy
        entity_nodes: list[IdentifiedNode] = [instance_node]
        if expression_node:
            entity_nodes.append(expression_node)
        if work_node and work_node not in entity_nodes:
            entity_nodes.append(work_node)

        # Source ID
        source_id = str(instance_node)
        if is_blank_node(instance_node):
            # A blank-node Instance has no id of its own: prefer a real
            # identifier, then the work's identity, and only then a digest of the
            # node's own triples. The test was `startswith("_:")`, which
            # `str(BNode())` never produces, so this whole branch was dead and the
            # record id was rdflib's parse-local label - a different target id
            # for the same input file on every run. Same defect as the ingest
            # side's work/expression/manifestation URIs, on the target side.
            isbns, issns, urns, other_ids = _extract_identifiers(g, instance_node, profile)
            source_id = (
                (urns[0] if urns else None)
                or (isbns[0] if isbns else None)
                or f"bnode:{derive_bibframe_node_id(g, work_node or instance_node)}"
            )
        else:
            isbns, issns, urns, other_ids = _extract_identifiers(g, instance_node, profile)

        # Titles: search expression first, then instance, then work
        title, subtitle, uniform_title, variant_titles = "", None, None, []
        for n in [expression_node, instance_node, work_node]:
            if n:
                t, st, ut, vt = _extract_title(g, n, profile)
                if not title and t:
                    title = t
                if not subtitle and st:
                    subtitle = st
                if not uniform_title and ut:
                    uniform_title = ut
                variant_titles.extend(vt)

        # Language: search expression, then instance, then work
        language: Optional[str] = None
        for n in [expression_node, instance_node, work_node]:
            if n:
                for l_pred in profile.expanded_language_predicates:
                    for l_obj in g.objects(n, URIRef(l_pred)):
                        if isinstance(l_obj, URIRef):
                            language = str(l_obj).split("/")[-1]
                        elif isinstance(l_obj, Literal):
                            language = str(l_obj)
                        if language:
                            break
                    if language:
                        break
            if language:
                break

        # Agents
        agents = _extract_agents(g, entity_nodes, profile)

        # Subjects
        subjects = extract_bibframe_subjects(g, entity_nodes, profile)

        # Provision activity (publisher, date, place)
        pub_year, publisher, pub_place = _extract_provision_activity(g, instance_node, profile)

        raw_rec = bibframe_to_raw_record(
            source_id=source_id,
            source=source,
            title=title,
            subtitle=subtitle,
            uniform_title=uniform_title,
            variant_titles=list(set(variant_titles)),
            publication_year=pub_year,
            publisher=publisher,
            publication_place=pub_place,
            language=language,
            isbns=isbns,
            issns=issns,
            urns=urns,
            other_ids=other_ids,
            agents=agents,
            subjects=subjects,
            profile=profile,
        )

        yield raw_rec, (instance_node, expression_node, work_node)


def iter_bibframe_records(
    file_path: Union[Path, str],
    profile: Optional[BIBFRAMESchemaProfile] = None,
    source: str = "target",
    format: Optional[str] = None,
) -> Iterator[
    tuple[
        dict[str, Any],
        int,
        tuple[Graph, tuple[IdentifiedNode, Optional[IdentifiedNode], Optional[IdentifiedNode]]],
    ]
]:
    """Stream BIBFRAME records from an RDF file (Turtle, RDF/XML, JSON-LD, etc.)."""
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"BIBFRAME file not found at: {path}")

    rdf_format = format or _guess_rdf_format(path)
    g = Graph()
    g.parse(str(path), format=rdf_format)

    file_size = path.stat().st_size
    for raw_rec, nodes in parse_bibframe_graph(g, profile=profile, source=source):
        yield raw_rec, file_size, (g, nodes)
