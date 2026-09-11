"""High-performance streaming JSON-LD parser for BIBFRAME 2.0, 3.0, and Finnish BFFI 1.0."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Iterator, Optional, Union

import orjson

from reconciliator.loaders.bibframe.base import (
    bibframe_to_raw_record,
    extract_provenance_tag,
    normalize_role_uri_or_label,
    parse_agent_dict,
)
from reconciliator.loaders.bibframe.profiles import (
    BIBFRAMESchemaProfile,
    detect_profile,
)

logger = logging.getLogger(__name__)


def _extract_values(obj: Any, keys: set[str] | list[str]) -> list[Any]:
    """Get all values matching any of the specified keys in a JSON-LD dict."""
    if not isinstance(obj, dict):
        return []
    res: list[Any] = []
    target_keys = set(keys)
    for k, v in obj.items():
        if k in target_keys:
            if isinstance(v, list):
                res.extend(v)
            else:
                res.append(v)
    return res


def _get_string_values(obj: Any, keys: set[str] | list[str]) -> list[str]:
    """Get clean string literals matching any of the specified keys."""
    raw_vals = _extract_values(obj, keys)
    results: list[str] = []
    for v in raw_vals:
        if isinstance(v, str):
            s = v.strip()
            if s and s not in results:
                results.append(s)
        elif isinstance(v, dict):
            # Might be {"@value": "..."}, or nested node with {"rdfs:label": "..."}
            val = (
                v.get("@value")
                or v.get("rdfs:label")
                or v.get("http://www.w3.org/2000/01/rdf-schema#label")
                or v.get("label")
                or v.get("skos:prefLabel")
                or v.get("http://www.w3.org/2004/02/skos/core#prefLabel")
                or v.get("prefLabel")
                or v.get("name")
            )
            if isinstance(val, str):
                s = val.strip()
                if s and s not in results:
                    results.append(s)
    return results


def _resolve_node(val: Any, node_index: dict[str, dict[str, Any]]) -> Optional[dict[str, Any]]:
    """Resolve an object or an @id reference to its node dictionary."""
    if isinstance(val, dict):
        ref_id = val.get("@id")
        if ref_id and ref_id in node_index:
            # Merge referenced node with inline attributes if any
            target = dict(node_index[ref_id])
            target.update(val)
            return target
        return val
    elif isinstance(val, str) and val in node_index:
        return node_index[val]
    return None


def _get_node_types(node: dict[str, Any]) -> set[str]:
    """Get all types declared for a node."""
    raw = node.get("@type", [])
    if isinstance(raw, str):
        return {raw}
    elif isinstance(raw, list):
        return {str(t) for t in raw}
    return set()


def _is_type_match(node: dict[str, Any], allowed_types: set[str]) -> bool:
    """Check if node has any type matching allowed_types."""
    node_types = _get_node_types(node)
    return bool(node_types & allowed_types)


def parse_jsonld_dict(
    doc: dict[str, Any],
    profile: Optional[BIBFRAMESchemaProfile] = None,
    source: str = "target",
) -> Iterator[tuple[dict[str, Any], dict[str, Any]]]:
    """
    Parse a JSON-LD document or graph dictionary.
    Yields (raw_record, instance_dict).
    """
    # 1. Profile detection from @context or types if profile not provided
    ctx = doc.get("@context", {})
    context_ns: dict[str, str] = {}
    if isinstance(ctx, dict):
        for k, v in ctx.items():
            if isinstance(v, str):
                context_ns[k] = v
    elif isinstance(ctx, list):
        for item in ctx:
            if isinstance(item, dict):
                for k, v in item.items():
                    if isinstance(v, str):
                        context_ns[k] = v

    if profile is None:
        profile = detect_profile(namespaces=context_ns)

    # 2. Build local node index
    node_index: dict[str, dict[str, Any]] = {}
    raw_graph = doc.get("@graph")
    nodes: list[dict[str, Any]] = []

    if isinstance(raw_graph, list):
        for n in raw_graph:
            if isinstance(n, dict):
                nodes.append(n)
                nid = n.get("@id")
                if nid:
                    node_index[nid] = n
    else:
        nodes.append(doc)
        nid = doc.get("@id")
        if nid:
            node_index[nid] = doc

    # 3. Discover instances
    instances: list[dict[str, Any]] = []
    allowed_inst_classes = (
        profile.expanded_instance_classes
        | set(profile.instance_classes)
        | {c.split(":")[-1] for c in profile.instance_classes}
    )

    for n in nodes:
        if _is_type_match(n, allowed_inst_classes):
            instances.append(n)

    # Fallback to works if no instances
    if not instances:
        allowed_work_classes = (
            profile.expanded_work_classes
            | set(profile.work_classes)
            | {c.split(":")[-1] for c in profile.work_classes}
        )
        for n in nodes:
            if _is_type_match(n, allowed_work_classes):
                instances.append(n)

    # If still nothing, but root node is present, use root node
    if not instances and nodes:
        instances = [nodes[0]]

    # 4. Extract records for each instance
    for inst in instances:
        inst_id = inst.get("@id", "")

        # Find expression and work nodes
        expression_node: Optional[dict[str, Any]] = None
        work_node: Optional[dict[str, Any]] = None

        # Look for expression via instance_to_expression_predicates
        inst_to_exp_keys = (
            profile.expanded_instance_to_expression_predicates
            | set(profile.instance_to_expression_predicates)
            | {p.split(":")[-1] for p in profile.instance_to_expression_predicates}
        )
        exp_candidates = _extract_values(inst, inst_to_exp_keys)
        for exp_c in exp_candidates:
            resolved = _resolve_node(exp_c, node_index)
            if resolved:
                expression_node = resolved
                break

        # Look for work via expression_to_work_predicates
        if expression_node:
            exp_to_work_keys = (
                profile.expanded_expression_to_work_predicates
                | set(profile.expression_to_work_predicates)
                | {p.split(":")[-1] for p in profile.expression_to_work_predicates}
            )
            work_candidates = _extract_values(expression_node, exp_to_work_keys)
            for wc in work_candidates:
                resolved = _resolve_node(wc, node_index)
                if resolved:
                    work_node = resolved
                    break

        # Look for work via instance_to_work_predicates directly
        if not work_node:
            inst_to_work_keys = (
                profile.expanded_instance_to_work_predicates
                | set(profile.instance_to_work_predicates)
                | {p.split(":")[-1] for p in profile.instance_to_work_predicates}
            )
            work_candidates = _extract_values(inst, inst_to_work_keys)
            for wc in work_candidates:
                resolved = _resolve_node(wc, node_index)
                if resolved:
                    work_node = resolved
                    break

        entity_nodes: list[dict[str, Any]] = [inst]
        if expression_node and expression_node not in entity_nodes:
            entity_nodes.append(expression_node)
        if work_node and work_node not in entity_nodes:
            entity_nodes.append(work_node)

        # Titles
        title_keys = (
            profile.expanded_title_predicates
            | set(profile.title_predicates)
            | {p.split(":")[-1] for p in profile.title_predicates}
        )
        main_title_keys = (
            profile.expanded_main_title_predicates
            | set(profile.main_title_predicates)
            | {p.split(":")[-1] for p in profile.main_title_predicates}
        )
        subtitle_keys = (
            profile.expanded_subtitle_predicates
            | set(profile.subtitle_predicates)
            | {p.split(":")[-1] for p in profile.subtitle_predicates}
        )

        title = ""
        subtitle: Optional[str] = None
        uniform_title: Optional[str] = None
        variant_titles: list[str] = []

        for n in [expression_node, inst, work_node]:
            if not n:
                continue
            title_objs = _extract_values(n, title_keys)
            for tobj in title_objs:
                res_title = _resolve_node(tobj, node_index) or (tobj if isinstance(tobj, dict) else None)
                if isinstance(tobj, str):
                    if not title:
                        title = tobj
                    else:
                        variant_titles.append(tobj)
                elif res_title:
                    m_titles = _get_string_values(res_title, main_title_keys)
                    s_titles = _get_string_values(res_title, subtitle_keys)
                    types = _get_node_types(res_title)
                    is_variant = any("Variant" in t or "KeyTitle" in t for t in types)

                    if m_titles:
                        mt = m_titles[0]
                        if is_variant:
                            variant_titles.append(mt)
                        else:
                            if not title:
                                title = mt
                                if s_titles and not subtitle:
                                    subtitle = s_titles[0]
                            else:
                                variant_titles.append(mt)

            if not title:
                # Fallback to rdfs:label
                labels = _get_string_values(n, ["rdfs:label", "http://www.w3.org/2000/01/rdf-schema#label", "label"])
                if labels:
                    title = labels[0]

        # Identifiers
        id_keys = (
            profile.expanded_identifier_predicates
            | set(profile.identifier_predicates)
            | {p.split(":")[-1] for p in profile.identifier_predicates}
        )
        isbns: list[str] = []
        issns: list[str] = []
        urns: list[str] = []
        other_ids: list[str] = []

        for id_val in _extract_values(inst, id_keys):
            res_id = _resolve_node(id_val, node_index)
            if res_id:
                types = _get_node_types(res_id)
                vals = _get_string_values(res_id, ["rdf:value", "http://www.w3.org/1999/02/22-rdf-syntax-ns#value", "value", "rdfs:label"])
                for v in vals:
                    if any("Isbn" in t for t in types) or "isbn" in v.lower():
                        if v not in isbns:
                            isbns.append(v)
                    elif any("Issn" in t for t in types) or "issn" in v.lower():
                        if v not in issns:
                            issns.append(v)
                    elif any("Urn" in t for t in types) or v.lower().startswith("urn:"):
                        if v not in urns:
                            urns.append(v)
                    else:
                        if v not in other_ids:
                            other_ids.append(v)
            elif isinstance(id_val, str):
                if id_val.lower().startswith("urn:"):
                    urns.append(id_val)
                else:
                    other_ids.append(id_val)

        if inst_id and ("urn:nbn:fi" in inst_id.lower() or "urn.fi" in inst_id.lower()):
            if inst_id not in urns:
                urns.append(inst_id)

        # Source ID
        source_id = inst_id or (urns[0] if urns else None) or (isbns[0] if isbns else None) or "bibframe-1"

        # Agents
        contrib_keys = (
            profile.expanded_contribution_predicates
            | set(profile.contribution_predicates)
            | {p.split(":")[-1] for p in profile.contribution_predicates}
        )
        agent_keys = (
            profile.expanded_agent_predicates
            | set(profile.agent_predicates)
            | {p.split(":")[-1] for p in profile.agent_predicates}
        )
        role_keys = (
            profile.expanded_role_predicates
            | set(profile.role_predicates)
            | {p.split(":")[-1] for p in profile.role_predicates}
        )

        agents: list[dict[str, Any]] = []
        seen_agent_names: set[str] = set()

        for n in entity_nodes:
            for contrib_val in _extract_values(n, contrib_keys):
                res_contrib = _resolve_node(contrib_val, node_index)
                if not res_contrib:
                    continue

                marc_key: Optional[str] = None
                if profile.provenance_predicate:
                    mk_keys = [profile.provenance_predicate, profile.provenance_predicate.split(":")[-1]]
                    mks = _get_string_values(res_contrib, mk_keys)
                    if mks:
                        marc_key = mks[0]
                tag = extract_provenance_tag(marc_key)

                # Extract role
                role: Optional[str] = None
                for role_val in _extract_values(res_contrib, role_keys):
                    if isinstance(role_val, str):
                        role = normalize_role_uri_or_label(role_val)
                    elif isinstance(role_val, dict):
                        res_role = _resolve_node(role_val, node_index) or role_val
                        r_labels = _get_string_values(res_role, ["rdfs:label", "skos:prefLabel", "rdf:value", "label"])
                        r_label = r_labels[0] if r_labels else None
                        r_id = res_role.get("@id")
                        if r_id:
                            role = normalize_role_uri_or_label(r_id, label=r_label)
                        elif r_label:
                            role = r_label

                # Extract agent
                for ag_val in _extract_values(res_contrib, agent_keys):
                    res_agent = _resolve_node(ag_val, node_index)
                    if not res_agent:
                        continue

                    types = _get_node_types(res_agent)
                    is_personal = not any("Organization" in t or "Corporate" in t or "Meeting" in t for t in types)

                    names = _get_string_values(
                        res_agent,
                        [
                            "rdfs:label",
                            "http://www.w3.org/2000/01/rdf-schema#label",
                            "skos:prefLabel",
                            "http://www.w3.org/2004/02/skos/core#prefLabel",
                            "bflc:name00MatchKey",
                            "name00MatchKey",
                            "bflc:name10MatchKey",
                            "name10MatchKey",
                            "label",
                        ],
                    )
                    if not names:
                        continue
                    name = names[0]
                    if name in seen_agent_names:
                        continue
                    seen_agent_names.add(name)

                    agent_uris: list[str] = []
                    ag_id = res_agent.get("@id")
                    if ag_id and not ag_id.startswith("_:"):
                        agent_uris.append(ag_id)

                    agents.append(
                        parse_agent_dict(
                            name=name,
                            field_tag=tag,
                            is_personal=is_personal,
                            role=role,
                            uri_subfield_0=agent_uris,
                            uri_subfield_1=[],
                        )
                    )

        # Subjects
        subj_keys = (
            profile.expanded_subject_predicates
            | set(profile.subject_predicates)
            | {p.split(":")[-1] for p in profile.subject_predicates}
        )
        subjects: dict[str, list[dict[str, Any]]] = {
            "topical": [],
            "genre_form": [],
            "geographic": [],
            "medium_of_performance": [],
        }

        for n in entity_nodes:
            for s_val in _extract_values(n, subj_keys):
                res_subj = _resolve_node(s_val, node_index)
                if not res_subj:
                    continue

                types = _get_node_types(res_subj)
                headings = _get_string_values(res_subj, ["rdfs:label", "skos:prefLabel", "label"])
                s_id = res_subj.get("@id")
                heading = headings[0] if headings else (s_id.split("/")[-1] if s_id else "")
                if not heading:
                    continue

                vocab = None
                if s_id:
                    s_lower = s_id.lower()
                    if "finto.fi/yso" in s_lower:
                        vocab = "yso"
                    elif "finto.fi/kauno" in s_lower:
                        vocab = "kauno"
                    elif "finto.fi/muso" in s_lower:
                        vocab = "muso"
                    elif "finto.fi/slm" in s_lower:
                        vocab = "slm"
                    elif "finto.fi/seko" in s_lower:
                        vocab = "seko"

                src_vals = _get_string_values(
                    res_subj,
                    ["bf:source", "source", "http://id.loc.gov/ontologies/bibframe/source"],
                )
                if src_vals and not vocab:
                    vocab = src_vals[0]

                item = {"heading": heading, "vocabulary": vocab, "uri": s_id}
                if any("GenreForm" in t for t in types):
                    item["field"] = "655"
                    subjects["genre_form"].append(item)
                elif any("Place" in t or "Geographic" in t for t in types):
                    item["field"] = "651"
                    subjects["geographic"].append(item)
                elif any("Music" in t or "mediumOfPerformance" in t for t in types):
                    item["field"] = "382"
                    subjects["medium_of_performance"].append(item)
                else:
                    item["field"] = "650"
                    subjects["topical"].append(item)

        # Provision Activity
        prov_keys = (
            profile.expanded_provision_activity_predicates
            | set(profile.provision_activity_predicates)
            | {p.split(":")[-1] for p in profile.provision_activity_predicates}
        )
        pub_year: Optional[str] = None
        publisher: Optional[str] = None
        pub_place: Optional[str] = None

        for prov_val in _extract_values(inst, prov_keys):
            res_prov = _resolve_node(prov_val, node_index)
            if not res_prov:
                continue

            dates = _get_string_values(res_prov, ["bf:date", "date", "http://id.loc.gov/ontologies/bibframe/date"])
            if dates and not pub_year:
                pub_year = dates[0]

            if not publisher:
                ag_keys = (
                    profile.expanded_agent_predicates
                    | set(profile.agent_predicates)
                    | {"bf:agent", "agent", "http://id.loc.gov/ontologies/bibframe/agent"}
                )
                for ag_v in _extract_values(res_prov, ag_keys):
                    res_ag = _resolve_node(ag_v, node_index)
                    if isinstance(res_ag, dict):
                        ag_names = _get_string_values(
                            res_ag,
                            ["rdfs:label", "skos:prefLabel", "label", "bflc:name00MatchKey", "http://www.w3.org/2000/01/rdf-schema#label"],
                        )
                        if ag_names:
                            publisher = ag_names[0]
                            break
                    elif isinstance(ag_v, str) and not ag_v.startswith("_:"):
                        publisher = ag_v
                        break

            if not pub_place:
                pl_keys = {
                    "bf:place",
                    "bffi:place",
                    "place",
                    "http://id.loc.gov/ontologies/bibframe/place",
                    "http://urn.fi/URN:NBN:fi:schema:bffi:place",
                }
                for pl_v in _extract_values(res_prov, pl_keys):
                    res_pl = _resolve_node(pl_v, node_index)
                    if isinstance(res_pl, dict):
                        pl_names = _get_string_values(
                            res_pl,
                            ["rdfs:label", "skos:prefLabel", "label", "http://www.w3.org/2000/01/rdf-schema#label"],
                        )
                        if pl_names:
                            pub_place = pl_names[0]
                            break
                    elif isinstance(pl_v, str) and not pl_v.startswith("_:"):
                        pub_place = pl_v
                        break

        # Language
        lang_keys = (
            profile.expanded_language_predicates
            | set(profile.language_predicates)
            | {p.split(":")[-1] for p in profile.language_predicates}
        )
        language: Optional[str] = None
        for n in [expression_node, inst, work_node]:
            if not n:
                continue
            langs = _get_string_values(n, lang_keys)
            if langs:
                language = langs[0].split("/")[-1]
                break

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

        yield raw_rec, inst


def iter_jsonld_bibframe_records(
    file_path: Union[Path, str],
    profile: Optional[BIBFRAMESchemaProfile] = None,
    source: str = "target",
) -> Iterator[tuple[dict[str, Any], int, dict[str, Any]]]:
    """
    Stream BIBFRAME records from a JSON-LD file (NDJSON lines or standard JSON-LD document).
    Tracks byte offsets efficiently with O(1) memory per record when streamed.
    """
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"JSON-LD file not found at: {path}")

    file_size = path.stat().st_size

    # First attempt: read the first non-empty line or small chunk to see if it's NDJSON
    with open(path, "rb") as f:
        first_line = f.readline().strip()

    is_ndjson = False
    if first_line:
        try:
            parsed = orjson.loads(first_line)
            if isinstance(parsed, dict) and not (first_line.startswith(b"{") and b'"@graph"' in first_line):
                # Check if there is a second line that parses as JSON
                with open(path, "rb") as f2:
                    f2.readline()
                    second_line = f2.readline().strip()
                    if second_line:
                        try:
                            orjson.loads(second_line)
                            is_ndjson = True
                        except Exception:
                            is_ndjson = False
        except Exception:
            is_ndjson = False

    if is_ndjson:
        # Stream line by line
        current_offset = 0
        with open(path, "rb") as f:
            for line in f:
                current_offset += len(line)
                clean_l = line.strip()
                if not clean_l:
                    continue
                try:
                    data = orjson.loads(clean_l)
                    if isinstance(data, dict):
                        for raw_rec, inst_node in parse_jsonld_dict(data, profile=profile, source=source):
                            yield raw_rec, current_offset, data
                except Exception as e:
                    logger.warning("Failed to parse JSON-LD line at offset %d: %s", current_offset, e)
    else:
        # Full file JSON-LD document
        with open(path, "rb") as f:
            content = f.read()
        doc = orjson.loads(content)
        if isinstance(doc, dict):
            for raw_rec, inst_node in parse_jsonld_dict(doc, profile=profile, source=source):
                yield raw_rec, file_size, doc
        elif isinstance(doc, list):
            # List of nodes / records
            doc_dict = {"@graph": doc}
            for raw_rec, inst_node in parse_jsonld_dict(doc_dict, profile=profile, source=source):
                yield raw_rec, file_size, doc_dict
