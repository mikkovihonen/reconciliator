"""Wikidata author extraction and Pass 1 pre-scanning into database."""

from __future__ import annotations

import logging
import multiprocessing as mp
import os
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

import orjson
from sqlalchemy.exc import DataError

from reconciliator.config import (
    WikidataIngestConfig,
    get_wikidata_author_properties,
)
from reconciliator.database.schema import agents_table
from reconciliator.ingestion.sanitizer import sanitize_entities
from reconciliator.ingestion.wikidata.claims import (
    _HUMAN_AND_ENTITY_TYPES,
    WIKIDATA_PREF_LANGS,
    get_claim_qids,
    get_claim_snak_value,
)

if TYPE_CHECKING:
    from reconciliator.database import WEMIRepository
    from reconciliator.progress import ProgressTracker

logger = logging.getLogger(__name__)


def _extract_author_data(
    entity: dict[str, Any],
    require_authority_id: bool = True,
    authority_properties: tuple[str, ...] | list[str] = (
        "P3595",
        "P213",
        "P214",
        "P227",
        "P244",
        "P906",
        "P496",
    ),
) -> Optional[dict[str, Any]]:
    """Extract author/entity metadata from a Wikidata entity if it matches criteria.

    Returns a dict suitable for insert_wikidata_authors, or None.
    """
    qid = entity.get("id", "")
    claims = entity.get("claims", {})
    labels = entity.get("labels", {})

    instance_of = get_claim_qids(claims, "P31")
    is_human = "Q5" in instance_of
    is_org_or_place = any(k in instance_of for k in _HUMAN_AND_ENTITY_TYPES - {"Q5"})
    auth_props = authority_properties or (
        "P3595",
        "P213",
        "P214",
        "P227",
        "P244",
        "P906",
        "P496",
    )
    has_auth_ids = any(prop in claims for prop in auth_props)

    if require_authority_id and is_human:
        if not has_auth_ids:
            return None
    elif not (is_human or is_org_or_place or has_auth_ids):
        return None

    name_val = ""
    for lang in WIKIDATA_PREF_LANGS:
        if lang in labels:
            name_val = labels[lang].get("value", "")
            if name_val:
                break
    if not name_val and labels:
        first_label = next(iter(labels.values()), None)
        if isinstance(first_label, dict):
            name_val = first_label.get("value", "")
        elif isinstance(first_label, str):
            name_val = first_label

    fam_name = ""
    fam_qids = get_claim_qids(claims, "P734")
    if fam_qids:
        fam_name = fam_qids[0]

    author_props = get_wikidata_author_properties()
    identifiers: dict[str, str] = {}
    for scheme, prop in author_props.items():
        val = get_claim_snak_value(claims, prop)
        if val is not None:
            val_str = str(val).strip()
            if val_str:
                identifiers[scheme] = val_str

    # Extract birth / death years
    birth_year: Optional[int] = None
    b_val = get_claim_snak_value(claims, "P569")
    if isinstance(b_val, dict) and "time" in b_val:
        m_b = re.search(r"([+-]?\d{4})", str(b_val["time"]))
        if m_b:
            birth_year = int(m_b.group(1))

    death_year: Optional[int] = None
    d_val = get_claim_snak_value(claims, "P570")
    if isinstance(d_val, dict) and "time" in d_val:
        m_d = re.search(r"([+-]?\d{4})", str(d_val["time"]))
        if m_d:
            death_year = int(m_d.group(1))

    occupations = get_claim_qids(claims, "P106")
    fields_of_work = get_claim_qids(claims, "P101")
    affiliations = get_claim_qids(claims, "P108") + get_claim_qids(claims, "P1416")

    # Extract aliases / variant names
    aliases_list: list[str] = []
    aliases_dict = entity.get("aliases", {})
    if isinstance(aliases_dict, dict):
        for lang in WIKIDATA_PREF_LANGS:
            if lang in aliases_dict:
                for a_item in aliases_dict[lang]:
                    val = a_item.get("value") if isinstance(a_item, dict) else None
                    if val and val not in aliases_list:
                        aliases_list.append(val)

    # Extract descriptions (qualifier)
    desc_val: Optional[str] = None
    descriptions = entity.get("descriptions", {})
    if isinstance(descriptions, dict):
        for lang in WIKIDATA_PREF_LANGS:
            if lang in descriptions:
                d_item = descriptions[lang]
                if isinstance(d_item, dict) and d_item.get("value"):
                    desc_val = d_item["value"]
                    break

    # Extract qualifiers: descriptions
    qualifiers: list[str] = []
    if desc_val:
        qualifiers.append(desc_val)

    if name_val or has_auth_ids or identifiers:
        return {
            "qid": qid,
            "name": name_val,
            "family_name": fam_name,
            "surname": fam_name,
            "identifiers": identifiers,
            "birth_year": birth_year,
            "death_year": death_year,
            "occupations": occupations,
            "fields_of_work": fields_of_work,
            "affiliations": affiliations,
            "qualifiers": qualifiers,
            "variant_names": aliases_list,
        }
    return None


def _worker_parse_authors_chunk(
    chunk_data: tuple[list[bytes], bool, tuple[str, ...]],
) -> list[dict[str, Any]]:
    """Worker process function: parses a chunk of raw JSON lines for author entities."""
    from reconciliator.ingestion.wikidata.streaming import _has_author_indicator

    lines, require_auth_id, auth_props = chunk_data
    batch: list[dict[str, Any]] = []
    for line in lines:
        if not _has_author_indicator(line):
            continue
        clean = line.strip().rstrip(b",")
        if not clean or clean in (b"[", b"]"):
            continue
        try:
            ent = orjson.loads(clean)
        except Exception:
            continue
        data = _extract_author_data(
            ent,
            require_authority_id=require_auth_id,
            authority_properties=auth_props,
        )
        if data:
            batch.append(data)
    return batch


