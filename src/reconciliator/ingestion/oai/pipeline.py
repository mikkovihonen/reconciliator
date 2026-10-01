"""End-to-end ingestion pipeline coordinating harvesting, parsing, and persistence."""

from __future__ import annotations

import asyncio
import logging
import time
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from reconciliator.config import OAIIngestConfig
from reconciliator.constituents import resolve_for_source
from reconciliator.database import WEMIRepository
from reconciliator.harvest.oai_pmh import OAIPMHHarvester, partition_date_range
from reconciliator.ingestion.oai.accumulator import WEMIBatchAccumulator
from reconciliator.ingestion.oai.parser import init_oai_worker, parse_and_decompose_page
from reconciliator.ingestion.oai.streaming import (
    resolve_harvest_window,
    stream_file_raw_elements,
)
from reconciliator.ingestion.oai.writer import write_wemi_batch
from reconciliator.loaders.marcxml import extract_marc_source_id
from reconciliator.models import HarvestState
from reconciliator.progress import ProgressTracker

logger = logging.getLogger(__name__)


async def ingest_oai_pipeline(
    source_name: str,
    config: OAIIngestConfig,
    store: WEMIRepository,
    tracker: Optional[ProgressTracker] = None,
    skip_existing: bool = False,
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
    indexes_dropped = False
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
        if getattr(effective_config, "drop_indexes_during_replace", False):
            logger.info(
                "Source '%s': dropping search indexes during bulk replace ingest...",
                source_name,
            )
            store.drop_search_indexes()
            indexes_dropped = True
    elif config.ingest_mode == "replace" and is_continuation:
        logger.info(
            "Source '%s': ingest_mode=replace skipped - this run continues an earlier "
            "harvest, and clearing would delete what it already wrote",
            source_name,
        )

    from_date, until_date = await resolve_harvest_window(source_name, effective_config)

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
            "harvested": already_harvested + harvested_count,
            "indexed": already_harvested + total_ingested,
            "session_harvested": harvested_count,
            "session_indexed": total_ingested,
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
        tracker.start_task(
            stage=stage_name,
            items_processed=already_harvested,
            metadata=_telemetry() if is_oai else None,
        )

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
                            items_processed=already_harvested + total_ingested,
                            percentage=_current_pct(),
                            metadata=_telemetry(),
                            force=False,
                        )
                elif "batch" in payload:
                    task_id = tracker.task_id if tracker else f"oai-ingest-{source_name}"
                    written = await loop.run_in_executor(
                        None,
                        write_wemi_batch,
                        store,
                        payload["batch"],
                        None,
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
                            force=False,
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
                        items_processed=already_harvested + total_ingested,
                        percentage=_current_pct(),
                        metadata=_telemetry(),
                    )
            except asyncio.CancelledError:
                pass

        heartbeat_task = asyncio.create_task(_heartbeat_runner())

    try:
        if not is_oai:
            acc = WEMIBatchAccumulator(
                source_name=source_name,
                id_source=effective_config.id_source,
                include_items=effective_config.include_items,
                vocab_store=vocab_store,
                strip_isbd_punctuation=strip_isbd_punctuation,
                isbd_punctuation=isbd_punctuation,
                constituents=source_constituents,
                filter_config=effective_config,
            )
            for item in stream_file_raw_elements(source_name, effective_config):
                harvested_count += 1
                if existing_ids:
                    raw_id = extract_marc_source_id(item, id_source=effective_config.id_source)
                    if raw_id and raw_id in existing_ids:
                        skipped_count += 1
                        continue
                if not acc.add(item):
                    skipped_count += 1
                    continue
                if acc.pending >= effective_config.batch_size:
                    await enqueue_batch({"batch": acc.take()})
            if acc.pending:
                await enqueue_batch({"batch": acc.take()})
        else:
            worker_ctx = {
                "source_name": source_name,
                "id_source": effective_config.id_source,
                "include_items": effective_config.include_items,
                "strip_isbd_punctuation": strip_isbd_punctuation,
                "isbd_punctuation": isbd_punctuation,
                "db_url": db_url,
                "skip_ids": existing_ids if existing_ids else None,
                "format": effective_config.format,
                "constituents": source_constituents,
                "filter_config": effective_config,
            }
            process_pool = ProcessPoolExecutor(
                max_workers=effective_config.workers,
                initializer=init_oai_worker,
                initargs=(worker_ctx,),
            )
            semaphore = asyncio.Semaphore(effective_config.max_concurrent_requests)

            async def harvest_partition_task(
                slice_from: Optional[str],
                slice_until: Optional[str],
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
                    batch_payload, dec_cnt, sk_cnt, err_cnt = await loop.run_in_executor(
                        process_pool,
                        parse_and_decompose_page,
                        page_bytes,
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
                            items_processed=already_harvested + total_ingested,
                            percentage=_current_pct(),
                            metadata=_telemetry(),
                            force=False,
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

        if indexes_dropped:
            logger.info(
                "Source '%s': rebuilding search indexes after bulk replace ingest...",
                source_name,
            )
            store.rebuild_search_indexes()

        _maybe_checkpoint(force=True)
        if is_oai:
            _save_state("completed", None, None)

    except Exception as exc:
        if indexes_dropped:
            try:
                store.rebuild_search_indexes()
            except Exception:
                pass
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
