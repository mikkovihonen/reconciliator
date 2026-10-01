"""Worker threads and streaming queue pipeline for reconciliation workflows."""

from __future__ import annotations

import json
import logging
import queue
import threading
from itertools import islice
from typing import IO, Any, Callable, Iterator, Optional

from lxml import etree

from reconciliator.config import AppConfig, ReconcileConfig
from reconciliator.database import WEMIRepository
from reconciliator.enrichment.bibframe import (
    enrich_bibframe_graph,
    enrich_bibframe_jsonld,
)
from reconciliator.enrichment.marcxml import MARCXMLEnricher
from reconciliator.loaders.bibframe import BIBFRAMESchemaProfile
from reconciliator.loaders.bibra import enrich_bibra_json
from reconciliator.models import (
    CorpusLinkage,
    ExpressionCluster,
    ManifestationCluster,
    WorkCluster,
)
from reconciliator.normalizers.record import RecordNormalizer
from reconciliator.progress import ProgressTracker
from reconciliator.reconciliation.matcher import WorkMatcher
from reconciliator.reconciliation.pipeline.formatting import (
    make_agent_report_row,
    to_target_record,
)
from reconciliator.reports import (
    ExpressionReportRow,
    ReportGenerator,
    SubjectReportRow,
    WorkReportRow,
)
from reconciliator.utils.curie import extract_wikidata_qid

logger = logging.getLogger(__name__)


def chunked_iter(iterable: Iterator[Any], size: int) -> Iterator[list[Any]]:
    """Yield successive chunks of size `size` from an iterator."""
    it = iter(iterable)
    while True:
        chunk = list(islice(it, size))
        if not chunk:
            break
        yield chunk


