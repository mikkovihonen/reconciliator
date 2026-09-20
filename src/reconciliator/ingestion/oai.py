"""AsyncIO and ProcessPool hybrid OAI-PMH, ResourceSync, and MARCXML ingestion into WEMI storage.

Harvesting is decoupled from database writes and checkpointed to harvest_state so
an interrupted run resumes where it stopped.
"""

from __future__ import annotations

import asyncio
import logging
import time
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

from lxml import etree
from rdflib import Graph

from reconciliator.config import OAIIngestConfig
from reconciliator.constituents import resolve_for_source
from reconciliator.harvest.oai_pmh import OAIPMHHarvester, partition_date_range
from reconciliator.ingestion.sanitizer import sanitize_batch_payload
from reconciliator.loaders.base import DecomposedRecord, LoadContext
from reconciliator.loaders.bibframe import (
    get_profile,
    stream_bibframe_records,
)
from reconciliator.loaders.marcxml import (
    extract_marc_source_id,
)
from reconciliator.loaders.registry import load_record
from reconciliator.models import HarvestState
from reconciliator.progress import ProgressTracker
from reconciliator.database import WEMIRepository

logger = logging.getLogger(__name__)

OAI_NS = {
    "oai": "http://www.openarchives.org/OAI/2.0/",
    "marc": "http://www.loc.gov/MARC21/slim",
}

_worker_vocab_store: Optional[Any] = None


def _get_worker_vocab_store(db_url: Optional[str]) -> Optional[Any]:
    global _worker_vocab_store
    if db_url and _worker_vocab_store is None:
        try:
            from reconciliator.vocab import VocabStore

            _worker_vocab_store = VocabStore(db_url)
        except Exception:
            _worker_vocab_store = None
    return _worker_vocab_store


class _WEMIBatchAccumulator:
    """Stages decomposed WEMI entities until a batch is ready to hand to the writer."""

    def __init__(
        self,
        source_name: str,
        id_source: str = "auto",
        include_items: bool = False,
        relator_mappings: Optional[dict[str, str]] = None,
        vocab_store: Optional[Any] = None,
        strip_isbd_punctuation: bool = True,
        isbd_punctuation: str = " ./,;:",
        constituents: Optional[Any] = None,
    ) -> None:
        self.source_name = source_name
        self.id_source = id_source
        self.include_items = include_items
        self.strip_isbd_punctuation = strip_isbd_punctuation
        self.isbd_punctuation = isbd_punctuation
        self.relator_mappings = relator_mappings
        self.vocab_store = vocab_store
        self.constituents = constituents
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
        self.entity_keys: list[Any] = []
        self.entity_subjects: list[Any] = []

    @property
    def pending(self) -> int:
        return self.staged_records

    def add_record(self, record: DecomposedRecord) -> None:
        self.staged_records += 1
        self.works.extend(record.works)
        self.expressions.extend(record.expressions)
        self.manifestations.extend(record.manifestations)
        self.items.extend(record.items)
        self.agents.extend(record.agents)
        self.contributions.extend(record.contributions)
        self.relationships.extend(record.relationships)
        self.identifiers.extend(record.identifiers)
        self.associations.extend(record.associations)
        self.entity_keys.extend(record.entity_keys)
        self.entity_subjects.extend(record.entity_subjects)

    def add(self, item: Any) -> None:
        ctx = LoadContext(
            source=self.source_name,
            record_origin=self.source_name,
            id_source=self.id_source,
            include_items=self.include_items,
            relator_mappings=self.relator_mappings,
            vocab_store=self.vocab_store,
            strip_isbd_punctuation=self.strip_isbd_punctuation,
            isbd_punctuation=self.isbd_punctuation,
            constituents=self.constituents,
        )
        record = load_record(item, ctx)
        self.add_record(record)

    def take(self) -> dict[str, Any]:
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
            "contributions": self.contributions,
            "entity_keys": self.entity_keys,
            "entity_subjects": self.entity_subjects,
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
        self.entity_keys = []
        self.entity_subjects = []
        return payload


