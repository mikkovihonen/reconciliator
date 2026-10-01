"""Streaming gzipped dump reader, parallel chunking, and two-pass dump iterators."""

from __future__ import annotations

import gzip
import logging
import multiprocessing as mp
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterator, Optional

import orjson

from reconciliator.config import WikidataIngestConfig
from reconciliator.ingestion.wikidata.cache import AuthorLRUCache, prefetch_batch_qids
from reconciliator.ingestion.wikidata.claims import (
    _BOOK_TYPES,
    _HUMAN_AND_ENTITY_TYPES,
    get_claim_qids,
)

if TYPE_CHECKING:
    from reconciliator.database import WEMIRepository
    from reconciliator.progress import ProgressTracker

logger = logging.getLogger(__name__)

BIB_INDICATORS = (
    b'"P212"',
    b'"P957"',
    b'"P3595"',
    b'"P213"',
    b'"Q571"',
    b'"Q7725634"',
    b'"Q47461344"',
    b'"Q2085381"',
    b'"P1476"',
)

AUTHOR_INDICATORS = (
    b'"Q5"',
    b'"Q170574"',
    b'"Q2085381"',
    b'"Q4830453"',
    b'"Q43229"',
    b'"Q515"',
    b'"Q6256"',
    b'"P3595"',
    b'"P213"',
    b'"P214"',
    b'"P227"',
    b'"P244"',
    b'"P906"',
    b'"P496"',
    b'"P50"',
    b'"P98"',
    b'"P86"',
)

BIB_INDICATORS_RE = re.compile(b'"(?:P212|P957|P3595|P213|Q571|Q7725634|Q47461344|Q2085381|P1476)"')

AUTHOR_INDICATORS_RE = re.compile(
    b'"(?:Q5|Q170574|Q2085381|Q4830453|Q43229|Q515|Q6256|P3595|P213|P214|P227|P244|P906|P496|P98|P86)"'
)


def find_pigz_binary(custom_path: Optional[str] = None) -> Optional[str]:
    """Find pigz binary on system or custom path."""
    if custom_path:
        if Path(custom_path).is_file() and os.access(custom_path, os.X_OK):
            return str(custom_path)
        return None
    which_path = shutil.which("pigz")
    if which_path:
        return which_path
    for candidate in ("/opt/homebrew/bin/pigz", "/usr/local/bin/pigz", "/usr/bin/pigz"):
        if Path(candidate).is_file() and os.access(candidate, os.X_OK):
            return candidate
    return None