def process_output_item(
    rec_fmt: str,
    orig_payload: Any,
    raw_rec: dict[str, Any],
    byte_offset: int,
    target_record: Any,
    target_wemi: Any,
    wemi_res: Any,
    res: Any,
    *,
    reporter: ReportGenerator,
    r_cfg: ReconcileConfig,
    cfg: AppConfig,
    linkage_map: dict[str, CorpusLinkage],
    work_cluster_map: dict[str, WorkCluster],
    expression_cluster_map: dict[str, ExpressionCluster],
    manifestation_cluster_map: dict[str, ManifestationCluster],
    out_f: Optional[IO[bytes]],
    bf_profile: Optional[BIBFRAMESchemaProfile],
    wemi_repo: WEMIRepository,
    enricher: MARCXMLEnricher,
    tracker: ProgressTracker,
) -> None:
    """Format, enrich, report, and serialize a single reconciled record."""
    if res.accepted_candidates:
        reporter.matched_records += 1

    best_match = res.matches[0] if res.matches else None
    match_tier = best_match.match_tier if best_match else "unmatched"
    match_score = best_match.match_score if best_match else 0.0
    dense_score = best_match.dense_score if best_match else None
    rerank_score = best_match.rerank_score if best_match else None

    for idx, agent in enumerate(target_record.agents):
        selected = res.selected_agent_uris.get(idx, [])
        suppressed = res.suppressed_agent_uris.get(idx, [])
        existing = set(agent.uri_subfield_0 + agent.uri_subfield_1)
        discovered_map = {u.uri: u for u in res.discovered_agent_uris.get(idx, [])}

        field_tag = agent.field_tag
        if not field_tag:
            if agent.is_personal:
                field_tag = "100" if idx == 0 else "700"
            else:
                field_tag = "110" if idx == 0 else "710"

        for uri in selected:
            is_novel = uri not in existing
            action = (
                "injected"
                if (r_cfg.enrich and is_novel)
                else ("existing" if not is_novel else "proposed")
            )
            reporter.add_agent_row(
                make_agent_report_row(
                    target_record,
                    agent,
                    field_tag,
                    idx + 1,
                    uri=uri,
                    action=action,
                    is_novel=is_novel,
                    discovered_map=discovered_map,
                    accepted_candidates=res.accepted_candidates,
                    match_tier=match_tier,
                    match_score=match_score,
                    dense_score=dense_score,
                    rerank_score=rerank_score,
                )
            )

        for uri in suppressed:
            reporter.add_agent_row(
                make_agent_report_row(
                    target_record,
                    agent,
                    field_tag,
                    idx + 1,
                    uri=uri,
                    action="suppressed_by_priority",
                    is_novel=True,
                    discovered_map=discovered_map,
                    accepted_candidates=res.accepted_candidates,
                    match_tier=match_tier,
                    match_score=match_score,
                    dense_score=dense_score,
                    rerank_score=rerank_score,
                )
            )

        if not selected and not suppressed:
            reporter.add_agent_row(
                make_agent_report_row(
                    target_record,
                    agent,
                    field_tag,
                    idx + 1,
                    action="unmatched",
                    is_novel=False,
                    match_tier=match_tier,
                    match_score=match_score,
                    dense_score=dense_score,
                    rerank_score=rerank_score,
                )
            )

    all_subjects = target_record.subjects.all_items()
    for s_idx, (default_tag, sub_item) in enumerate(all_subjects):
        s_tag = sub_item.source_field or default_tag
        t_tag = sub_item.target_field or default_tag
        s_vocab = sub_item.source_vocab or ""
        s_uri = sub_item.source_uri or ""
        t_vocab = sub_item.vocabulary or ""
        t_uri = sub_item.uri or ""
        s_heading = sub_item.source_heading or sub_item.pref_label
        m_type = sub_item.mapping_type or "pref_label_lookup"
        tag_changed = s_tag != t_tag

        if not t_uri:
            action = "unmapped"
            is_novel = False
        elif s_uri and s_uri == t_uri:
            action = "existing"
            is_novel = False
        elif r_cfg.enrich:
            action = "injected"
            is_novel = True
        else:
            action = "proposed"
            is_novel = True

        reporter.add_subject_row(
            SubjectReportRow(
                target_record_id=target_record.source_id,
                target_title=target_record.title,
                source_field_tag=s_tag,
                field_occurrence=s_idx + 1,
                source_heading=s_heading,
                source_vocab=s_vocab,
                source_uri=s_uri,
                language=target_record.language or cfg.linguistic.default_language,
                target_field_tag=t_tag,
                target_vocab=t_vocab,
                target_uri=t_uri,
                target_pref_label=sub_item.pref_label,
                mapping_type=m_type,
                action=action,
                is_novel=is_novel,
                tag_changed=tag_changed,
            )
        )

    linkage = linkage_map.get(target_wemi.record_id)
    w_cluster = work_cluster_map.get(linkage.work_cluster_id) if linkage else None
    e_cluster = expression_cluster_map.get(linkage.expression_cluster_id) if linkage else None
    m_cluster = (
        manifestation_cluster_map.get(linkage.manifestation_cluster_id) if linkage else None
    )

    is_clustered = linkage is not None and linkage.work_cluster_id is not None
    record_level_w_uri = wemi_res.work_uri if wemi_res else None
    matched_w_uri = (
        (w_cluster.canonical_work_uri if w_cluster else None)
        or (linkage.final_work_uri if linkage else None)
        or record_level_w_uri
    )
    qid = None
    if wemi_res and wemi_res.matched_work and wemi_res.matched_work.source == "wikidata":
        qid = wemi_res.matched_work.source_id
    if not qid and matched_w_uri:
        qid = extract_wikidata_qid(matched_w_uri)
    w_action = (
        "injected"
        if (r_cfg.enrich and matched_w_uri)
        else ("proposed" if matched_w_uri else "unmatched")
    )
    if is_clustered and not (wemi_res and wemi_res.matched_work):
        w_action = (
            "hoisted"
            if (
                w_cluster
                and w_cluster.canonical_work_uri
                and not w_cluster.canonical_work_uri.startswith("urn:reconciliator:")
            )
            else "minted"
        )

    reporter.add_work_row(
        WorkReportRow(
            target_record_id=target_wemi.record_id,
            work_type=target_wemi.work_type,
            target_title=target_wemi.title,
            target_author=target_wemi.author_key,
            target_year=target_wemi.year,
            work_blocking_key=target_wemi.work_blocking_key,
            matched_work_uri=matched_w_uri,
            wikidata_qid=qid,
            match_tier=wemi_res.match_tier if wemi_res else "unmatched",
            funnel_used=wemi_res.funnel_used if wemi_res else "none",
            confidence_score=wemi_res.confidence_score if wemi_res else 0.0,
            is_clustered=is_clustered,
            cluster_id=linkage.work_cluster_id if linkage else None,
            action=w_action,
            record_level_work_uri=record_level_w_uri,
        )
    )

    matched_e_uri = (
        (wemi_res.expression_uri if wemi_res else None)
        or (linkage.final_expression_uri if linkage else None)
        or (e_cluster.canonical_expression_uri if e_cluster else None)
    )
    e_action = (
        "injected"
        if (r_cfg.enrich and matched_e_uri)
        else ("proposed" if matched_e_uri else "unmatched")
    )
    e_content_type = (
        (
            wemi_res.matched_expression.content_type
            if (wemi_res and wemi_res.matched_expression)
            else None
        )
        or (e_cluster.content_type if e_cluster else None)
        or "text"
    )
    e_lang = (
        (
            wemi_res.matched_expression.language
            if (
                wemi_res
                and wemi_res.matched_expression
                and wemi_res.matched_expression.language
            )
            else None
        )
        or (e_cluster.language if e_cluster else None)
        or target_wemi.language
        or "und"
    )
    e_version = (
        (
            wemi_res.matched_expression.version_designation
            if (wemi_res and wemi_res.matched_expression)
            else None
        )
        or (e_cluster.version_designation if e_cluster else None)
        or target_wemi.volume_designation
    )

    reporter.add_expression_row(
        ExpressionReportRow(
            target_record_id=target_wemi.record_id,
            work_uri=matched_w_uri,
            expression_uri=matched_e_uri,
            content_type=e_content_type,
            language=e_lang,
            version_designation=e_version,
            translators=None,
            performers=None,
            match_tier=wemi_res.match_tier if wemi_res else "unmatched",
            confidence_score=wemi_res.confidence_score if wemi_res else 0.0,
            cluster_id=linkage.expression_cluster_id if linkage else None,
            action=e_action,
        )
    )

    if out_f is not None:
        if rec_fmt == "bibra":
            enriched_json = enrich_bibra_json(orig_payload, res, target_record=target_record)
            out_f.write(json.dumps(enriched_json, ensure_ascii=False).encode("utf-8"))
            out_f.write(b"\n")
        elif rec_fmt == "bibframe_jsonld":
            enriched_doc = enrich_bibframe_jsonld(
                orig_payload,
                res,
                profile=bf_profile,
                target_record=target_record,
                enrich=r_cfg.enrich,
                fold_legacy=r_cfg.fold_legacy,
                wemi_result=wemi_res,
                linkage=linkage,
                work_cluster=w_cluster,
                expression_cluster=e_cluster,
                manifestation_cluster=m_cluster,
                enrichment_config=r_cfg.enrichment,
            )
            out_f.write(json.dumps(enriched_doc, ensure_ascii=False).encode("utf-8"))
            out_f.write(b"\n")
        elif rec_fmt == "marcxml":
            if not r_cfg.enrich:
                if isinstance(orig_payload, bytes):
                    out_f.write(orig_payload)
                elif isinstance(orig_payload, str):
                    out_f.write(orig_payload.encode("utf-8"))
                else:
                    out_f.write(
                        etree.tostring(orig_payload, encoding="utf-8", pretty_print=True)
                    )
                out_f.write(b"\n")
            else:
                sibs = (
                    wemi_repo.get_sibling_manifestation_record_ids(target_wemi.record_id)
                    if is_clustered
                    else []
                )
                payload_elem = (
                    etree.fromstring(orig_payload)
                    if isinstance(orig_payload, (bytes, str))
                    else orig_payload
                )
                enricher.enrich_record_tree(
                    payload_elem,
                    res,
                    enrich=r_cfg.enrich,
                    fold_legacy=r_cfg.fold_legacy,
                    wemi_result=wemi_res,
                    linkage=linkage,
                    work_cluster=w_cluster,
                    expression_cluster=e_cluster,
                    manifestation_cluster=m_cluster,
                    target_record=target_wemi,
                    enrichment_config=r_cfg.enrichment,
                    sibling_ids=sibs,
                )
                out_f.write(etree.tostring(payload_elem, encoding="utf-8", pretty_print=True))
                out_f.write(b"\n")
    elif rec_fmt == "bibframe_rdf":
        g, nodes = orig_payload
        enrich_bibframe_graph(
            g,
            nodes,
            res,
            profile=bf_profile,
            target_record=target_record,
            enrich=r_cfg.enrich,
            fold_legacy=r_cfg.fold_legacy,
            wemi_result=wemi_res,
            linkage=linkage,
            work_cluster=w_cluster,
            expression_cluster=e_cluster,
            manifestation_cluster=m_cluster,
            enrichment_config=r_cfg.enrichment,
        )

    tracker.update_progress(
        items_increment=1,
        bytes_processed=byte_offset,
    )