def parse_and_decompose_page(
    page_bytes: bytes,
    source_name: str,
    id_source: str,
    include_items: bool,
    strip_isbd_punctuation: bool,
    isbd_punctuation: str,
    relator_mappings: Optional[dict[str, str]],
    database_url: Optional[str],
    skip_ids: Optional[set[str]],
    format: str = "marcxml",
    constituents: Optional[Any] = None,
    task_id: Optional[str] = None,
) -> tuple[dict[str, Any], int, int, int]:
    """Parse raw XML page bytes and decompose MARCXML/BibFrame records in a worker process.

    Returns:
        (batch_payload_dict, decomposed_records_count, skipped_records_count, error_count)
    """
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
            source_id=source_name,
            reason=f"XMLSyntaxError in OAI page: {exc}",
            task_id=task_id,
        )
        return {}, 0, 0, 1

    records = root.findall(".//oai:record", OAI_NS)
    if not records:
        records = root.findall(".//record")

    vocab_store = _get_worker_vocab_store(database_url)

    ctx = LoadContext(
        source=source_name,
        record_origin=source_name,
        id_source=id_source,
        include_items=include_items,
        relator_mappings=relator_mappings,
        vocab_store=vocab_store,
        strip_isbd_punctuation=strip_isbd_punctuation,
        isbd_punctuation=isbd_punctuation,
        constituents=constituents,
    )

    acc = _WEMIBatchAccumulator(
        source_name=source_name,
        id_source=id_source,
        include_items=include_items,
        relator_mappings=relator_mappings,
        vocab_store=vocab_store,
        strip_isbd_punctuation=strip_isbd_punctuation,
        isbd_punctuation=isbd_punctuation,
        constituents=constituents,
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

        if skip_ids:
            raw_id = extract_marc_source_id(target_elem, id_source=id_source)
            if raw_id and raw_id in skip_ids:
                skipped_count += 1
                continue

        try:
            if format == "bibframe":
                raw_xml = etree.tostring(target_elem, encoding="utf-8")
                g = Graph()
                g.parse(data=raw_xml.decode("utf-8"), format="xml")
                item_to_load = g
            else:
                item_to_load = target_elem

            rec_decomp = load_record(item_to_load, ctx)
            acc.add_record(rec_decomp)
            decomposed_count += 1
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


def write_wemi_batch(
    repo: WEMIRepository,
    payload: dict[str, Any],
    chunk_size: int = 1000,
    config: Optional[OAIIngestConfig] = None,
    task_id: str = "",
    tracker: Optional[ProgressTracker] = None,
) -> int:
    """Persist a batch detached by `_WEMIBatchAccumulator.take()` using multi-row bulk upserts."""
    if config is not None and config.truncate_overlong_values:
        sanitize_batch_payload(payload, config=config, task_id=task_id, tracker=tracker)

    record_count = int(payload.get("record_count") or 0)
    record_origin = str(payload.get("record_origin") or "unknown")

    works = payload.get("works") or []
    agents = payload.get("agents") or []
    manifestations = payload.get("manifestations") or []
    expressions = payload.get("expressions") or []
    if not (works or agents or manifestations or expressions):
        return 0

    try:
        repo.insert_wemi_batch(
            works=works,
            expressions=payload.get("expressions") or [],
            manifestations=payload.get("manifestations") or [],
            items=payload.get("items") or [],
            agents=agents,
            relationships=payload.get("relationships") or [],
            identifiers=payload.get("identifiers") or [],
            manifestation_expressions=payload.get("associations") or [],
            contributions=payload.get("contributions") or [],
            entity_keys=payload.get("entity_keys") or [],
            entity_subjects=payload.get("entity_subjects") or [],
            record_origin=record_origin,
            chunk_size=chunk_size,
        )
    except Exception as exc:
        from sqlalchemy.exc import DataError

        if isinstance(exc, DataError) and config is not None and config.truncate_overlong_values:
            from reconciliator.logging import log_validation

            log_validation(
                logger,
                action="skip_batch",
                entity="wemi_batch",
                source_id=record_origin,
                reason=f"database DataError: {exc}",
                task_id=task_id,
                tracker=tracker,
            )
            return 0
        raise

    return record_count or (len(works) or len(agents))


async def _resolve_harvest_window(
    source_name: str,
    config: OAIIngestConfig,
) -> tuple[Optional[str], Optional[str]]:
    """Resolve the (from, until) window for a partitioned harvest."""
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
            max_retries=config.max_retries,
            base_delay=config.base_delay,
            http2=config.http2,
            prefetch=config.prefetch,
        )
        discovered = await harvester.get_earliest_datestamp()
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


