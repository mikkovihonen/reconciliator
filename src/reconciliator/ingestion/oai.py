"""Generic OAI-PMH and MARCXML authority ingestion pipeline coordinating harvesting, normalization, embedding, and indexing."""

from __future__ import annotations

import logging
import queue
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Iterator, Optional, Union

from lxml import etree
from rdflib import Graph

from reconciliator.config import OAIIngestConfig
from reconciliator.embedder import SentenceEmbedder
from reconciliator.harvest.oai_pmh import OAIPMHHarvester, partition_date_range
from reconciliator.harvest.resourcesync import ResourceSyncHarvester
from reconciliator.loaders.bibframe import (
    BIBFRAMESchemaProfile,
    build_target_record_from_bibframe,
    decompose_bibframe_work,
    get_profile,
    parse_bibframe_graph,
    stream_bibframe_records,
)
from reconciliator.loaders.marcxml import (
    build_target_record_from_marc,
    decompose_marc_wemi,
    iter_marcxml_records,
    parse_marc_record_elem,
)
from reconciliator.models import BibliographicRecord, HarvestState, TargetRecord
from reconciliator.normalizers.record import RecordNormalizer
from reconciliator.progress import ProgressTracker
from reconciliator.repository import AuthorityStore, CorpusRepository, WEMIRepository

logger = logging.getLogger(__name__)


def stream_oai_records(
    source_name: str,
    config: OAIIngestConfig,
    tracker: Optional[ProgressTracker] = None,
    on_record_harvested: Optional[Callable[[], None]] = None,
) -> Iterator[dict[str, Any]]:
    """Yield raw record dictionaries from local file, ResourceSync, or OAI-PMH harvest."""
    profile: Optional[BIBFRAMESchemaProfile] = (
        get_profile(config.schema_profile) if config.schema_profile else None
    )

    if config.source_type == "file":
        if not config.input_path:
            raise ValueError(
                f"input_path must be specified for source '{source_name}' when source_type is 'file'"
            )
        if config.format == "bibframe":
            for raw_rec, _offset, _orig in stream_bibframe_records(
                config.input_path, profile=profile, source=source_name
            ):
                yield raw_rec
        else:
            for raw_rec in iter_marcxml_records(config.input_path, source=source_name):
                yield raw_rec
        return

    if config.source_type == "resourcesync":
        url = config.resourcesync_url or config.oai_url
        if not url:
            raise ValueError(
                f"resourcesync_url or oai_url must be specified for source '{source_name}' when source_type is 'resourcesync'"
            )
        rs_harvester = ResourceSyncHarvester(
            endpoint_url=url,
            timeout=config.timeout,
            request_delay=config.request_delay,
        )
        for item, payload in rs_harvester.harvest_contents():
            if on_record_harvested is not None:
                on_record_harvested()
            elif tracker is not None:
                tracker.update_progress(items_increment=1)

            g = Graph()
            fmt = "turtle"
            if item.mime_type:
                if "json" in item.mime_type:
                    fmt = "json-ld"
                elif "xml" in item.mime_type or "rdf" in item.mime_type:
                    fmt = "xml"
            elif item.loc.endswith(".json") or item.loc.endswith(".jsonld"):
                fmt = "json-ld"
            elif item.loc.endswith(".rdf") or item.loc.endswith(".xml"):
                fmt = "xml"

            try:
                g.parse(data=payload.decode("utf-8", errors="replace"), format=fmt)
                for raw_rec, _nodes in parse_bibframe_graph(g, profile=profile, source=source_name):
                    yield raw_rec
            except Exception as exc:
                logger.warning("Failed to parse ResourceSync payload from %s: %s", item.loc, exc)
        return

    # OAI-PMH source
    if not config.oai_url:
        raise ValueError(
            f"oai_url must be specified for source '{source_name}' when source_type is 'oai'"
        )

    def _parse_harvested_elem(elem: etree._Element) -> list[dict[str, Any]]:
        if config.format == "bibframe":
            raw_xml = etree.tostring(elem, encoding="utf-8")
            g = Graph()
            g.parse(data=raw_xml.decode("utf-8"), format="xml")
            return [r for r, _ in parse_bibframe_graph(g, profile=profile, source=source_name)]
        return [parse_marc_record_elem(elem, source=source_name, id_source=config.id_source)]

    harvester = OAIPMHHarvester(
        endpoint_url=config.oai_url,
        metadata_prefix=config.metadata_prefix,
        set_spec=config.set_spec,
        request_delay=config.request_delay,
        timeout=config.timeout,
    )

    from_date = config.from_date
    until_date = config.until_date

    # When multiple harvest workers are requested, temporal date slicing can be used.
    # If from_date is omitted, attempt to auto-discover the repository earliestDatestamp via OAI Identify.
    if config.workers > 1:
        if not from_date:
            discovered_from = harvester.get_earliest_datestamp()
            if discovered_from:
                from_date = discovered_from
                if not until_date:
                    until_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                logger.info(
                    "Source '%s': auto-discovered earliestDatestamp '%s'. Partitioning across %d workers up to %s.",
                    source_name,
                    from_date,
                    config.workers,
                    until_date,
                )
            else:
                logger.warning(
                    "Source '%s': multi-worker harvest requested (%d workers) but from_date was omitted and earliestDatestamp could not be discovered. Falling back to single continuous stream.",
                    source_name,
                    config.workers,
                )
        elif not until_date:
            until_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    if from_date and until_date and config.workers > 1:
        slices = partition_date_range(from_date, until_date, config.slice_days)
    else:
        slices = [(from_date, until_date)]

    if len(slices) <= 1 or config.workers <= 1:
        for from_dt, until_dt in slices:
            for elem in harvester.harvest_partition(
                from_dt,
                until_dt,
                tracker=tracker if on_record_harvested is None else None,
                on_record_harvested=on_record_harvested,
            ):
                for raw_rec in _parse_harvested_elem(elem):
                    yield raw_rec
    else:
        queue_size = (
            config.queue_size
            if getattr(config, "queue_size", None) is not None
            else max(config.batch_size * config.workers * 2, 2000)
        )
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
                    for raw_rec in _parse_harvested_elem(elem):
                        while not worker_error:
                            try:
                                record_queue.put(raw_rec, timeout=0.2)
                                break
                            except queue.Full:
                                continue
            except Exception as e:
                logger.error(
                    "Error harvesting partition %s to %s for source '%s': %s",
                    from_dt,
                    until_dt,
                    source_name,
                    e,
                )
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


