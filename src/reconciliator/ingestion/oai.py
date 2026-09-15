"""OAI-PMH, ResourceSync, and MARCXML ingestion into WEMI storage.

Harvesting is decoupled from database writes and checkpointed to harvest_state so
an interrupted run resumes where it stopped. Records are not embedded here; vector
indices are built separately by build-index.
"""

from __future__ import annotations

import logging
import queue
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Iterator, Optional

from lxml import etree
from rdflib import Graph

from reconciliator.config import OAIIngestConfig
from reconciliator.harvest.oai_pmh import OAIPMHHarvester, partition_date_range
from reconciliator.harvest.resourcesync import ResourceSyncHarvester
from reconciliator.loaders.bibframe import (
    bibframe_entity_keys,
    decompose_bibframe_work,
    get_profile,
    stream_bibframe_records,
)
from reconciliator.loaders.marcxml import (
    decompose_marc_authority,
    decompose_marc_wemi,
    extract_marc_source_id,
    is_marc_authority_record,
)
from reconciliator.models import HarvestState
from reconciliator.progress import ProgressTracker
from reconciliator.repository import WEMIRepository

logger = logging.getLogger(__name__)


def stream_oai_raw_elements(
    source_name: str,
    config: OAIIngestConfig,
    initial_resumption_token: Optional[str] = None,
    tracker: Optional[ProgressTracker] = None,
    on_record_harvested: Optional[Callable[[], None]] = None,
    on_resumption_token: Optional[Callable[[Optional[str]], None]] = None,
    from_date: Optional[str] = None,
    until_date: Optional[str] = None,
) -> Iterator[Any]:
    """Yield raw MARCXML etree elements or RDF graphs from local file, ResourceSync, or OAI-PMH harvest.

    ``from_date`` / ``until_date`` override the configured harvest window, so a
    caller can drive one date slice of a partitioned harvest.
    """
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
                # The RDFLib loader yields (graph, nodes); the JSON-LD streamer
                # yields a plain dict. Unwrap the tuple before deciding.
                if isinstance(orig_doc, tuple) and orig_doc:
                    orig_doc = orig_doc[0]
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

    for elem in harvester.harvest_partition(
        from_date=from_date if from_date is not None else config.from_date,
        until_date=until_date if until_date is not None else config.until_date,
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


def _raw_source_id(item: Any, config: OAIIngestConfig) -> Optional[str]:
    """Best-effort source_id of a harvested record, for skip-existing filtering.

    RDF graphs return None: their identifiers live in the graph rather than a
    control field, so they are always ingested rather than skipped.
    """
    if isinstance(item, Graph):
        return None
    try:
        return extract_marc_source_id(item, id_source=config.id_source)
    except Exception:
        return None


class _WEMIBatchAccumulator:
    """Stages decomposed WEMI entities until a batch is ready to hand to the writer.

    One accumulator per producer thread; batches are detached with take() and
    persisted by write_wemi_batch() on the consumer thread.
    """

    def __init__(
        self,
        source_name: str,
        config: OAIIngestConfig,
        relator_mappings: Optional[dict[str, str]] = None,
        vocab_store: Optional[Any] = None,
        strip_isbd_punctuation: bool = True,
        isbd_punctuation: str = " ./,;:",
    ) -> None:
        self.source_name = source_name
        self.strip_isbd_punctuation = strip_isbd_punctuation
        self.isbd_punctuation = isbd_punctuation
        self.config = config
        self.relator_mappings = relator_mappings
        self.vocab_store = vocab_store
        # Harvested records staged since the last take(). Counted separately from
        # the entity lists because one record can decompose into several agents,
        # and the resume watermark has to advance in records, not entities.
        self.staged_records = 0

        self.works: list[Any] = []
        self.expressions: list[Any] = []
        self.manifestations: list[Any] = []
        self.items: list[Any] = []
        self.agents: list[Any] = []
        self.contributions: list[Any] = []
        self.relationships: list[Any] = []
        self.identifiers: list[Any] = []
        self.associations: list[Any] = []
        self.mediums: list[Any] = []
        self.entity_keys: list[Any] = []

    @property
    def pending(self) -> int:
        """Number of staged records awaiting a flush."""
        return self.staged_records

    def add(self, item: Any) -> None:
        """Decompose one harvested MARCXML element or RDF graph into the staging batch."""
        self.staged_records += 1

        if isinstance(item, Graph):
            w, e, m, itms, agts, rels, idents, assoc, mops, contribs = decompose_bibframe_work(
                item, source=self.source_name
            )
            self.works.append(w)
            self.expressions.append(e)
            self.manifestations.append(m)
            self.entity_keys.extend(bibframe_entity_keys(m, w, agts, idents))
        elif is_marc_authority_record(item):
            (
                w_list,
                e_list,
                m_list,
                itms,
                agts,
                rels,
                idents,
                assoc,
                mops,
                contribs,
            ) = decompose_marc_authority(
                item,
                source=self.source_name,
                id_source=self.config.id_source,
                record_origin=self.source_name,
                strip_isbd_punctuation=self.strip_isbd_punctuation,
                isbd_punctuation=self.isbd_punctuation,
            )
            self.works.extend(w_list)
            self.expressions.extend(e_list)
            self.manifestations.extend(m_list)
        else:
            w, e, m, itms, agts, rels, idents, assoc, mops, contribs = decompose_marc_wemi(
                item,
                source=self.source_name,
                id_source=self.config.id_source,
                include_items=self.config.include_items,
                record_origin=self.source_name,
                relator_mappings=self.relator_mappings,
                vocab_store=self.vocab_store,
                strip_isbd_punctuation=self.strip_isbd_punctuation,
                isbd_punctuation=self.isbd_punctuation,
                key_sink=self.entity_keys,
            )
            self.works.append(w)
            self.expressions.append(e)
            self.manifestations.append(m)

        self.items.extend(itms)
        self.agents.extend(agts)
        self.contributions.extend(contribs)
        self.relationships.extend(rels)
        self.identifiers.extend(idents)
        self.associations.extend(assoc)
        self.mediums.extend(mops)

    def take(self) -> dict[str, Any]:
        """Detach the staged batch so it can be handed to a writer thread."""
        record_count = self.staged_records
        self.staged_records = 0

        payload = {
            "works": self.works,
            "expressions": self.expressions,
            "manifestations": self.manifestations,
            "items": self.items,
            "agents": self.agents,
            "relationships": self.relationships,
            "identifiers": self.identifiers,
            "associations": self.associations,
            "mediums": self.mediums,
            "contributions": self.contributions,
            "entity_keys": self.entity_keys,
            "record_count": record_count,
            "record_origin": self.source_name,
        }
        self.works = []
        self.expressions = []
        self.manifestations = []
        self.items = []
        self.agents = []
        self.contributions = []
        self.relationships = []
        self.identifiers = []
        self.associations = []
        self.mediums = []
        self.entity_keys = []
        return payload


def write_wemi_batch(
    repo: WEMIRepository,
    payload: dict[str, Any],
) -> int:
    """Persist a batch detached by `_WEMIBatchAccumulator.take()`.

    Standalone so a writer thread can persist batches produced by several
    accumulators without owning any of them. Returns the number of harvested
    records written, which is not the number of entities: one record can
    decompose into several agents.
    """
    record_count = int(payload.get("record_count") or 0)
    record_origin = str(payload.get("record_origin") or "unknown")

    works = payload.get("works") or []
    agents = payload.get("agents") or []
    if not (works or agents):
        return 0
    repo.insert_wemi_batch(
        works=works,
        expressions=payload.get("expressions") or [],
        manifestations=payload.get("manifestations") or [],
        items=payload.get("items") or [],
        agents=agents,
        relationships=payload.get("relationships") or [],
        identifiers=payload.get("identifiers") or [],
        manifestation_expressions=payload.get("associations") or [],
        mediums_of_performance=payload.get("mediums") or [],
        contributions=payload.get("contributions") or [],
        entity_keys=payload.get("entity_keys") or [],
        record_origin=record_origin,
    )
    return record_count or (len(works) or len(agents))


def _resolve_harvest_window(
    source_name: str,
    config: OAIIngestConfig,
) -> tuple[Optional[str], Optional[str]]:
    """Resolve the (from, until) window for a partitioned harvest.

    Date partitioning needs both bounds. When more than one worker is requested
    but from_date was omitted, the repository's earliestDatestamp is discovered
    via the OAI-PMH Identify verb; until_date defaults to today.
    """
    from_date = config.from_date
    until_date = config.until_date

    if config.workers <= 1 or config.source_type != "oai" or not config.oai_url:
        return from_date, until_date

    if not from_date:
        harvester = OAIPMHHarvester(
            endpoint_url=config.oai_url,
            metadata_prefix=config.metadata_prefix,
            set_spec=config.set_spec,
            request_delay=config.request_delay,
            timeout=config.timeout,
        )
        discovered = harvester.get_earliest_datestamp()
        if discovered:
            from_date = discovered
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
                "Source '%s': multi-worker harvest requested (%d workers) but from_date was "
                "omitted and earliestDatestamp could not be discovered. Falling back to a "
                "single continuous stream.",
                source_name,
                config.workers,
            )
    elif not until_date:
        until_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    return from_date, until_date


