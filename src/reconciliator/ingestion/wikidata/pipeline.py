"""Full two-pass streaming Wikidata ingestion pipeline into WEMIRepository."""

from __future__ import annotations

import logging
import multiprocessing as mp
import os
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from sqlalchemy.exc import DataError

from reconciliator.config import WikidataIngestConfig
from reconciliator.embedding.template import WEMISerializationEngine
from reconciliator.ingestion.sanitizer import sanitize_batch_payload
from reconciliator.ingestion.wikidata.authors import _prescan_authors
from reconciliator.ingestion.wikidata.cache import AuthorLRUCache, prefetch_batch_qids
from reconciliator.ingestion.wikidata.decomposition import decompose_wikidata_entity
from reconciliator.ingestion.wikidata.streaming import (
    _chunk_stream_lines,
    _worker_parse_raw_bib_chunk,
    open_dump_stream,
)

if TYPE_CHECKING:
    from reconciliator.database import WEMIRepository
    from reconciliator.progress import ProgressTracker

logger = logging.getLogger(__name__)


def ingest_wikidata_wemi_pipeline(
    config: WikidataIngestConfig,
    wemi_repo: WEMIRepository,
    tracker: Optional[ProgressTracker] = None,
) -> int:
    """Run two-pass streaming Wikidata ingestion landing into WEMIRepository."""
    path = Path(config.dump_path)
    if not path.exists():
        raise FileNotFoundError(f"Wikidata dump not found: {path}")

    total_bytes = os.path.getsize(path)

    # Pass 1: Scan author entities into PostgreSQL wikidata_authors & agents
    logger.info("Pass 1/2: Pre-scanning author entities into database...")
    if tracker:
        tracker.start_task(stage="wikidata_prescan", bytes_total=total_bytes)
    _prescan_authors(path, wemi_repo, tracker, config=config)
    if tracker:
        tracker.complete_task(metadata={"stage": "prescan"})

    if config.pass1_only:
        return 0

    # Pass 2: Stream bibliographic entities and insert into WEMIRepository
    logger.info("Pass 2/2: Decomposing bibliographic entities into WEMI...")
    if tracker:
        tracker.start_task(stage="wikidata_wemi", bytes_total=total_bytes)

    total_ingested = 0
    engine = WEMISerializationEngine()
    works_batch = []
    expressions_batch = []
    manifestations_batch = []
    items_batch = []
    agents_batch = []
    relationships_batch = []
    identifiers_batch = []
    associations_batch = []
    contributions_batch = []

    def _flush_batch() -> None:
        nonlocal total_ingested
        if works_batch:
            task_id = tracker.task_id if tracker and tracker.task_id else "wikidata-wemi"
            payload = {
                "works": works_batch,
                "expressions": expressions_batch,
                "manifestations": manifestations_batch,
                "items": items_batch,
                "agents": agents_batch,
                "relationships": relationships_batch,
                "identifiers": identifiers_batch,
                "manifestation_expressions": associations_batch,
                "contributions": contributions_batch,
            }
            if config and getattr(config, "truncate_overlong_values", True):
                sanitize_batch_payload(
                    payload,
                    config=config,
                    task_id=task_id,
                    tracker=tracker,
                )
            try:
                wemi_repo.insert_wemi_batch(
                    works=works_batch,
                    expressions=expressions_batch,
                    manifestations=manifestations_batch,
                    items=items_batch,
                    agents=agents_batch,
                    relationships=relationships_batch,
                    identifiers=identifiers_batch,
                    manifestation_expressions=associations_batch,
                    contributions=contributions_batch,
                    use_synchronous_commit_off=getattr(config, "use_synchronous_commit_off", True),
                )
            except DataError as err:
                if config and getattr(config, "truncate_overlong_values", True):
                    from reconciliator.logging import log_validation

                    log_validation(
                        logger,
                        action="skip_batch",
                        entity="wikidata_wemi",
                        source_id="wikidata",
                        reason=f"database DataError: {err}",
                        task_id=task_id,
                        tracker=tracker,
                    )
                else:
                    raise
            total_ingested += len(works_batch)
            works_batch.clear()
            expressions_batch.clear()
            manifestations_batch.clear()
            items_batch.clear()
            agents_batch.clear()
            relationships_batch.clear()
            identifiers_batch.clear()
            associations_batch.clear()
            contributions_batch.clear()
            if tracker:
                tracker.update_progress(stage="wikidata_wemi", items_processed=total_ingested)

    author_cache = AuthorLRUCache(maxsize=config.lru_cache_size)
    batch_size = config.batch_size
    throttle_interval = config.progress_throttle_interval
    num_workers = config.workers
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
                    prefetch_batch_qids(candidate_chunk, store=wemi_repo, cache=author_cache)
                    for entity in candidate_chunk:
                        decomp = decompose_wikidata_entity(
                            entity,
                            store=wemi_repo,
                            serialization_engine=engine,
                            cache=author_cache,
                            config=config,
                        )
                        if decomp:
                            w, e, m, itms, agts, rels, idents, assoc, contribs = decomp
                            works_batch.append(w)
                            expressions_batch.append(e)
                            manifestations_batch.append(m)
                            items_batch.extend(itms)
                            agents_batch.extend(agts)
                            relationships_batch.extend(rels)
                            identifiers_batch.extend(idents)
                            associations_batch.extend(assoc)
                            contributions_batch.extend(contribs)

                            if len(works_batch) >= batch_size:
                                _flush_batch()

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
                    prefetch_batch_qids(candidate_chunk, store=wemi_repo, cache=author_cache)
                    for entity in candidate_chunk:
                        decomp = decompose_wikidata_entity(
                            entity,
                            store=wemi_repo,
                            serialization_engine=engine,
                            cache=author_cache,
                            config=config,
                        )
                        if decomp:
                            w, e, m, itms, agts, rels, idents, assoc, contribs = decomp
                            works_batch.append(w)
                            expressions_batch.append(e)
                            manifestations_batch.append(m)
                            items_batch.extend(itms)
                            agents_batch.extend(agts)
                            relationships_batch.extend(rels)
                            identifiers_batch.extend(idents)
                            associations_batch.extend(assoc)
                            contributions_batch.extend(contribs)

                            if len(works_batch) >= batch_size:
                                _flush_batch()

                if tracker and (lines_processed - last_tracked_lines >= throttle_interval):
                    last_tracked_lines = lines_processed
                    curr_bytes = None
                    if hasattr(stream, "fileobj") and hasattr(stream.fileobj, "tell"):
                        try:
                            curr_bytes = int(stream.fileobj.tell())
                        except Exception:
                            pass
                    tracker.update_progress(bytes_processed=curr_bytes, items_increment=0)

        _flush_batch()

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

    if tracker:
        tracker.complete_task(metadata={"total_ingested": total_ingested})

    return total_ingested


def ingest_wikidata_pipeline(
    config: WikidataIngestConfig,
    store: WEMIRepository,
    tracker: Optional[ProgressTracker] = None,
) -> int:
    """Run streaming Wikidata dump ingestion into WEMI repository."""
    return ingest_wikidata_wemi_pipeline(config=config, wemi_repo=store, tracker=tracker)