def stream_oai_raw_elements(
    source_name: str,
    config: OAIIngestConfig,
    initial_resumption_token: Optional[str] = None,
    tracker: Optional[ProgressTracker] = None,
    on_record_harvested: Optional[Callable[[], None]] = None,
    on_resumption_token: Optional[Callable[[Optional[str]], None]] = None,
) -> Iterator[Any]:
    """Yield raw MARCXML etree elements or RDF graphs from local file, ResourceSync, or OAI-PMH harvest."""
    if config.source_type == "file":
        if not config.input_path:
            raise ValueError(
                f"input_path must be specified for source '{source_name}' when source_type is 'file'"
            )
        if config.format == "bibframe":
            profile = get_profile(config.schema_profile) if config.schema_profile else None
            for _raw_rec, _offset, orig_doc in stream_bibframe_records(
                config.input_path, profile=profile, source=source_name
            ):
                if isinstance(orig_doc, Graph):
                    yield orig_doc
                else:
                    g = Graph()
                    import orjson

                    g.parse(data=orjson.dumps(orig_doc).decode("utf-8"), format="json-ld")
                    yield g
        else:
            path = str(config.input_path)
            if path.endswith(".gz"):
                import gzip

                f_in = gzip.open(path, "rb")
            else:
                f_in = open(path, "rb")
            try:
                context = etree.iterparse(f_in, events=("end",))
                for _, elem in context:
                    tag = elem.tag
                    if tag.endswith("record") and not tag.endswith("collection"):
                        yield elem
                        elem.clear()
            finally:
                f_in.close()
        return

    if config.source_type == "resourcesync":
        url = config.resourcesync_url or config.oai_url
        if not url:
            raise ValueError(
                f"resourcesync_url or oai_url must be specified for source '{source_name}' when source_type is 'resourcesync'"
            )
        rs_harvester = ResourceSyncHarvester(
            endpoint_url=url,
            timeout=config.timeout,
            request_delay=config.request_delay,
        )
        for item, payload in rs_harvester.harvest_contents():
            if on_record_harvested is not None:
                on_record_harvested()
            elif tracker is not None:
                tracker.update_progress(items_increment=1)

            g = Graph()
            fmt = "turtle"
            if item.mime_type:
                if "json" in item.mime_type:
                    fmt = "json-ld"
                elif "xml" in item.mime_type or "rdf" in item.mime_type:
                    fmt = "xml"
            elif item.loc.endswith(".json") or item.loc.endswith(".jsonld"):
                fmt = "json-ld"
            elif item.loc.endswith(".rdf") or item.loc.endswith(".xml"):
                fmt = "xml"

            try:
                g.parse(data=payload.decode("utf-8", errors="replace"), format=fmt)
                yield g
            except Exception as exc:
                logger.warning("Failed to parse ResourceSync payload from %s: %s", item.loc, exc)
        return

    # OAI-PMH source
    if not config.oai_url:
        raise ValueError(
            f"oai_url must be specified for source '{source_name}' when source_type is 'oai'"
        )

    harvester = OAIPMHHarvester(
        endpoint_url=config.oai_url,
        metadata_prefix=config.metadata_prefix,
        set_spec=config.set_spec,
        request_delay=config.request_delay,
        timeout=config.timeout,
    )

    from_date = config.from_date
    until_date = config.until_date

    for elem in harvester.harvest_partition(
        from_date=from_date,
        until_date=until_date,
        resumption_token=initial_resumption_token,
        tracker=tracker if on_record_harvested is None else None,
        on_record_harvested=on_record_harvested,
        on_resumption_token=on_resumption_token,
    ):
        if config.format == "bibframe":
            raw_xml = etree.tostring(elem, encoding="utf-8")
            g = Graph()
            g.parse(data=raw_xml.decode("utf-8"), format="xml")
            yield g
        else:
            yield elem


