"""Formatting and conversion utilities for reconciliation pipeline records and reports."""

from __future__ import annotations

import json
from typing import Any, Optional

from lxml import etree
from rdflib import Graph

from reconciliator.loaders.bibframe import (
    BIBFRAMESchemaProfile,
    build_target_record_from_bibframe,
)
from reconciliator.loaders.marcxml import (
    build_target_record_from_marc,
    parse_marc_xml_str_fast,
)
from reconciliator.models import MatchRecord
from reconciliator.normalizers.blocking import (
    compute_author_key,
    compute_title_stem,
    compute_work_blocking_key,
)
from reconciliator.reports import AgentReportRow
from reconciliator.vocab import detect_vocabulary


def make_agent_report_row(
    target_record: Any,
    agent: Any,
    field_tag: str,
    occurrence: int,
    uri: Optional[str] = None,
    action: str = "unmatched",
    is_novel: bool = False,
    discovered_map: Optional[dict[str, Any]] = None,
    accepted_candidates: Optional[list[Any]] = None,
    match_tier: str = "unmatched",
    match_score: float = 0.0,
    dense_score: Optional[float] = None,
    rerank_score: Optional[float] = None,
) -> AgentReportRow:
    """Build an AgentReportRow from target record and matched candidate agent."""
    c_agent = None
    if discovered_map and uri and uri in discovered_map:
        c_agent = discovered_map[uri]
    elif accepted_candidates:
        for cand in accepted_candidates:
            for c_uri in cand.agent_uris:
                if c_uri == uri:
                    c_agent = cand
                    break
            if c_agent:
                break

    auth_id = ""
    auth_vocab = ""
    target_pref_label = ""
    birth_year = None
    death_year = None
    variants = ""
    affils = ""
    subjs = ""
    lang = ""

    if c_agent:
        auth_id = getattr(c_agent, "authority_id", "") or ""
        auth_vocab = getattr(c_agent, "vocabulary", "") or detect_vocabulary(uri or "")
        target_pref_label = getattr(c_agent, "name", "") or ""
        birth_year = getattr(c_agent, "birth_year", None)
        death_year = getattr(c_agent, "death_year", None)
        v_list = getattr(c_agent, "variant_names", []) or []
        variants = " | ".join(v_list) if isinstance(v_list, list) else str(v_list)
        af_list = getattr(c_agent, "affiliations", []) or []
        affils = " | ".join(af_list) if isinstance(af_list, list) else str(af_list)
        s_list = getattr(c_agent, "subjects", []) or []
        subjs = " | ".join(s_list) if isinstance(s_list, list) else str(s_list)
        lang = getattr(c_agent, "language", "") or ""
    elif uri:
        auth_vocab = detect_vocabulary(uri)

    qualifier = " | ".join(agent.qualifiers) if agent.qualifiers else None
    existing_uris = agent.uri_subfield_0 + agent.uri_subfield_1
    existing_uri_str = " | ".join(existing_uris) if existing_uris else ""

    return AgentReportRow(
        target_record_id=target_record.source_id,
        target_title=target_record.title,
        field_tag=field_tag,
        occurrence=occurrence,
        target_raw_name=agent.name,
        target_normalized_name=agent.name_normalized,
        target_role=agent.role,
        target_qualifier=qualifier,
        target_birth_year=agent.birth_year,
        target_death_year=agent.death_year,
        existing_uri=existing_uri_str,
        matched_authority_id=auth_id,
        matched_vocabulary=auth_vocab,
        matched_uri=uri or "",
        matched_pref_label=target_pref_label,
        matched_variant_names=variants,
        matched_birth_year=birth_year,
        matched_death_year=death_year,
        matched_affiliations=affils,
        matched_subjects=subjs,
        matched_language=lang,
        match_tier=match_tier,
        match_score=match_score,
        dense_score=dense_score,
        rerank_score=rerank_score,
        action=action,
        is_novel=is_novel,
    )


def to_target_record(
    rec_fmt: str,
    orig_payload: Any,
    raw_rec: dict[str, Any],
    profile: Optional[BIBFRAMESchemaProfile] = None,
    constituent_profile: Optional[Any] = None,
) -> MatchRecord:
    """Decompose or map raw bibliographic items into normalized MatchRecord instances."""
    try:
        if rec_fmt == "marcxml":
            fast_rec = None
            if isinstance(orig_payload, (bytes, str)):
                try:
                    xml_str = (
                        orig_payload.decode("utf-8")
                        if isinstance(orig_payload, bytes)
                        else orig_payload
                    )
                    parsed = parse_marc_xml_str_fast(xml_str)
                    if parsed:
                        fast_rec = parsed[0]
                except Exception:
                    fast_rec = None
            payload_elem = (
                fast_rec
                if fast_rec is not None
                else (
                    etree.fromstring(orig_payload)
                    if isinstance(orig_payload, (bytes, str))
                    else orig_payload
                )
            )
            return build_target_record_from_marc(payload_elem, source="target")
        elif rec_fmt == "bibframe_rdf":
            g, nodes = orig_payload
            instance_node = nodes[0] if nodes else None
            return build_target_record_from_bibframe(
                g,
                instance_node=instance_node,
                profile=profile,
                source="target",
                constituent_profile=constituent_profile,
            )
        elif rec_fmt == "bibframe_jsonld":
            g = Graph()
            g.parse(data=json.dumps(orig_payload), format="json-ld")
            return build_target_record_from_bibframe(
                g, profile=profile, source="target", constituent_profile=constituent_profile
            )
    except Exception:
        pass

    source_id = raw_rec.get("source_id") or "unknown_target"
    title = raw_rec.get("title") or ""
    agents = raw_rec.get("agents") or []
    author_name = agents[0].get("name") if agents else None
    author_surname = agents[0].get("surname") if agents else None
    author_key = compute_author_key(author_name, author_surname)
    title_stem = compute_title_stem(title)
    work_blocking_key = compute_work_blocking_key(author_key, title_stem)

    if isinstance(orig_payload, (dict, list)):
        raw_bytes = json.dumps(orig_payload, ensure_ascii=False).encode("utf-8")
    elif hasattr(orig_payload, "tag"):
        raw_bytes = etree.tostring(orig_payload, encoding="utf-8")
    else:
        raw_bytes = str(orig_payload).encode("utf-8")

    return MatchRecord(
        record_id=source_id,
        format=rec_fmt,
        work_type="text",
        title=title,
        title_stem=title_stem,
        author_key=author_key,
        work_blocking_key=work_blocking_key,
        raw_payload=raw_bytes,
    )