def _prescan_authors(
    path: Path,
    store: Optional[WEMIRepository],
    tracker: Optional[ProgressTracker],
    config: Optional[WikidataIngestConfig] = None,
) -> int:
    """Pass 1: Stream the Wikidata dump and persist all author/org/place entities to PostgreSQL.

    Processes line-by-line or in chunks via multiprocess worker pool with O(1) memory.
    Returns the number of author entities persisted.
    """
    from reconciliator.ingestion.wikidata.streaming import (
        _chunk_stream_lines,
        open_dump_stream,
    )

    author_batch: list[dict[str, Any]] = []
    total_authors = 0
    require_auth_id = True if config is None else config.require_author_authority_id
    auth_props = (
        ("P3595", "P213", "P214", "P227", "P244", "P906", "P496")
        if config is None
        else tuple(config.author_authority_properties)
    )
    batch_size = config.batch_size if config else 5000
    sync_off = config.use_synchronous_commit_off if config else True
    throttle_interval = config.progress_throttle_interval if config else 32768
    num_workers = config.workers if config else 0
    if num_workers <= 0:
        num_workers = max(1, min(8, (os.cpu_count() or 2) - 1))

    def _flush_author_batch(batch: list[dict[str, Any]]) -> None:
        if not batch or store is None:
            return
        task_id = tracker.task_id if tracker and tracker.task_id else "wikidata-prescan"
        if config and getattr(config, "truncate_overlong_values", True):
            sanitize_entities(
                batch,
                agents_table,
                config=config,
                task_id=task_id,
                tracker=tracker,
                record_origin="wikidata",
            )
            for a in batch:
                if "surname" in a and "family_name" in a and a["family_name"] != a["surname"]:
                    a["family_name"] = a["surname"]
        try:
            store.insert_wikidata_authors(batch, use_synchronous_commit_off=sync_off)
        except DataError as err:
            if config and getattr(config, "truncate_overlong_values", True):
                from reconciliator.logging import log_validation

                log_validation(
                    logger,
                    action="skip_batch",
                    entity="wikidata_authors",
                    source_id="wikidata",
                    reason=f"database DataError: {err}",
                    task_id=task_id,
                    tracker=tracker,
                )
            else:
                raise

    stream, proc = open_dump_stream(path, config=config)
    lines_processed = 0
    last_tracked_lines = 0

    try:
        if num_workers > 1:
            with mp.Pool(processes=num_workers) as pool:
                chunk_gen = (
                    (chunk, require_auth_id, auth_props)
                    for chunk in _chunk_stream_lines(stream, chunk_size=batch_size)
                )
                for parsed_chunk in pool.imap_unordered(
                    _worker_parse_authors_chunk, chunk_gen, chunksize=1
                ):
                    lines_processed += batch_size
                    if parsed_chunk:
                        author_batch.extend(parsed_chunk)
                        if len(author_batch) >= batch_size:
                            _flush_author_batch(author_batch)
                            total_authors += len(author_batch)
                            author_batch.clear()
                            if tracker:
                                tracker.update_progress(
                                    stage="wikidata_prescan", items_processed=total_authors
                                )

                    if tracker and (lines_processed - last_tracked_lines >= throttle_interval):
                        last_tracked_lines = lines_processed
                        curr_bytes = None
                        if hasattr(stream, "fileobj") and hasattr(stream.fileobj, "tell"):
                            try:
                                curr_bytes = int(stream.fileobj.tell())
                            except Exception:
                                pass
                        tracker.update_progress(
                            stage="wikidata_prescan",
                            bytes_processed=curr_bytes,
                            items_processed=total_authors,
                        )
        else:
            for chunk in _chunk_stream_lines(stream, chunk_size=batch_size):
                lines_processed += len(chunk)
                parsed_chunk = _worker_parse_authors_chunk((chunk, require_auth_id, auth_props))
                if parsed_chunk:
                    author_batch.extend(parsed_chunk)
                    if len(author_batch) >= batch_size:
                        _flush_author_batch(author_batch)
                        total_authors += len(author_batch)
                        author_batch.clear()
                        if tracker:
                            tracker.update_progress(
                                stage="wikidata_prescan", items_processed=total_authors
                            )

                if tracker and (lines_processed - last_tracked_lines >= throttle_interval):
                    last_tracked_lines = lines_processed
                    curr_bytes = None
                    if hasattr(stream, "fileobj") and hasattr(stream.fileobj, "tell"):
                        try:
                            curr_bytes = int(stream.fileobj.tell())
                        except Exception:
                            pass
                    tracker.update_progress(
                        stage="wikidata_prescan",
                        bytes_processed=curr_bytes,
                        items_processed=total_authors,
                    )

        # Flush remaining batch
        if author_batch:
            _flush_author_batch(author_batch)
            total_authors += len(author_batch)
            author_batch.clear()
            if tracker:
                tracker.update_progress(stage="wikidata_prescan", items_processed=total_authors)

    finally:
        if hasattr(stream, "close"):
            try:
                stream.close()
            except Exception:
                pass
        if proc is not None:
            try:
                proc.terminate()
                proc.wait(timeout=2.0)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

    logger.info("Pass 1 complete: %d author entities persisted to database", total_authors)
    return total_authors