def ingest_oai_wemi_pipeline(
    source_name: str,
    config: OAIIngestConfig,
    wemi_repo: Optional[WEMIRepository] = None,
    corpus_repo: Optional[CorpusRepository] = None,
    tracker: Optional[ProgressTracker] = None,
    relator_mappings: Optional[dict[str, str]] = None,
    vocab_store: Optional[Any] = None,
) -> int:
    """Run WEMI authority ingestion (authority.db) or target corpus staging (reconcile_corpus.db) with checkpointing."""
    if corpus_repo is None and config.target_database is not None:
        corpus_repo = CorpusRepository(config.target_database)

    is_target = corpus_repo is not None
    repo: Union[CorpusRepository, WEMIRepository]
    if is_target:
        assert corpus_repo is not None
        repo = corpus_repo
    elif wemi_repo is not None:
        repo = wemi_repo
    else:
        raise ValueError(
            "Either wemi_repo or corpus_repo (or config.target_database) must be provided."
        )

    existing_state = repo.get_harvest_state(source_name)
    initial_resumption_token: Optional[str] = None
    records_already_harvested = 0
    if existing_state:
        if existing_state.status == "in_progress" and existing_state.resumption_token:
            initial_resumption_token = existing_state.resumption_token
            records_already_harvested = existing_state.records_harvested
            logger.info(
                "Source '%s': resuming harvest from token '%s' (previously harvested: %d)",
                source_name,
                initial_resumption_token,
                records_already_harvested,
            )
        elif (
            existing_state.status == "completed"
            and not config.from_date
            and existing_state.last_harvest_until
        ):
            config = config.model_copy(update={"from_date": existing_state.last_harvest_until})
            logger.info(
                "Source '%s': incremental harvest from %s",
                source_name,
                config.from_date,
            )

    total_ingested = 0
    cur_token = initial_resumption_token

    def _save_checkpoint(status: str = "in_progress", token: Optional[str] = None) -> None:
        nonlocal cur_token
        cur_token = token
        state = HarvestState(
            source_id=source_name,
            source_type=config.source_type,
            metadata_prefix=config.metadata_prefix,
            set_spec=config.set_spec,
            last_harvest_from=config.from_date,
            last_harvest_until=config.until_date,
            resumption_token=token,
            records_harvested=records_already_harvested + total_ingested,
            status=status,
        )
        repo.save_harvest_state(state)

    if tracker:
        tracker.start_task(
            stage="harvest_wemi", metadata={"source": source_name, "target_routing": is_target}
        )

    target_records_batch: list[TargetRecord] = []
    works_batch = []
    expressions_batch = []
    manifestations_batch = []
    items_batch = []
    agents_batch = []
    contributions_batch = []
    relationships_batch = []
    identifiers_batch = []
    associations_batch = []
    mediums_batch = []

    def _flush_batches() -> None:
        nonlocal total_ingested
        if is_target and target_records_batch:
            assert corpus_repo is not None
            corpus_repo.insert_target_records_batch(target_records_batch)
            total_ingested += len(target_records_batch)
            target_records_batch.clear()
            _save_checkpoint(status="in_progress", token=cur_token)
        elif not is_target and works_batch:
            assert wemi_repo is not None
            wemi_repo.insert_wemi_batch(
                works=works_batch,
                expressions=expressions_batch,
                manifestations=manifestations_batch,
                items=items_batch,
                agents=agents_batch,
                relationships=relationships_batch,
                identifiers=identifiers_batch,
                manifestation_expressions=associations_batch,
                mediums_of_performance=mediums_batch,
                contributions=contributions_batch,
            )
            total_ingested += len(works_batch)
            works_batch.clear()
            expressions_batch.clear()
            manifestations_batch.clear()
            items_batch.clear()
            agents_batch.clear()
            contributions_batch.clear()
            relationships_batch.clear()
            identifiers_batch.clear()
            associations_batch.clear()
            mediums_batch.clear()
            _save_checkpoint(status="in_progress", token=cur_token)

    try:
        for item in stream_oai_raw_elements(
            source_name=source_name,
            config=config,
            initial_resumption_token=initial_resumption_token,
            tracker=tracker,
            on_resumption_token=lambda tok: _save_checkpoint(status="in_progress", token=tok),
        ):
            if is_target:
                if isinstance(item, Graph):
                    rec = build_target_record_from_bibframe(item, source=source_name)
                else:
                    rec = build_target_record_from_marc(
                        item,
                        source=source_name,
                        id_source=config.id_source,
                        relator_mappings=relator_mappings,
                        vocab_store=vocab_store,
                    )
                target_records_batch.append(rec)
                if len(target_records_batch) >= config.batch_size:
                    _flush_batches()
            else:
                if isinstance(item, Graph):
                    w, e, m, itms, agts, rels, idents, assoc, mops, contribs = (
                        decompose_bibframe_work(item, source=source_name)
                    )
                else:
                    w, e, m, itms, agts, rels, idents, assoc, mops, contribs = decompose_marc_wemi(
                        item,
                        source=source_name,
                        id_source=config.id_source,
                        include_items=config.include_items,
                        relator_mappings=relator_mappings,
                        vocab_store=vocab_store,
                    )
                works_batch.append(w)
                expressions_batch.append(e)
                manifestations_batch.append(m)
                items_batch.extend(itms)
                agents_batch.extend(agts)
                contributions_batch.extend(contribs)
                relationships_batch.extend(rels)
                identifiers_batch.extend(idents)
                associations_batch.extend(assoc)
                mediums_batch.extend(mops)
                if len(works_batch) >= config.batch_size:
                    _flush_batches()

        _flush_batches()
        _save_checkpoint(status="completed", token=None)
        if tracker:
            tracker.complete_task(
                metadata={"total_ingested": total_ingested, "source": source_name}
            )
    except Exception as exc:
        if tracker:
            tracker.fail_task(error_message=str(exc))
        raise

    return total_ingested