def run_pipeline_workers(
    record_iter_fn: Callable[[], Iterator[tuple[str, Any, dict[str, Any], int]]],
    normalizer: RecordNormalizer,
    work_matcher: WorkMatcher,
    bf_profile: Optional[BIBFRAMESchemaProfile],
    target_const_prof: Optional[Any],
    reconcile_batch_size: int,
    queue_cap: int,
    output_processor: Callable[..., None],
) -> int:
    """Execute streaming reader-worker-writer pipeline across worker threads."""
    in_queue: queue.Queue[Any] = queue.Queue(maxsize=queue_cap)
    out_queue: queue.Queue[Any] = queue.Queue(maxsize=queue_cap)
    stop_event = threading.Event()
    reader_error: list[Exception] = []
    writer_error: list[Exception] = []
    total_processed = 0

    def _reader_worker() -> None:
        try:
            for batch in chunked_iter(record_iter_fn(), reconcile_batch_size):
                if stop_event.is_set():
                    break
                batch_items = []
                wemi_targets = []
                for rec_fmt, orig_payload, raw_rec, byte_offset in batch:
                    target_record = normalizer.normalize(raw_rec)
                    target_wemi = to_target_record(
                        rec_fmt,
                        orig_payload,
                        raw_rec,
                        bf_profile,
                        constituent_profile=target_const_prof,
                    )
                    batch_items.append(
                        (
                            rec_fmt,
                            orig_payload,
                            raw_rec,
                            byte_offset,
                            target_record,
                            target_wemi,
                        )
                    )
                    wemi_targets.append(target_wemi)
                while not stop_event.is_set():
                    try:
                        in_queue.put((batch_items, wemi_targets), timeout=0.1)
                        break
                    except queue.Full:
                        continue
            while not stop_event.is_set():
                try:
                    in_queue.put(None, timeout=0.1)
                    break
                except queue.Full:
                    continue
        except Exception as exc:
            reader_error.append(exc)
            while not stop_event.is_set():
                try:
                    in_queue.put(None, timeout=0.1)
                    break
                except queue.Full:
                    continue

    def _writer_worker() -> None:
        nonlocal total_processed
        try:
            while True:
                while not stop_event.is_set():
                    try:
                        item = out_queue.get(timeout=0.1)
                        break
                    except queue.Empty:
                        continue
                else:
                    break
                if item is None:
                    out_queue.task_done()
                    break
                for rec_item in item:
                    if stop_event.is_set():
                        break
                    output_processor(*rec_item)
                    total_processed += 1
                out_queue.task_done()
        except Exception as exc:
            writer_error.append(exc)

    reader_thread = threading.Thread(target=_reader_worker, daemon=True, name="ReconcileReader")
    writer_thread = threading.Thread(target=_writer_worker, daemon=True, name="ReconcileWriter")
    reader_thread.start()
    writer_thread.start()

    try:
        while not stop_event.is_set():
            try:
                batch_data = in_queue.get(timeout=0.1)
            except queue.Empty:
                if reader_error:
                    raise reader_error[0]
                if writer_error:
                    raise writer_error[0]
                continue

            if batch_data is None:
                if reader_error:
                    raise reader_error[0]
                break
            batch_items, wemi_targets = batch_data
            wemi_results = work_matcher.match_targets_batch(wemi_targets)

            processed_batch = []
            for (
                rec_fmt,
                orig_payload,
                raw_rec,
                byte_offset,
                target_record,
                target_wemi,
            ), wemi_res in zip(batch_items, wemi_results):
                res = work_matcher.resolve_agents(wemi_res, target_record)
                processed_batch.append(
                    (
                        rec_fmt,
                        orig_payload,
                        raw_rec,
                        byte_offset,
                        target_record,
                        target_wemi,
                        wemi_res,
                        res,
                    )
                )

            if writer_error:
                raise writer_error[0]

            while not stop_event.is_set():
                try:
                    out_queue.put(processed_batch, timeout=0.1)
                    break
                except queue.Full:
                    if writer_error:
                        raise writer_error[0]
                    continue

        while not stop_event.is_set():
            try:
                out_queue.put(None, timeout=0.1)
                break
            except queue.Full:
                if writer_error:
                    raise writer_error[0]
                continue

        writer_thread.join()
        if writer_error:
            raise writer_error[0]
    except Exception:
        stop_event.set()
        try:
            while not in_queue.empty():
                in_queue.get_nowait()
        except queue.Empty:
            pass
        try:
            while not out_queue.empty():
                out_queue.get_nowait()
        except queue.Empty:
            pass
        raise
    finally:
        stop_event.set()
        reader_thread.join(timeout=2.0)
        writer_thread.join(timeout=2.0)

    return total_processed