def ingest_oai_pipeline(
    source_name: str,
    config: OAIIngestConfig,
    store: WEMIRepository,
    tracker: Optional[ProgressTracker] = None,
    skip_existing: bool = False,
    relator_mappings: Optional[dict[str, str]] = None,
    vocab_store: Optional[Any] = None,
    resume: bool = True,
    strip_isbd_punctuation: bool = True,
    isbd_punctuation: str = " ./,;:",
) -> int:
    """Ingest a configured OAI/file source into WEMI storage over a decoupled pipeline.

    Producer threads fetch records, drop already-ingested ones, and decompose what
    remains into WEMI entities; this thread performs the batch writes. That keeps
    network fetching and parsing off the critical path of the SQLite writes, with
    ``batch_queue_size`` batches of slack between the stages.

    Decomposition deliberately runs on the producer thread rather than a further
    stage: the MARCXML streamer calls ``elem.clear()`` as soon as control returns
    to it, so a queued lxml element would be emptied before another thread could
    read it.

    With ``workers > 1`` and a resolvable date window the harvest is partitioned
    into ``slice_days`` slices fetched concurrently; otherwise a single continuous
    stream is followed via resumptionToken.

    Harvest progress is checkpointed to ``harvest_state`` so an interrupted run
    resumes where it left off, and a completed run harvests incrementally next
    time. Pass ``resume=False`` to force a full re-harvest.
    """
    is_oai = config.source_type != "file"
    stage_name = "harvest_and_index" if is_oai else "indexing"

    # Every ingested source is registered, authoritative or not, so matching can
    # decide what may be used as authority from the registry rather than from a
    # literal stamped on each row.
    store.register_source(source_name, is_authoritative=config.authoritative)

    existing_ids = store.get_indexed_source_ids(source=source_name) if skip_existing else set()
    if existing_ids:
        logger.info(
            "Skip-existing active: found %d already ingested records for source '%s'.",
            len(existing_ids),
            source_name,
        )

    # ------------------------------------------------------------------
    # Resume decision
    # ------------------------------------------------------------------
    initial_token: Optional[str] = None
    already_harvested = 0
    done_slices: set[tuple[Optional[str], Optional[str]]] = set()
    effective_config = config

    if is_oai and resume:
        prior = store.get_harvest_state(source_name)
        if prior:
            if prior.status == "in_progress":
                if prior.resumption_token:
                    initial_token = prior.resumption_token
                    already_harvested = prior.records_harvested
                    logger.info(
                        "Source '%s': resuming harvest from token '%s' (previously harvested: %d)",
                        source_name,
                        initial_token,
                        already_harvested,
                    )
                if prior.completed_slices:
                    done_slices = {tuple(s) for s in prior.completed_slices}  # type: ignore[misc]
                    already_harvested = prior.records_harvested
                    logger.info(
                        "Source '%s': resuming partitioned harvest, skipping %d completed slices "
                        "(previously harvested: %d)",
                        source_name,
                        len(done_slices),
                        already_harvested,
                    )
            elif (
                prior.status == "completed"
                and not config.from_date
                and prior.last_harvest_until
            ):
                effective_config = config.model_copy(
                    update={"from_date": prior.last_harvest_until}
                )
                logger.info(
                    "Source '%s': incremental harvest from %s",
                    source_name,
                    prior.last_harvest_until,
                )

    # ------------------------------------------------------------------
    # Replace mode
    # ------------------------------------------------------------------
    # Clear once, here, before the first batch - and only when this run is not
    # continuing an earlier one. A resumed harvest that cleared would delete
    # exactly what its own completed slices had just written, and an incremental
    # harvest would throw away everything outside the new date window.
    is_continuation = bool(initial_token or done_slices) or effective_config is not config
    if config.ingest_mode == "replace" and not is_continuation:
        removed = store.delete_source(source_name)
        total_removed = sum(removed.values())
        if total_removed:
            logger.info(
                "Source '%s': ingest_mode=replace removed %d existing rows (%s)",
                source_name,
                total_removed,
                ", ".join(f"{k}={v}" for k, v in sorted(removed.items()) if v),
            )
    elif config.ingest_mode == "replace" and is_continuation:
        logger.info(
            "Source '%s': ingest_mode=replace skipped - this run continues an earlier "
            "harvest, and clearing would delete what it already wrote",
            source_name,
        )

    from_date, until_date = _resolve_harvest_window(source_name, effective_config)

    if from_date and until_date and effective_config.workers > 1:
        slices = partition_date_range(from_date, until_date, effective_config.slice_days)
    else:
        slices = [(from_date, until_date)]

    pending_slices = [s for s in slices if tuple(s) not in done_slices]
    partitioned = len(pending_slices) > 1 and effective_config.workers > 1
    if not pending_slices:
        logger.info("Source '%s': every date slice already harvested.", source_name)

    # A resumptionToken only applies to a single continuous stream; a partitioned
    # harvest resumes at slice granularity instead.
    if partitioned:
        initial_token = None

    counters_lock = threading.Lock()
    harvested_count = 0
    skipped_count = 0
    total_ingested = 0
    # (records_handed_off_at_page_end, token) watermarks. A token is only safe to
    # checkpoint once every record handed off before it was received has been
    # written, otherwise resuming from it would skip unwritten records.
    pending_tokens: list[tuple[int, Optional[str]]] = []
    delivered = 0
    # Furthest token justified by durable writes. Sticky, so re-checkpointing
    # after every watermark has been promoted cannot regress it.
    safe_token: Optional[str] = initial_token
    finished_slices: list[tuple[Optional[str], Optional[str]]] = [
        tuple(s) for s in slices if tuple(s) in done_slices  # type: ignore[misc]
    ]

    def _telemetry() -> dict[str, Any]:
        return {
            "harvested": harvested_count,
            "indexed": total_ingested,
            "skipped": skipped_count,
            "source": source_name,
        }

    def _save_state(
        status: str,
        token: Optional[str],
        slices_done: Optional[list[tuple[Optional[str], Optional[str]]]] = None,
    ) -> None:
        store.save_harvest_state(
            HarvestState(
                source_id=source_name,
                source_type=effective_config.source_type,
                metadata_prefix=effective_config.metadata_prefix,
                set_spec=effective_config.set_spec,
                last_harvest_from=from_date,
                last_harvest_until=until_date
                or datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                resumption_token=token,
                records_harvested=already_harvested + total_ingested,
                status=status,
                completed_slices=slices_done or [],
            )
        )

    def on_record_harvested() -> None:
        nonlocal harvested_count
        with counters_lock:
            harvested_count += 1
            should_report = harvested_count % 200 == 0
            snapshot = _telemetry()
        if tracker and should_report:
            tracker.update_progress(stage=stage_name, metadata=snapshot)

    if tracker:
        tracker.start_task(stage=stage_name, metadata=_telemetry() if is_oai else None)

    batch_queue_size = effective_config.batch_queue_size
    batch_queue: queue.Queue[Any] = queue.Queue(maxsize=batch_queue_size)

    sentinel = object()
    stop_event = threading.Event()
    harvest_error: list[BaseException] = []

    def _should_skip(item: Any) -> bool:
        """Test a harvested record against the already-ingested id set.

        Both sides derive the id from extract_marc_source_id, so an exact match
        is sufficient; records without a resolvable id are always ingested.
        """
        if not existing_ids:
            return False
        raw_id = _raw_source_id(item, effective_config)
        return bool(raw_id and raw_id in existing_ids)

    def _enqueue(payload: Any) -> None:
        while not stop_event.is_set():
            if harvest_error:
                return
            try:
                batch_queue.put(payload, timeout=0.2)
                return
            except queue.Full:
                continue

    def harvest_slice(slice_from: Optional[str], slice_until: Optional[str]) -> None:
        nonlocal skipped_count, delivered
        acc = _WEMIBatchAccumulator(
            source_name=source_name,
            config=effective_config,
            relator_mappings=relator_mappings,
            vocab_store=vocab_store,
            strip_isbd_punctuation=strip_isbd_punctuation,
            isbd_punctuation=isbd_punctuation,
        )

        def note_token(token: Optional[str]) -> None:
            # Called at a page boundary, after every record on the page was yielded.
            with counters_lock:
                pending_tokens.append((delivered, token))

        try:
            for item in stream_oai_raw_elements(
                source_name=source_name,
                config=effective_config,
                initial_resumption_token=initial_token,
                tracker=tracker,
                on_record_harvested=on_record_harvested if is_oai else None,
                on_resumption_token=None if partitioned else note_token,
                from_date=slice_from,
                until_date=slice_until,
            ):
                if stop_event.is_set() or harvest_error:
                    return

                if _should_skip(item):
                    with counters_lock:
                        skipped_count += 1
                        cur_sk = skipped_count
                        snapshot = _telemetry()
                    if cur_sk % 10000 == 0:
                        logger.info(
                            "Source '%s': skipped %d already ingested records...",
                            source_name,
                            cur_sk,
                        )
                    if tracker and cur_sk % 500 == 0:
                        tracker.update_progress(
                            stage=stage_name,
                            items_processed=snapshot["indexed"],
                            metadata=snapshot if is_oai else None,
                        )
                    continue

                acc.add(item)
                with counters_lock:
                    delivered += 1
                if acc.pending >= effective_config.batch_size:
                    _enqueue({"batch": acc.take()})

            if acc.pending:
                _enqueue({"batch": acc.take()})
            # Only a fully drained slice may be recorded as complete.
            _enqueue({"slice_done": (slice_from, slice_until)})
        except Exception as exc:
            logger.error(
                "Error harvesting partition %s to %s for source '%s': %s",
                slice_from,
                slice_until,
                source_name,
                exc,
            )
            harvest_error.append(exc)
            raise

    def produce() -> None:
        try:
            if not pending_slices:
                return
            if not partitioned:
                harvest_slice(*pending_slices[0])
                return
            with ThreadPoolExecutor(max_workers=effective_config.workers) as executor:
                futures = [executor.submit(harvest_slice, f, u) for f, u in pending_slices]
                for fut in futures:
                    fut.result()
        except Exception as exc:
            if exc not in harvest_error:
                harvest_error.append(exc)
        finally:
            batch_queue.put(sentinel)

    def _checkpoint() -> None:
        """Persist the furthest checkpoint that durable writes actually justify.

        Watermarks are promoted only once the records handed off before them have
        been written, so an interrupted run never resumes past stored data.
        """
        nonlocal safe_token
        if not is_oai:
            return
        with counters_lock:
            while pending_tokens and pending_tokens[0][0] <= total_ingested:
                safe_token = pending_tokens.pop(0)[1]
            slices_done = list(finished_slices)
            token = safe_token
        if partitioned:
            _save_state("in_progress", None, slices_done)
        else:
            _save_state("in_progress", token, None)

    def handle(payload: Any) -> None:
        """Apply one queued payload: a decomposed batch, or a slice completion."""
        nonlocal total_ingested
        if "slice_done" in payload:
            finished_slices.append(payload["slice_done"])
            _checkpoint()
            return

        written = write_wemi_batch(
            store, payload["batch"]
        )
        with counters_lock:
            total_ingested += written
            snapshot = _telemetry()
        _checkpoint()

        if tracker:
            tracker.update_progress(
                stage=stage_name,
                items_processed=snapshot["indexed"],
                metadata=snapshot if is_oai else None,
            )

    def drain() -> None:
        """Apply whatever is already queued, without waiting for more.

        Used after a failure: batches and slice completions still in flight are
        real, finished work, and dropping them would re-harvest them next run.
        """
        while True:
            try:
                payload = batch_queue.get_nowait()
            except queue.Empty:
                return
            try:
                if payload is not sentinel:
                    handle(payload)
            finally:
                batch_queue.task_done()

    producer = threading.Thread(target=produce, daemon=True)
    try:
        producer.start()

        while True:
            if harvest_error:
                break

            try:
                payload = batch_queue.get(timeout=0.2)
            except queue.Empty:
                if not producer.is_alive() and batch_queue.empty():
                    break
                continue

            if payload is sentinel:
                batch_queue.task_done()
                break

            try:
                handle(payload)
            finally:
                batch_queue.task_done()

        # A producer failure must surface, but only after the work that did land
        # has been written and checkpointed.
        if harvest_error:
            stop_event.set()
            producer.join(timeout=5.0)
            drain()
            _checkpoint()
            raise harvest_error[0]

        # Late watermarks can arrive after the final batch, so settle up once more.
        _checkpoint()
        if is_oai:
            # Harvest ran to the end: clear the token and the slice ledger so the
            # next run harvests incrementally from last_harvest_until.
            _save_state("completed", None, None)
    except Exception as exc:
        stop_event.set()
        if tracker:
            tracker.fail_task(error_message=str(exc))
        raise
    finally:
        stop_event.set()
        producer.join(timeout=5.0)

    if skipped_count > 0:
        logger.info(
            "Source '%s': skipped %d already ingested records, ingested %d new/updated records.",
            source_name,
            skipped_count,
            total_ingested,
        )

    if tracker:
        meta = (
            _telemetry()
            if is_oai
            else {"records_ingested": total_ingested, "source": source_name}
        )
        tracker.complete_task(metadata=meta)

    return total_ingested
