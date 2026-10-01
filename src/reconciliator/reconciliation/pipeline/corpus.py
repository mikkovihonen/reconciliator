"""Corpus staging and WEMI persistence for reconciliation pipelines."""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

from lxml import etree
from rdflib import Graph

from reconciliator.config import ReconcileConfig
from reconciliator.constituents import get_constituent_profile
from reconciliator.ingestion.sanitizer import sanitize_batch_payload
from reconciliator.loaders.aggregates import build_embodied_constituents
from reconciliator.loaders.bibframe import (
    BIBFRAMESchemaProfile,
    bibframe_entity_keys,
    decompose_bibframe_work,
)
from reconciliator.loaders.marcxml import (
    decompose_marc_wemi,
    extract_marc_constituents,
    parse_marc_xml_str_fast,
)

logger = logging.getLogger(__name__)


def ingest_corpus_as_wemi(
    repo: Any,
    records: Any,
    source_name: str,
    profile: Optional[BIBFRAMESchemaProfile] = None,
    strip_isbd_punctuation: bool = True,
    isbd_punctuation: str = " ./,;:",
    batch_size: int = 500,
    config: Optional[ReconcileConfig] = None,
    tracker: Optional[Any] = None,
) -> int:
    """Persist the corpus being reconciled as ordinary WEMI rows."""
    total = 0
    batch: dict[str, list[Any]] = {}

    const_cfg = config.constituents if config else None
    const_mode = const_cfg.mode if const_cfg else "entities"
    marc_const_prof = None
    if const_cfg and const_mode != "off":
        try:
            marc_const_prof = get_constituent_profile(const_cfg.profile or "marc_enhanced")
        except Exception as e:
            logger.warning("Could not load MARC constituent profile: %s", e)

    bf_const_prof = None
    if const_cfg and const_mode != "off":
        try:
            if profile and profile.name == "bffi_1":
                bf_const_prof = get_constituent_profile(const_cfg.bffi_profile or "bffi_haspart")
            else:
                bf_const_prof = get_constituent_profile(
                    const_cfg.bibframe_profile or "bibframe_haspart"
                )
        except Exception as e:
            logger.warning("Could not load BIBFRAME constituent profile: %s", e)

    def _flush() -> None:
        if not batch.get("works"):
            return

        task_id = getattr(tracker, "task_id", "") or f"corpus-ingest-{source_name}"
        if config is not None and config.truncate_overlong_values:
            batch["record_origin"] = source_name
            sanitize_batch_payload(batch, config=config, task_id=task_id, tracker=tracker)

        try:
            repo.insert_wemi_batch(
                works=batch.get("works") or [],
                expressions=batch.get("expressions") or [],
                manifestations=batch.get("manifestations") or [],
                agents=batch.get("agents") or [],
                relationships=batch.get("relationships") or [],
                identifiers=batch.get("identifiers") or [],
                manifestation_expressions=batch.get("associations") or [],
                contributions=batch.get("contributions") or [],
                entity_keys=batch.get("entity_keys") or [],
                entity_subjects=batch.get("entity_subjects") or [],
                record_origin=source_name,
            )
        except Exception as exc:
            from sqlalchemy.exc import DataError

            if (
                isinstance(exc, DataError)
                and config is not None
                and config.truncate_overlong_values
            ):
                from reconciliator.logging import log_validation

                log_validation(
                    logger,
                    action="skip_batch",
                    entity="corpus_wemi_batch",
                    source_id=source_name,
                    reason=f"database DataError: {exc}",
                    task_id=task_id,
                    tracker=tracker,
                )
                batch.clear()
                return
            raise

        batch.clear()

    def _stage(key: str, values: Any) -> None:
        batch.setdefault(key, []).extend(values)

    for rec_fmt, orig_payload, _raw_rec, _offset in records:
        keys: list[Any] = []
        subjs: list[Any] = []
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
                w, e, m, itms, agts, rels, idents, assoc, contribs = decompose_marc_wemi(
                    payload_elem,
                    source=source_name,
                    record_origin=source_name,
                    strip_isbd_punctuation=strip_isbd_punctuation,
                    isbd_punctuation=isbd_punctuation,
                    key_sink=keys,
                    subject_sink=subjs,
                )
                if const_mode == "entities" and marc_const_prof:
                    consts = extract_marc_constituents(payload_elem, marc_const_prof)
                    if consts:
                        c_batch = build_embodied_constituents(
                            container_work=w,
                            container_expression=e,
                            container_manifestation=m,
                            constituents=consts,
                            source=source_name,
                            record_origin=source_name,
                            language=e.language,
                            content_type=e.content_type,
                            work_type=w.work_type,
                            container_creator=w.title_normalized or None,
                            relationship_type="has_part",
                        )
                        _stage("works", c_batch.works)
                        _stage("expressions", c_batch.expressions)
                        _stage("associations", c_batch.associations)
                        _stage("relationships", c_batch.relationships)
            elif rec_fmt in ("bibframe_rdf", "bibframe_jsonld"):
                if rec_fmt == "bibframe_rdf":
                    g, nodes = orig_payload
                    node = nodes[0] if nodes else None
                else:
                    g = Graph()
                    g.parse(data=json.dumps(orig_payload), format="json-ld")
                    node = None
                constituent_sink: list[Any] = []
                w, e, m, itms, agts, rels, idents, assoc, contribs = decompose_bibframe_work(
                    g=g,
                    instance_node=node,
                    profile=profile,
                    source=source_name,
                    subject_sink=subjs,
                    constituent_profile=bf_const_prof if const_mode != "off" else None,
                    constituent_sink=constituent_sink if const_mode != "off" else None,
                )
                keys = bibframe_entity_keys(m, w, agts, idents, constituents=constituent_sink)
                if const_mode == "entities" and constituent_sink:
                    c_batch = build_embodied_constituents(
                        container_work=w,
                        container_expression=e,
                        container_manifestation=m,
                        constituents=constituent_sink,
                        source=source_name,
                        record_origin=source_name,
                        language=e.language,
                        content_type=e.content_type,
                        work_type=w.work_type,
                        container_creator=w.title_normalized or None,
                        relationship_type="has_part",
                    )
                    _stage("works", c_batch.works)
                    _stage("expressions", c_batch.expressions)
                    _stage("associations", c_batch.associations)
                    _stage("relationships", c_batch.relationships)
            else:
                continue
        except Exception:
            continue

        _stage("works", [w])
        _stage("expressions", [e])
        _stage("manifestations", [m])
        _stage("items", itms)
        _stage("agents", agts)
        _stage("relationships", rels)
        _stage("identifiers", idents)
        _stage("associations", assoc)
        _stage("contributions", contribs)
        _stage("entity_keys", keys)
        _stage("entity_subjects", subjs)
        total += 1
        if total % batch_size == 0:
            _flush()

    _flush()
    return total
