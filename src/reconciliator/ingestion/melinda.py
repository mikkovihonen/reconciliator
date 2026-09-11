"""Melinda authority ingestion pipeline coordinating harvesting/file reading, normalization, embedding, and indexing."""

from __future__ import annotations

from typing import Any, Iterator, Optional

from reconciliator.config import MelindaIngestConfig
from reconciliator.embedder import SentenceEmbedder
from reconciliator.harvest.oai_pmh import OAIPMHHarvester, partition_date_range
from reconciliator.loaders.marcxml import iter_marcxml_records, parse_marc_record_elem
from reconciliator.models import BibliographicRecord
from reconciliator.normalizers.record import RecordNormalizer
from reconciliator.progress import ProgressTracker
from reconciliator.repository import AuthorityStore


def stream_melinda_records(
    config: MelindaIngestConfig,
    tracker: Optional[ProgressTracker] = None,
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
        )
        slices = partition_date_range(config.from_date, config.until_date, config.slice_days)

        if len(slices) <= 1 or config.workers <= 1:
            for from_dt, until_dt in slices:
                for elem in harvester.harvest_partition(from_dt, until_dt, tracker=tracker):
                    raw_rec = parse_marc_record_elem(elem, source="melinda")
                    yield raw_rec
        else:
            import queue
            from concurrent.futures import ThreadPoolExecutor

            record_queue: queue.Queue[Any] = queue.Queue(maxsize=config.batch_size * 2)
            sentinel = object()
            worker_error: list[BaseException] = []

            def worker_task(from_dt: Optional[str], until_dt: Optional[str]) -> None:
                try:
                    for elem in harvester.harvest_partition(from_dt, until_dt, tracker=tracker):
                        if worker_error:
                            return
                        raw_rec = parse_marc_record_elem(elem, source="melinda")
                        record_queue.put(raw_rec)
                except Exception as e:
                    worker_error.append(e)

            def dispatch() -> None:
                with ThreadPoolExecutor(max_workers=config.workers) as executor:
                    futures = [executor.submit(worker_task, f, u) for f, u in slices]
                    for fut in futures:
                        fut.result()
                record_queue.put(sentinel)

            # Start background dispatcher thread
            import threading

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
    if tracker:
        tracker.start_task(stage="ingesting_melinda")

    batch: list[BibliographicRecord] = []
    total_ingested = 0

    for raw_rec in stream_melinda_records(config, tracker=tracker):
        record = normalizer.normalize(raw_rec)
        batch.append(record)

        if len(batch) >= config.batch_size:
            vectors = embedder.embed_records(batch)
            store.insert_records_batch(batch, vectors=vectors)
            total_ingested += len(batch)
            batch.clear()

            if tracker:
                tracker.update_progress(
                    stage="indexing",
                    items_processed=total_ingested,
                )

    if batch:
        vectors = embedder.embed_records(batch)
        store.insert_records_batch(batch, vectors=vectors)
        total_ingested += len(batch)
        batch.clear()

    # Save index and record metadata
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
        tracker.complete_task(metadata={"records_ingested": total_ingested})

    return total_ingested