def ingest_oai_pipeline(
    source_name: str,
    config: OAIIngestConfig,
    store: Union[AuthorityStore, WEMIRepository, CorpusRepository],
    normalizer: Optional[RecordNormalizer] = None,
    embedder: Optional[SentenceEmbedder] = None,
    tracker: Optional[ProgressTracker] = None,
    skip_existing: bool = False,
) -> int:
    """Run full authority ingestion, embedding, and storage indexing for a configured OAI/file source."""
    if isinstance(store, (WEMIRepository, CorpusRepository)) or config.target_database is not None:
        w_repo = store if isinstance(store, WEMIRepository) else None
        c_repo = store if isinstance(store, CorpusRepository) else None
        r_mappings = getattr(getattr(normalizer, "config", None), "relator_mappings", None)
        v_store = getattr(getattr(normalizer, "vocab_folder", None), "vocab_store", None)
        return ingest_oai_wemi_pipeline(
            source_name=source_name,
            config=config,
            wemi_repo=w_repo,
            corpus_repo=c_repo,
            tracker=tracker,
            relator_mappings=r_mappings,
            vocab_store=v_store,
        )

    if normalizer is None or embedder is None:
        raise ValueError("normalizer and embedder required for AuthorityStore ingestion")

    is_oai = config.source_type != "file"
    stage_name = "harvest_and_index" if is_oai else "indexing"

    harvested_count = 0
    harvested_lock = threading.Lock()
    total_ingested = 0

    existing_ids = store.get_indexed_source_ids(source=source_name) if skip_existing else set()
    if existing_ids:
        logger.info(
            "Skip-existing active: found %d already indexed records for source '%s'.",
            len(existing_ids),
            source_name,
        )
    skipped_count = 0
    skipped_lock = threading.Lock()

    def on_record_harvested() -> None:
        nonlocal harvested_count
        with harvested_lock:
            harvested_count += 1
            cur_h = harvested_count
        with skipped_lock:
            cur_sk = skipped_count
        if tracker and cur_h % 200 == 0:
            tracker.update_progress(
                stage=stage_name,
                metadata={
                    "harvested": cur_h,
                    "indexed": total_ingested,
                    "skipped": cur_sk,
                    "source": source_name,
                },
            )

    if tracker:
        tracker.start_task(
            stage=stage_name,
            metadata={"harvested": 0, "indexed": 0, "skipped": 0, "source": source_name}
            if is_oai
            else None,
        )

    last_checkpoint = 0
    checkpoint_interval = getattr(config, "checkpoint_interval", 50000)
    norm_workers = getattr(config, "normalization_workers", 4)
    batch_queue_size = getattr(config, "batch_queue_size", 4)

    # Harvest queue capacity defaults to 20 batches (or at least 10,000 items) unless explicitly configured.
    # This decouples network fetching and skip filtering from GPU embedding and DB writing.
    harvest_queue_size = (
        config.queue_size
        if getattr(config, "queue_size", None) is not None
        else max(config.batch_size * 20, 10000)
    )

    harvest_queue: queue.Queue[Any] = queue.Queue(maxsize=harvest_queue_size)
    batch_queue: queue.Queue[Any] = queue.Queue(maxsize=batch_queue_size)

    sentinel = object()
    stop_event = threading.Event()
    harvest_error: list[BaseException] = []
    normalizer_error: list[BaseException] = []
    harvester_thread: Optional[threading.Thread] = None

    def harvest_worker() -> None:
        nonlocal skipped_count
        try:
            for raw_rec in stream_oai_records(
                source_name=source_name,
                config=config,
                tracker=tracker,
                on_record_harvested=on_record_harvested if is_oai else None,
            ):
                if stop_event.is_set() or harvest_error or normalizer_error:
                    return

                raw_id = raw_rec.get("source_id")
                is_existing = False
                if skip_existing and raw_id:
                    if raw_id in existing_ids or normalizer.normalize_id(raw_id) in existing_ids:
                        is_existing = True

                if is_existing:
                    with skipped_lock:
                        skipped_count += 1
                        cur_sk = skipped_count
                    if cur_sk % 10000 == 0:
                        logger.info(
                            "Source '%s': skipped %d already indexed records...",
                            source_name,
                            cur_sk,
                        )
                    if tracker and cur_sk % 500 == 0:
                        with harvested_lock:
                            cur_h = harvested_count
                        tracker.update_progress(
                            stage=stage_name,
                            items_processed=total_ingested,
                            metadata={
                                "harvested": cur_h,
                                "indexed": total_ingested,
                                "skipped": cur_sk,
                                "source": source_name,
                            }
                            if is_oai
                            else None,
                        )
                    continue

                while not stop_event.is_set():
                    if harvest_error or normalizer_error:
                        return
                    try:
                        harvest_queue.put(raw_rec, timeout=0.2)
                        break
                    except queue.Full:
                        continue
        except Exception as exc:
            logger.error("Error in harvester worker for source '%s': %s", source_name, exc)
            harvest_error.append(exc)
        finally:
            harvest_queue.put(sentinel)

    def normalizer_worker() -> None:
        try:
            raw_chunk: list[dict[str, Any]] = []

            def _flush_chunk(
                chunk: list[dict[str, Any]], executor: Optional[ThreadPoolExecutor]
            ) -> None:
                if not chunk:
                    return
                if executor is not None:
                    chunksize = max(1, len(chunk) // (norm_workers * 2))
                    norm_batch = list(
                        executor.map(normalizer.normalize, chunk, chunksize=chunksize)
                    )
                else:
                    norm_batch = [normalizer.normalize(r) for r in chunk]

                while not stop_event.is_set():
                    if harvest_error or normalizer_error:
                        return
                    try:
                        batch_queue.put(norm_batch, timeout=0.2)
                        break
                    except queue.Full:
                        continue

            executor = ThreadPoolExecutor(max_workers=norm_workers) if norm_workers > 1 else None
            try:
                while not stop_event.is_set():
                    if harvest_error or normalizer_error:
                        return
                    try:
                        item = harvest_queue.get(timeout=0.2)
                    except queue.Empty:
                        if (
                            harvester_thread is not None
                            and not harvester_thread.is_alive()
                            and harvest_queue.empty()
                        ):
                            break
                        continue

                    if item is sentinel:
                        harvest_queue.task_done()
                        break

                    raw_chunk.append(item)
                    harvest_queue.task_done()
                    if len(raw_chunk) >= config.batch_size:
                        chunk_to_norm = raw_chunk
                        raw_chunk = []
                        _flush_chunk(chunk_to_norm, executor)

                if raw_chunk:
                    _flush_chunk(raw_chunk, executor)
            finally:
                if executor is not None:
                    executor.shutdown(wait=True)
        except Exception as exc:
            logger.error("Error in normalizer worker for source '%s': %s", source_name, exc)
            normalizer_error.append(exc)
        finally:
            batch_queue.put(sentinel)

    try:
        harvester_thread = threading.Thread(target=harvest_worker, daemon=True)
        normalizer_thread = threading.Thread(target=normalizer_worker, daemon=True)
        harvester_thread.start()
        normalizer_thread.start()

        while True:
            if harvest_error:
                raise harvest_error[0]
            if normalizer_error:
                raise normalizer_error[0]

            try:
                item = batch_queue.get(timeout=0.2)
            except queue.Empty:
                if (
                    not normalizer_thread.is_alive()
                    and not harvester_thread.is_alive()
                    and batch_queue.empty()
                    and harvest_queue.empty()
                ):
                    if harvest_error:
                        raise harvest_error[0]
                    if normalizer_error:
                        raise normalizer_error[0]
                    break
                continue

            if item is sentinel:
                batch_queue.task_done()
                if harvest_error:
                    raise harvest_error[0]
                if normalizer_error:
                    raise normalizer_error[0]
                break

            current_batch: list[BibliographicRecord] = item
            vectors = embedder.embed_records(current_batch)
            store.insert_records_batch(current_batch, vectors=vectors)
            total_ingested += len(current_batch)
            batch_queue.task_done()

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
                with harvested_lock:
                    cur_h = harvested_count
                with skipped_lock:
                    cur_sk = skipped_count
                tracker.update_progress(
                    stage=stage_name,
                    items_processed=total_ingested,
                    metadata={
                        "harvested": cur_h,
                        "indexed": total_ingested,
                        "skipped": cur_sk,
                        "source": source_name,
                    }
                    if is_oai
                    else None,
                )
    except Exception as exc:
        stop_event.set()
        if tracker:
            tracker.fail_task(error_message=str(exc))
        raise
    finally:
        stop_event.set()
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

    if skipped_count > 0:
        logger.info(
            "Source '%s': skipped %d already indexed records, ingested %d new/updated records.",
            source_name,
            skipped_count,
            total_ingested,
        )

    if tracker:
        meta = (
            {
                "harvested": harvested_count,
                "indexed": total_ingested,
                "skipped": skipped_count,
                "source": source_name,
            }
            if is_oai
            else {"records_ingested": total_ingested, "source": source_name}
        )
        tracker.complete_task(metadata=meta)

    return total_ingested