def open_dump_stream(
    path: Path | str,
    config: Optional[WikidataIngestConfig] = None,
) -> tuple[Any, Optional[subprocess.Popen]]:
    """Open gzipped dump via pigz, gzip -dc subprocess, or gzip.open fallback."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Wikidata dump not found: {p}")

    use_pigz = True if config is None else config.use_pigz
    pigz_path = None if config is None else config.pigz_path
    pigz_threads = 0 if config is None else config.pigz_threads

    if use_pigz:
        pigz_bin = find_pigz_binary(pigz_path)
        if pigz_bin:
            threads = pigz_threads if pigz_threads > 0 else max(1, (os.cpu_count() or 2) // 2)
            cmd = [pigz_bin, "-dc", "-p", str(threads), str(p)]
            try:
                proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    bufsize=16 * 1024 * 1024,
                )
                if proc.stdout:
                    logger.info(
                        "Decompressing %s via pigz (%s) with %d threads",
                        p.name,
                        pigz_bin,
                        threads,
                    )
                    return proc.stdout, proc
            except Exception as e:
                logger.warning("Failed to launch pigz subprocess (%s): %s", pigz_bin, e)

        # Fallback to standard gzip -dc subprocess
        gzip_bin = shutil.which("gzip") or "/usr/bin/gzip"
        if Path(gzip_bin).is_file() and os.access(gzip_bin, os.X_OK):
            try:
                proc = subprocess.Popen(
                    [gzip_bin, "-dc", str(p)],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    bufsize=16 * 1024 * 1024,
                )
                if proc.stdout:
                    logger.info(
                        "Decompressing %s via %s -dc (install pigz for multi-threaded decompression)",
                        p.name,
                        gzip_bin,
                    )
                    return proc.stdout, proc
            except Exception as e:
                logger.debug("Failed to launch gzip -dc subprocess: %s", e)

    # Standard library gzip fallback
    logger.info("Decompressing %s via standard library gzip.open", p.name)
    return gzip.open(p, "rb"), None


def _chunk_stream_lines(stream: Any, chunk_size: int = 5000) -> Iterator[list[bytes]]:
    """Yield buffered chunks of raw lines from a decompressed stream."""
    chunk: list[bytes] = []
    for line in stream:
        chunk.append(line)
        if len(chunk) >= chunk_size:
            yield chunk
            chunk = []
    if chunk:
        yield chunk


def _has_bib_indicator(line: bytes) -> bool:
    """Fast byte-level pre-filter for lines likely containing bibliographic entities."""
    return bool(BIB_INDICATORS_RE.search(line))


def _has_author_indicator(line: bytes) -> bool:
    """Fast byte-level pre-filter for lines likely containing author, contributor, or agent entities."""
    return bool(AUTHOR_INDICATORS_RE.search(line))


def _worker_parse_raw_bib_chunk(
    lines: list[bytes],
) -> list[dict[str, Any]]:
    """Worker process function: parses a chunk of raw JSON lines for candidate bibliographic entities."""
    batch: list[dict[str, Any]] = []
    for line in lines:
        if not _has_bib_indicator(line):
            continue
        clean = line.strip().rstrip(b",")
        if not clean or clean in (b"[", b"]"):
            continue
        try:
            ent = orjson.loads(clean)
        except Exception:
            continue
        claims = ent.get("claims", {})
        instance_of = get_claim_qids(claims, "P31")
        is_human = "Q5" in instance_of
        is_org_or_place = any(k in instance_of for k in _HUMAN_AND_ENTITY_TYPES - {"Q5"})
        if is_human or is_org_or_place:
            continue
        is_book = any(k in instance_of for k in _BOOK_TYPES)
        has_bib_ids = "P212" in claims or "P957" in claims or "P3595" in claims
        has_p629 = bool(get_claim_qids(claims, "P629"))
        is_edition = has_p629 or "Q3331189" in instance_of
        is_creative_work = is_book or any(
            k in instance_of
            for k in {
                "Q7725634",
                "Q571",
                "Q47461344",
                "Q2188189",
                "Q11424",
                "Q40063",
                "Q1004",
                "Q7889",
                "Q131436",
            }
        )
        if is_edition or is_creative_work or has_bib_ids:
            batch.append(ent)
    return batch


def _stream_bibliographic_entities(
    path: Path,
    store: Optional[WEMIRepository],
    tracker: Optional[ProgressTracker],
    config: Optional[WikidataIngestConfig] = None,
    cache: Optional[AuthorLRUCache] = None,
) -> Iterator[dict[str, Any]]:
    """Pass 2: Stream the Wikidata dump and yield bibliographic entities with LRU cache."""
    from reconciliator.ingestion.wikidata.decomposition import parse_wikidata_entity

    batch_size = config.batch_size if config else 5000
    author_cache = cache or AuthorLRUCache(maxsize=config.lru_cache_size if config else 500000)
    throttle_interval = config.progress_throttle_interval if config else 32768
    num_workers = config.workers if config else 0
    if num_workers <= 0:
        num_workers = max(1, min(8, (os.cpu_count() or 2) - 1))

    stream, proc = open_dump_stream(path, config=config)
    lines_processed = 0
    last_tracked_lines = 0

    try:
        if num_workers > 1:
            with mp.Pool(processes=num_workers) as pool:
                chunk_gen = _chunk_stream_lines(stream, chunk_size=batch_size)
                for candidate_chunk in pool.imap_unordered(
                    _worker_parse_raw_bib_chunk, chunk_gen, chunksize=1
                ):
                    lines_processed += batch_size
                    if not candidate_chunk:
                        continue
                    prefetch_batch_qids(candidate_chunk, store=store, cache=author_cache)
                    for entity in candidate_chunk:
                        parsed = parse_wikidata_entity(entity, store=store, cache=author_cache)
                        if parsed:
                            yield parsed

                    if tracker and (lines_processed - last_tracked_lines >= throttle_interval):
                        last_tracked_lines = lines_processed
                        curr_bytes = None
                        if hasattr(stream, "fileobj") and hasattr(stream.fileobj, "tell"):
                            try:
                                curr_bytes = int(stream.fileobj.tell())
                            except Exception:
                                pass
                        tracker.update_progress(bytes_processed=curr_bytes, items_increment=0)
        else:
            for chunk in _chunk_stream_lines(stream, chunk_size=batch_size):
                lines_processed += len(chunk)
                candidate_chunk = _worker_parse_raw_bib_chunk(chunk)
                if candidate_chunk:
                    prefetch_batch_qids(candidate_chunk, store=store, cache=author_cache)
                    for entity in candidate_chunk:
                        parsed = parse_wikidata_entity(entity, store=store, cache=author_cache)
                        if parsed:
                            yield parsed

                if tracker and (lines_processed - last_tracked_lines >= throttle_interval):
                    last_tracked_lines = lines_processed
                    curr_bytes = None
                    if hasattr(stream, "fileobj") and hasattr(stream.fileobj, "tell"):
                        try:
                            curr_bytes = int(stream.fileobj.tell())
                        except Exception:
                            pass
                    tracker.update_progress(bytes_processed=curr_bytes, items_increment=0)
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


def stream_wikidata_dump(
    dump_path: Path | str,
    store: Optional[WEMIRepository] = None,
    tracker: Optional[ProgressTracker] = None,
    config: Optional[WikidataIngestConfig] = None,
) -> Iterator[dict[str, Any]]:
    """Two-pass Wikidata streaming: pre-scan authors into PostgreSQL, then extract books.

    Pass 1: Scans the dump to collect all human/organization/place entities
            and persists them to the PostgreSQL agents table.
    Pass 2: Scans the dump to process bibliographic entities, hydrating
            author/publisher/subject names from the database with LRU cache.

    This achieves true O(1) memory consumption regardless of dump size.
    """
    from reconciliator.ingestion.wikidata.authors import _prescan_authors

    path = Path(dump_path)
    if not path.exists():
        raise FileNotFoundError(f"Wikidata dump not found: {path}")

    total_bytes = os.path.getsize(path)

    # Pass 1: Pre-scan all human/org entities into agents
    logger.info("Pass 1/2: Pre-scanning author entities into database...")
    if tracker:
        tracker.start_task(stage="wikidata_prescan", bytes_total=total_bytes)
    _prescan_authors(path, store, tracker, config=config)
    if tracker:
        tracker.complete_task(metadata={"stage": "prescan"})

    # Pass 2: Stream bibliographic entities, hydrating authors from database
    logger.info("Pass 2/2: Streaming bibliographic entities...")
    if tracker:
        tracker.start_task(stage="streaming_wikidata", bytes_total=total_bytes)
    yield from _stream_bibliographic_entities(path, store, tracker, config=config)