def stream_file_raw_elements(
    source_name: str,
    config: OAIIngestConfig,
) -> Iterator[Any]:
    """Stream raw elements from a local MARCXML or Bibframe file."""
    if not config.input_path:
        raise ValueError(
            f"input_path must be specified for source '{source_name}' when source_type is 'file'"
        )
    if config.format == "bibframe":
        profile = get_profile(config.schema_profile) if config.schema_profile else None
        for _raw_rec, _offset, orig_doc in stream_bibframe_records(
            config.input_path, profile=profile, source=source_name
        ):
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
        f_in: Any
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


async def ingest_oai_pipeline(
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
    constituents_default_profile: Optional[str] = None,
    constituents_default_mode: str = "keys_only",
    config_dir: Optional[Path] = None,
) -> int:
    """Ingest a configured OAI/file source into WEMI storage using AsyncIO and ProcessPool."""
    is_oai = config.source_type == "oai"
    stage_name = "harvest_and_index" if is_oai else "indexing"

    store.register_source(source_name, is_authoritative=config.authoritative)

    existing_ids = store.get_indexed_source_ids(source=source_name) if skip_existing else set()
    if existing_ids:
        logger.info(
            "Skip-existing active: found %d already ingested records for source '%s'.",
            len(existing_ids),
            source_name,
        )

    source_constituents = resolve_for_source(
        source_name,
        config.constituents,
        config.constituents_mode,
        default_profile=constituents_default_profile,
        default_mode=constituents_default_mode,  # type: ignore[arg-type]
        config_dir=config_dir,
    )

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
            elif prior.status == "completed" and not config.from_date and prior.last_harvest_until:
                effective_config = config.model_copy(update={"from_date": prior.last_harvest_until})
                logger.info(
                    "Source '%s': incremental harvest from %s",
                    source_name,
                    prior.last_harvest_until,
                )

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

    from_date, until_date = await _resolve_harvest_window(source_name, effective_config)

    if from_date and until_date and effective_config.workers > 1:
        slices = partition_date_range(from_date, until_date, effective_config.slice_days)
    else:
        slices = [(from_date, until_date)]

    pending_slices = [s for s in slices if tuple(s) not in done_slices]
    partitioned = len(pending_slices) > 1 and effective_config.workers > 1

    if partitioned:
        initial_token = None

    harvested_count = 0
    skipped_count = 0
    total_ingested = 0
    finished_slices: list[tuple[Optional[str], Optional[str]]] = [
        (s[0], s[1]) for s in slices if tuple(s) in done_slices
    ]
    safe_token: Optional[str] = initial_token
    pending_tokens: list[tuple[int, Optional[str]]] = []
    delivered = 0

    def _telemetry() -> dict[str, Any]:
        data: dict[str, Any] = {
            "harvested": harvested_count,
            "indexed": total_ingested,
            "skipped": skipped_count,
            "source": source_name,
            "errors": tracker.error_count if tracker else 0,
        }
        if partitioned:
            data["slices_done"] = len(finished_slices)
            data["slices_total"] = len(slices)
        return data

    def _current_pct() -> Optional[float]:
        if partitioned and slices:
            return round((len(finished_slices) / len(slices)) * 100.0, 1)
        return None

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
                last_harvest_until=until_date or datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                resumption_token=token,
                records_harvested=already_harvested + total_ingested,
                status=status,
                completed_slices=slices_done or [],
            )
        )

    last_checkpoint_time = time.time()
    last_checkpoint_count = 0

    def _maybe_checkpoint(force: bool = False) -> None:
        nonlocal safe_token, last_checkpoint_time, last_checkpoint_count
        if not is_oai:
            return
        now = time.time()
        records_since = total_ingested - last_checkpoint_count
        time_since = now - last_checkpoint_time

        should_checkpoint = force or (
            records_since >= effective_config.checkpoint_interval_records
            or time_since >= effective_config.checkpoint_interval_seconds
        )
        if not should_checkpoint:
            return

        last_checkpoint_time = now
        last_checkpoint_count = total_ingested

        while pending_tokens and pending_tokens[0][0] <= total_ingested:
            safe_token = pending_tokens.pop(0)[1]

        if partitioned:
            _save_state("in_progress", None, list(finished_slices))
        else:
            _save_state("in_progress", safe_token, None)

    if tracker:
        tracker.start_task(stage=stage_name, metadata=_telemetry() if is_oai else None)

    loop = asyncio.get_running_loop()
    batch_queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=effective_config.batch_queue_size)
    sentinel = object()
    db_url = store.db_path

    async def db_writer() -> None:
        nonlocal total_ingested
        while True:
            payload = await batch_queue.get()
            if payload is sentinel:
                batch_queue.task_done()
                break

            try:
                if "slice_done" in payload:
                    finished_slices.append(payload["slice_done"])
                    _maybe_checkpoint(force=True)
                    if tracker:
                        tracker.update_progress(
                            stage=stage_name,
                            items_processed=total_ingested,
                            percentage=_current_pct(),
                            metadata=_telemetry(),
                        )
                elif "batch" in payload:
                    task_id = tracker.task_id if tracker else f"oai-ingest-{source_name}"
                    written = await loop.run_in_executor(
                        None,
                        write_wemi_batch,
                        store,
                        payload["batch"],
                        effective_config.db_batch_chunk_size,
                        effective_config,
                        task_id,
                        tracker,
                    )
                    total_ingested += written
                    snapshot = _telemetry()
                    _maybe_checkpoint(force=False)
                    if tracker:
                        tracker.update_progress(
                            stage=stage_name,
                            items_processed=snapshot["indexed"],
                            percentage=_current_pct(),
                            metadata=snapshot if is_oai else None,
                            force=True,
                        )
            finally:
                batch_queue.task_done()

    writer_task = asyncio.create_task(db_writer())

    async def enqueue_batch(payload: Any) -> None:
        while True:
            if writer_task.done():
                exc = writer_task.exception()
                if exc:
                    raise exc
                raise RuntimeError("db_writer task finished unexpectedly")
            try:
                await asyncio.wait_for(batch_queue.put(payload), timeout=1.0)
                break
            except asyncio.TimeoutError:
                continue

    heartbeat_task: Optional[asyncio.Task[None]] = None
    if tracker:
        interval = getattr(tracker, "heartbeat_interval", 1.0)
        if not isinstance(interval, (int, float)) or interval <= 0:
            interval = 1.0

        async def _heartbeat_runner() -> None:
            try:
                while True:
                    await asyncio.sleep(interval)
                    tracker.update_progress(
                        stage=stage_name,
                        items_processed=total_ingested,
                        percentage=_current_pct(),
                        metadata=_telemetry(),
                    )
            except asyncio.CancelledError:
                pass

        heartbeat_task = asyncio.create_task(_heartbeat_runner())

    try:
        if not is_oai:
            # File or ResourceSync source
            acc = _WEMIBatchAccumulator(
                source_name=source_name,
                id_source=effective_config.id_source,
                include_items=effective_config.include_items,
                relator_mappings=relator_mappings,
                vocab_store=vocab_store,
                strip_isbd_punctuation=strip_isbd_punctuation,
                isbd_punctuation=isbd_punctuation,
                constituents=source_constituents,
            )
            for item in stream_file_raw_elements(source_name, effective_config):
                harvested_count += 1
                if existing_ids:
                    raw_id = extract_marc_source_id(item, id_source=effective_config.id_source)
                    if raw_id and raw_id in existing_ids:
                        skipped_count += 1
                        continue
                acc.add(item)
                if acc.pending >= effective_config.batch_size:
                    await enqueue_batch({"batch": acc.take()})
            if acc.pending:
                await enqueue_batch({"batch": acc.take()})
        else:
            # OAI-PMH harvest with ProcessPoolExecutor and AsyncIO
            process_pool = ProcessPoolExecutor(max_workers=effective_config.workers)
            semaphore = asyncio.Semaphore(effective_config.max_concurrent_requests)

            async def harvest_partition_task(
                slice_from: Optional[str], slice_until: Optional[str]
            ) -> None:
                nonlocal harvested_count, skipped_count, delivered
                harvester = OAIPMHHarvester(
                    endpoint_url=effective_config.oai_url,  # type: ignore[arg-type]
                    metadata_prefix=effective_config.metadata_prefix,
                    set_spec=effective_config.set_spec,
                    timeout=effective_config.timeout,
                    request_delay=effective_config.request_delay,
                    max_retries=effective_config.max_retries,
                    base_delay=effective_config.base_delay,
                    http2=effective_config.http2,
                    prefetch=effective_config.prefetch,
                )
                async for page_bytes, next_token in harvester.harvest_partition(
                    from_date=slice_from,
                    until_date=slice_until,
                    resumption_token=initial_token if not partitioned else None,
                    semaphore=semaphore,
                ):
                    task_id = tracker.task_id if tracker else f"oai-ingest-{source_name}"
                    batch_payload, dec_cnt, sk_cnt, err_cnt = await loop.run_in_executor(
                        process_pool,
                        parse_and_decompose_page,
                        page_bytes,
                        source_name,
                        effective_config.id_source,
                        effective_config.include_items,
                        strip_isbd_punctuation,
                        isbd_punctuation,
                        relator_mappings,
                        db_url,
                        existing_ids if existing_ids else None,
                        effective_config.format,
                        source_constituents,
                        task_id,
                    )
                    if err_cnt > 0 and tracker:
                        tracker.record_error(n=err_cnt)
                    harvested_count += dec_cnt + sk_cnt
                    skipped_count += sk_cnt
                    delivered += dec_cnt

                    if not partitioned:
                        pending_tokens.append((delivered, next_token))

                    if batch_payload.get("record_count", 0) > 0 or any(
                        batch_payload.get(k)
                        for k in ("works", "agents", "expressions", "manifestations")
                    ):
                        await enqueue_batch({"batch": batch_payload})

                    if tracker:
                        tracker.update_progress(
                            stage=stage_name,
                            items_processed=total_ingested,
                            percentage=_current_pct(),
                            metadata=_telemetry(),
                        )

                await enqueue_batch({"slice_done": (slice_from, slice_until)})

            try:
                if pending_slices:
                    tasks = [
                        asyncio.create_task(harvest_partition_task(s_from, s_until))
                        for s_from, s_until in pending_slices
                    ]
                    results = await asyncio.gather(*tasks, return_exceptions=True)
                    for res in results:
                        if isinstance(res, Exception):
                            raise res
            finally:
                process_pool.shutdown(wait=False)

        await enqueue_batch(sentinel)
        await writer_task

        _maybe_checkpoint(force=True)
        if is_oai:
            _save_state("completed", None, None)

    except Exception as exc:
        try:
            if not writer_task.done():
                await batch_queue.put(sentinel)
                await writer_task
        except Exception:
            pass
        _maybe_checkpoint(force=True)
        if tracker:
            tracker.fail_task(error_message=str(exc))
        raise
    finally:
        if heartbeat_task:
            heartbeat_task.cancel()
            try:
                await heartbeat_task
            except asyncio.CancelledError:
                pass

    if skipped_count > 0:
        logger.info(
            "Source '%s': skipped %d already ingested records, ingested %d new/updated records.",
            source_name,
            skipped_count,
            total_ingested,
        )

    if tracker:
        tracker.complete_task(metadata=_telemetry())

    return total_ingested
