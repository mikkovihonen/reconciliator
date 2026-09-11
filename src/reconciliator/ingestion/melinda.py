"""Melinda authority ingestion pipeline coordinating harvesting/file reading, normalization, embedding, and indexing."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any, Iterator, Optional

from reconciliator.config import MelindaIngestConfig
from reconciliator.embedder import SentenceEmbedder
from reconciliator.harvest.oai_pmh import OAIPMHHarvester, partition_date_range
from reconciliator.loaders.marcxml import iter_marcxml_records, parse_marc_record_elem
from reconciliator.models import BibliographicRecord
from reconciliator.normalizers.record import RecordNormalizer
from reconciliator.progress import ProgressTracker
from reconciliator.repository import AuthorityStore

logger = logging.getLogger(__name__)


def stream_melinda_records(
    config: MelindaIngestConfig,
    tracker: Optional[ProgressTracker] = None,
    on_record_harvested: Optional[Callable[[], None]] = None,
) -> Iterator[dict[str, Any]]:
    """Yield raw record dictionaries from either local file or OAI-PMH harvest."""
    if config.source_type == "file":
        if not config.input_path:
            raise ValueError("input_path must be specified when source_type is 'file'")
        for raw_rec in iter_marcxml_records(config.input_path, source="melinda"):
            yield raw_rec
    else:
        harvester = OAIPMHHarvester(
            endpoint_url=config.oai_url or "https://oai-pmh.api.melinda.kansalliskirjasto.fi/bib",
            metadata_prefix=config.metadata_prefix,
            set_spec=config.set_spec,
            request_delay=config.request_delay,
        )

        from_date = config.from_date
        until_date = config.until_date

        if config.workers > 1 and (not from_date or not until_date):
            if not until_date:
                until_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            if not from_date:
                discovered = harvester.get_earliest_datestamp()
                from_date = discovered or "2000-01-01"
                logger.info(
                    "Auto-discovered date range for parallel harvesting (%d workers): %s to %s",
                    config.workers,
                    from_date,
                    until_date,
                )

        slices = partition_date_range(from_date, until_date, config.slice_days)

        if len(slices) <= 1 or config.workers <= 1:
            for from_dt, until_dt in slices:
                for elem in harvester.harvest_partition(
                    from_dt,
                    until_dt,
                    tracker=tracker if on_record_harvested is None else None,
                    on_record_harvested=on_record_harvested,
                ):
                    raw_rec = parse_marc_record_elem(elem, source="melinda")
                    yield raw_rec
        else:
            import queue
            from concurrent.futures import ThreadPoolExecutor

            queue_size = max(config.batch_size * config.workers * 2, 2000)
            record_queue: queue.Queue[Any] = queue.Queue(maxsize=queue_size)
            sentinel = object()
            worker_error: list[BaseException] = []

            def worker_task(from_dt: Optional[str], until_dt: Optional[str]) -> None:
                try:
                    for elem in harvester.harvest_partition(
                        from_dt,
                        until_dt,
                        tracker=None,
                        on_record_harvested=on_record_harvested,
                    ):
                        if worker_error:
                            return
                        raw_rec = parse_marc_record_elem(elem, source="melinda")
                        record_queue.put(raw_rec)
                except Exception as e:
                    logger.error("Error harvesting partition %s to %s: %s", from_dt, until_dt, e)
                    worker_error.append(e)

            def dispatch() -> None:
                try:
                    with ThreadPoolExecutor(max_workers=config.workers) as executor:
                        futures = [executor.submit(worker_task, f, u) for f, u in slices]
                        for fut in futures:
                            fut.result()
                except Exception as exc:
                    worker_error.append(exc)
                finally:
                    record_queue.put(sentinel)

            # Start background dispatcher thread
            dispatch_thread = threading.Thread(target=dispatch, daemon=True)
            dispatch_thread.start()

            while True:
                if worker_error:
                    raise worker_error[0]
                try:
                    item = record_queue.get(timeout=0.1)
                    if item is sentinel:
                        break
                    yield item
                    record_queue.task_done()
                except queue.Empty:
                    if not dispatch_thread.is_alive() and record_queue.empty():
                        break
                    continue


def ingest_melinda_pipeline(
    config: MelindaIngestConfig,
    store: AuthorityStore,
    normalizer: RecordNormalizer,
    embedder: SentenceEmbedder,
    tracker: Optional[ProgressTracker] = None,
) -> int:
    """Run full Melinda authority ingestion, embedding, and storage indexing."""
    is_oai = config.source_type != "file"
    stage_name = "harvest_and_index" if is_oai else "indexing"

    harvested_count = 0
    harvested_lock = threading.Lock()
    total_ingested = 0

    def on_record_harvested() -> None:
        nonlocal harvested_count
        with harvested_lock:
            harvested_count += 1
            cur_h = harvested_count
        # Periodically emit initial heartbeat if 0 records indexed yet
        if tracker and cur_h % 200 == 0:
            tracker.update_progress(
                stage=stage_name,
                metadata={"harvested": cur_h, "indexed": total_ingested},
            )

    if tracker:
        tracker.start_task(
            stage=stage_name,
            metadata={"harvested": 0, "indexed": 0} if is_oai else None,
        )

    batch: list[BibliographicRecord] = []
    last_checkpoint = 0
    checkpoint_interval = 5000

    try:
        for raw_rec in stream_melinda_records(
            config,
            tracker=tracker,
            on_record_harvested=on_record_harvested if is_oai else None,
        ):
            record = normalizer.normalize(raw_rec)
            batch.append(record)

            if len(batch) >= config.batch_size:
                vectors = embedder.embed_records(batch)
                store.insert_records_batch(batch, vectors=vectors)
                total_ingested += len(batch)
                batch.clear()

                if total_ingested - last_checkpoint >= checkpoint_interval:
                    store.save_index(
                        metadata={
                            "model_name": embedder.config.model_name,
                            "dimension": embedder.dimension,
                            "template_hash": embedder.template_hash,
                        }
                    )
                    last_checkpoint = total_ingested

                if tracker:
                    meta = (
                        {"harvested": harvested_count, "indexed": total_ingested}
                        if is_oai
                        else None
                    )
                    tracker.update_progress(
                        stage=stage_name,
                        items_processed=total_ingested,
                        metadata=meta,
                    )

        if batch:
            vectors = embedder.embed_records(batch)
            store.insert_records_batch(batch, vectors=vectors)
            total_ingested += len(batch)
            batch.clear()
            if tracker and is_oai:
                tracker.update_progress(
                    stage=stage_name,
                    items_processed=total_ingested,
                    metadata={"harvested": harvested_count, "indexed": total_ingested},
                )
    finally:
        # Always persist FAISS index and metadata even if interrupted or cancelled
        store.save_index(
            metadata={
                "model_name": embedder.config.model_name,
                "dimension": embedder.dimension,
                "template_hash": embedder.template_hash,
            }
        )
        store.set_embedding_metadata(
            model_name=embedder.config.model_name,
            dimension=embedder.dimension,
            template_hash=embedder.template_hash,
        )

    if tracker:
        meta = (
            {"harvested": harvested_count, "indexed": total_ingested}
            if is_oai
            else {"records_ingested": total_ingested}
        )
        tracker.complete_task(metadata=meta)

    return total_ingested
