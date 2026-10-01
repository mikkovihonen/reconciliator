"""Worker process parsing and decomposition of OAI-PMH XML pages."""

from __future__ import annotations

import logging
from typing import Any, Optional

from lxml import etree
from rdflib import Graph

from reconciliator.ingestion.oai.accumulator import WEMIBatchAccumulator
from reconciliator.loaders.base import LoadContext
from reconciliator.loaders.marcxml import extract_marc_source_id
from reconciliator.loaders.registry import load_record

logger = logging.getLogger(__name__)

OAI_NS = {
    "oai": "http://www.openarchives.org/OAI/2.0/",
    "marc": "http://www.loc.gov/MARC21/slim",
}

_worker_vocab_store: Optional[Any] = None
_worker_context: dict[str, Any] = {}


def init_oai_worker(context: dict[str, Any]) -> None:
    """Initialize worker process static context once on process pool startup."""
    global _worker_context
    _worker_context = context
    db_url = context.get("db_url")
    if db_url:
        get_worker_vocab_store(db_url)


def get_worker_vocab_store(db_url: Optional[str]) -> Optional[Any]:
    """Retrieve or lazily initialize the worker's cached VocabStore."""
    global _worker_vocab_store
    if db_url and _worker_vocab_store is None:
        try:
            from reconciliator.vocab import VocabStore

            _worker_vocab_store = VocabStore(db_url)
        except Exception:
            _worker_vocab_store = None
    return _worker_vocab_store


def parse_and_decompose_page(
    page_bytes: bytes,
    source_name: Optional[str] = None,
    id_source: Optional[str] = None,
    include_items: Optional[bool] = None,
    strip_isbd_punctuation: Optional[bool] = None,
    isbd_punctuation: Optional[str] = None,
    database_url: Optional[str] = None,
    skip_ids: Optional[set[str]] = None,
    format: Optional[str] = None,
    constituents: Optional[Any] = None,
    task_id: Optional[str] = None,
    filter_config: Optional[Any] = None,
) -> tuple[dict[str, Any], int, int, int]:
    """Parse raw XML page bytes and decompose MARCXML/BibFrame records in a worker process.

    Returns:
        (batch_payload_dict, decomposed_records_count, skipped_records_count, error_count)
    """
    eff_source_name = source_name or _worker_context.get("source_name", "unknown")
    eff_id_source = id_source or _worker_context.get("id_source", "auto")
    eff_include_items = (
        include_items if include_items is not None else _worker_context.get("include_items", False)
    )
    eff_strip_isbd = (
        strip_isbd_punctuation
        if strip_isbd_punctuation is not None
        else _worker_context.get("strip_isbd_punctuation", True)
    )
    eff_isbd_punct = isbd_punctuation or _worker_context.get("isbd_punctuation", " ./,;:")
    eff_database_url = database_url or _worker_context.get("db_url")
    eff_skip_ids = skip_ids if skip_ids is not None else _worker_context.get("skip_ids")
    eff_format = format or _worker_context.get("format", "marcxml")
    eff_constituents = constituents or _worker_context.get("constituents")
    eff_filter_config = filter_config or _worker_context.get("filter_config")

    if task_id:
        from reconciliator.logging import init_worker_logging

        init_worker_logging(task_id)

    from reconciliator.logging import log_validation

    parser = etree.XMLParser(recover=True, resolve_entities=False)
    error_count = 0
    try:
        root = etree.fromstring(page_bytes, parser=parser)
    except Exception as exc:
        log_validation(
            logger,
            action="skip_page",
            entity="oai_page_xml",
            source_id=eff_source_name,
            reason=f"XMLSyntaxError in OAI page: {exc}",
            task_id=task_id,
        )
        return {}, 0, 0, 1

    records = root.findall(".//oai:record", OAI_NS)
    if not records:
        records = root.findall(".//record")

    vocab_store = get_worker_vocab_store(eff_database_url)

    ctx = LoadContext(
        source=eff_source_name,
        record_origin=eff_source_name,
        id_source=eff_id_source,
        include_items=eff_include_items,
        vocab_store=vocab_store,
        strip_isbd_punctuation=eff_strip_isbd,
        isbd_punctuation=eff_isbd_punct,
        constituents=eff_constituents,
    )

    acc = WEMIBatchAccumulator(
        source_name=eff_source_name,
        id_source=eff_id_source,
        include_items=eff_include_items,
        vocab_store=vocab_store,
        strip_isbd_punctuation=eff_strip_isbd,
        isbd_punctuation=eff_isbd_punct,
        constituents=eff_constituents,
        filter_config=eff_filter_config,
    )

    skipped_count = 0
    decomposed_count = 0

    for rec in records:
        hdr = rec.find("oai:header", OAI_NS)
        if hdr is None:
            hdr = rec.find(".//header")
        if hdr is not None and hdr.get("status") == "deleted":
            continue

        header_id: Optional[str] = None
        if hdr is not None:
            id_el = hdr.find("oai:identifier", OAI_NS)
            if id_el is None:
                id_el = hdr.find(".//identifier")
            if id_el is not None and id_el.text:
                header_id = id_el.text.strip()

        metadata_elem = rec.find("oai:metadata", OAI_NS)
        if metadata_elem is not None and len(metadata_elem) > 0:
            target_elem = metadata_elem[0]
        else:
            marc_rec = rec.find(".//marc:record", OAI_NS)
            target_elem = marc_rec if marc_rec is not None else rec

        if header_id:
            target_elem.set("_oai_identifier", header_id)

        if eff_skip_ids:
            raw_id = extract_marc_source_id(target_elem, id_source=eff_id_source)
            if raw_id and raw_id in eff_skip_ids:
                skipped_count += 1
                continue

        try:
            if eff_format == "bibframe":
                raw_xml = etree.tostring(target_elem, encoding="utf-8")
                g = Graph()
                g.parse(data=raw_xml.decode("utf-8"), format="xml")
                item_to_load = g
            else:
                item_to_load = target_elem

            rec_decomp = load_record(item_to_load, ctx)
            if acc.add_record(rec_decomp):
                decomposed_count += 1
            else:
                skipped_count += 1
        except Exception as exc:
            error_count += 1
            log_validation(
                logger,
                action="skip_record",
                entity="oai_record",
                source_id=header_id or "unknown",
                reason=f"Failed to decompose record: {exc}",
                task_id=task_id,
            )

    return acc.take(), decomposed_count, skipped_count, error_count
