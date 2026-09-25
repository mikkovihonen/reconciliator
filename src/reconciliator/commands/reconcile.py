"""Reconciliation and corpus alignment commands."""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
from itertools import islice
from pathlib import Path
from typing import IO, Any, Iterator, Optional

import typer
from lxml import etree
from rdflib import Graph

from reconciliator.clustering import CorpusClusterer
from reconciliator.commands.common import (
    _resolve_cli_config,
    _wemi_repo,
    console,
    logger,
)
from reconciliator.config import ReconcileConfig, configure_logging, load_config
from reconciliator.enrichment.bibframe import (
    enrich_bibframe_graph,
    enrich_bibframe_jsonld,
)
from reconciliator.enrichment.marcxml import MARCXMLEnricher
from reconciliator.ingestion.sanitizer import sanitize_batch_payload
from reconciliator.loaders.bibframe import (
    BIBFRAMESchemaProfile,
    build_target_record_from_bibframe,
    get_profile,
    iter_bibframe_records,
    iter_jsonld_bibframe_records,
)
from reconciliator.loaders.bibra import enrich_bibra_json, iter_bibra_records
from reconciliator.loaders.marcxml import (
    build_target_record_from_marc,
    iter_marc_record_elements,
    parse_marc_record_elem,
    parse_marc_xml_str_fast,
)
from reconciliator.models import (
    CorpusLinkage,
    ExpressionCluster,
    ManifestationCluster,
    MatchRecord,
    WorkCluster,
)
from reconciliator.normalizers.blocking import (
    compute_author_key,
    compute_title_stem,
    compute_work_blocking_key,
)
from reconciliator.normalizers.record import RecordNormalizer
from reconciliator.progress import ProgressTracker
from reconciliator.reconciliation import WorkMatcher
from reconciliator.reports import (
    AgentReportRow,
    ExpressionReportRow,
    ReportGenerator,
    SubjectReportRow,
    WorkReportRow,
)
from reconciliator.utils.curie import extract_wikidata_qid
from reconciliator.vocab import VocabFolder, VocabStore, detect_vocabulary


def _ingest_corpus_as_wemi(
    repo: Any,
    records: Any,
    source_name: str,
    profile: Optional[BIBFRAMESchemaProfile] = None,
    strip_isbd_punctuation: bool = True,
    isbd_punctuation: str = " ./,;:",
    batch_size: int = 500,
    config: Optional[ReconcileConfig] = None,
    tracker: Optional[Any] = None,
) -> int:
    """Persist the corpus being reconciled as ordinary WEMI rows.

    Replaces the `target_records` staging path. Every ingested record, corpus or
    authority, becomes works/expressions/manifestations plus identifiers and
    entity_keys; `authoritative: false` on the source is what keeps a corpus from
    being matched against as if it were authority data.
    """
    from reconciliator.constituents import get_constituent_profile
    from reconciliator.loaders.aggregates import build_embodied_constituents
    from reconciliator.loaders.bibframe import bibframe_entity_keys, decompose_bibframe_work
    from reconciliator.loaders.marcxml import decompose_marc_wemi, extract_marc_constituents

    total = 0
    batch: dict[str, list[Any]] = {}

    const_cfg = config.constituents if config else None
    const_mode = const_cfg.mode if const_cfg else "entities"
    marc_const_prof = None
    if const_cfg and const_mode != "off":
        try:
            marc_const_prof = get_constituent_profile(const_cfg.profile or "marc_enhanced")
        except Exception as e:
            logger.warning("Could not load MARC constituent profile: %s", e)

    bf_const_prof = None
    if const_cfg and const_mode != "off":
        try:
            if profile and profile.name == "bffi_1":
                bf_const_prof = get_constituent_profile(const_cfg.bffi_profile or "bffi_haspart")
            else:
                bf_const_prof = get_constituent_profile(
                    const_cfg.bibframe_profile or "bibframe_haspart"
                )
        except Exception as e:
            logger.warning("Could not load BIBFRAME constituent profile: %s", e)

    def _flush() -> None:
        if not batch.get("works"):
            return

        task_id = getattr(tracker, "task_id", "") or f"corpus-ingest-{source_name}"
        if config is not None and config.truncate_overlong_values:
            batch["record_origin"] = source_name
            sanitize_batch_payload(batch, config=config, task_id=task_id, tracker=tracker)

        try:
            repo.insert_wemi_batch(
                works=batch.get("works") or [],
                expressions=batch.get("expressions") or [],
                manifestations=batch.get("manifestations") or [],
                agents=batch.get("agents") or [],
                relationships=batch.get("relationships") or [],
                identifiers=batch.get("identifiers") or [],
                manifestation_expressions=batch.get("associations") or [],
                contributions=batch.get("contributions") or [],
                entity_keys=batch.get("entity_keys") or [],
                entity_subjects=batch.get("entity_subjects") or [],
                record_origin=source_name,
            )
        except Exception as exc:
            from sqlalchemy.exc import DataError

            if (
                isinstance(exc, DataError)
                and config is not None
                and config.truncate_overlong_values
            ):
                from reconciliator.logging import log_validation

                log_validation(
                    logging.getLogger(__name__),
                    action="skip_batch",
                    entity="corpus_wemi_batch",
                    source_id=source_name,
                    reason=f"database DataError: {exc}",
                    task_id=task_id,
                    tracker=tracker,
                )
                batch.clear()
                return
            raise

        batch.clear()

    def _stage(key: str, values: Any) -> None:
        batch.setdefault(key, []).extend(values)

    for rec_fmt, orig_payload, _raw_rec, _offset in records:
        keys: list[Any] = []
        subjs: list[Any] = []
        try:
            if rec_fmt == "marcxml":
                fast_rec = None
                if isinstance(orig_payload, (bytes, str)):
                    try:
                        xml_str = (
                            orig_payload.decode("utf-8")
                            if isinstance(orig_payload, bytes)
                            else orig_payload
                        )
                        parsed = parse_marc_xml_str_fast(xml_str)
                        if parsed:
                            fast_rec = parsed[0]
                    except Exception:
                        fast_rec = None
                payload_elem = (
                    fast_rec
                    if fast_rec is not None
                    else (
                        etree.fromstring(orig_payload)
                        if isinstance(orig_payload, (bytes, str))
                        else orig_payload
                    )
                )
                w, e, m, itms, agts, rels, idents, assoc, contribs = decompose_marc_wemi(
                    payload_elem,
                    source=source_name,
                    record_origin=source_name,
                    strip_isbd_punctuation=strip_isbd_punctuation,
                    isbd_punctuation=isbd_punctuation,
                    key_sink=keys,
                    subject_sink=subjs,
                )
                if const_mode == "entities" and marc_const_prof:
                    consts = extract_marc_constituents(payload_elem, marc_const_prof)
                    if consts:
                        c_batch = build_embodied_constituents(
                            container_work=w,
                            container_expression=e,
                            container_manifestation=m,
                            constituents=consts,
                            source=source_name,
                            record_origin=source_name,
                            language=e.language,
                            content_type=e.content_type,
                            work_type=w.work_type,
                            container_creator=w.title_normalized or None,
                            relationship_type="has_part",
                        )
                        _stage("works", c_batch.works)
                        _stage("expressions", c_batch.expressions)
                        _stage("associations", c_batch.associations)
                        _stage("relationships", c_batch.relationships)
            elif rec_fmt in ("bibframe_rdf", "bibframe_jsonld"):
                if rec_fmt == "bibframe_rdf":
                    g, nodes = orig_payload
                    node = nodes[0] if nodes else None
                else:
                    g = Graph()
                    g.parse(data=json.dumps(orig_payload), format="json-ld")
                    node = None
                constituent_sink: list[Any] = []
                w, e, m, itms, agts, rels, idents, assoc, contribs = decompose_bibframe_work(
                    g=g,
                    instance_node=node,
                    profile=profile,
                    source=source_name,
                    subject_sink=subjs,
                    constituent_profile=bf_const_prof if const_mode != "off" else None,
                    constituent_sink=constituent_sink if const_mode != "off" else None,
                )
                keys = bibframe_entity_keys(m, w, agts, idents, constituents=constituent_sink)
                if const_mode == "entities" and constituent_sink:
                    c_batch = build_embodied_constituents(
                        container_work=w,
                        container_expression=e,
                        container_manifestation=m,
                        constituents=constituent_sink,
                        source=source_name,
                        record_origin=source_name,
                        language=e.language,
                        content_type=e.content_type,
                        work_type=w.work_type,
                        container_creator=w.title_normalized or None,
                        relationship_type="has_part",
                    )
                    _stage("works", c_batch.works)
                    _stage("expressions", c_batch.expressions)
                    _stage("associations", c_batch.associations)
                    _stage("relationships", c_batch.relationships)
            else:
                continue
        except Exception:
            continue

        _stage("works", [w])
        _stage("expressions", [e])
        _stage("manifestations", [m])
        _stage("items", itms)
        _stage("agents", agts)
        _stage("relationships", rels)
        _stage("identifiers", idents)
        _stage("associations", assoc)
        _stage("contributions", contribs)
        _stage("entity_keys", keys)
        _stage("entity_subjects", subjs)
        total += 1
        if total % batch_size == 0:
            _flush()

    _flush()
    return total


def _to_target_record(
    rec_fmt: str,
    orig_payload: Any,
    raw_rec: dict[str, Any],
    profile: Optional[BIBFRAMESchemaProfile] = None,
    constituent_profile: Optional[Any] = None,
) -> MatchRecord:
    """Decompose or map raw bibliographic items into normalized MatchRecord instances."""
    try:
        if rec_fmt == "marcxml":
            fast_rec = None
            if isinstance(orig_payload, (bytes, str)):
                try:
                    xml_str = (
                        orig_payload.decode("utf-8")
                        if isinstance(orig_payload, bytes)
                        else orig_payload
                    )
                    parsed = parse_marc_xml_str_fast(xml_str)
                    if parsed:
                        fast_rec = parsed[0]
                except Exception:
                    fast_rec = None
            payload_elem = (
                fast_rec
                if fast_rec is not None
                else (
                    etree.fromstring(orig_payload)
                    if isinstance(orig_payload, (bytes, str))
                    else orig_payload
                )
            )
            return build_target_record_from_marc(payload_elem, source="target")
        elif rec_fmt == "bibframe_rdf":
            g, nodes = orig_payload
            instance_node = nodes[0] if nodes else None
            return build_target_record_from_bibframe(
                g,
                instance_node=instance_node,
                profile=profile,
                source="target",
                constituent_profile=constituent_profile,
            )
        elif rec_fmt == "bibframe_jsonld":
            g = Graph()
            g.parse(data=json.dumps(orig_payload), format="json-ld")
            return build_target_record_from_bibframe(
                g, profile=profile, source="target", constituent_profile=constituent_profile
            )
    except Exception:
        pass

    source_id = raw_rec.get("source_id") or "unknown_target"
    title = raw_rec.get("title") or ""
    agents = raw_rec.get("agents") or []
    author_name = agents[0].get("name") if agents else None
    author_surname = agents[0].get("surname") if agents else None
    author_key = compute_author_key(author_name, author_surname)
    title_stem = compute_title_stem(title)
    work_blocking_key = compute_work_blocking_key(author_key, title_stem)

    if isinstance(orig_payload, (dict, list)):
        raw_bytes = json.dumps(orig_payload, ensure_ascii=False).encode("utf-8")
    elif hasattr(orig_payload, "tag"):
        raw_bytes = etree.tostring(orig_payload, encoding="utf-8")
    else:
        raw_bytes = str(orig_payload).encode("utf-8")

    return MatchRecord(
        record_id=source_id,
        format=rec_fmt,
        work_type="text",
        title=title,
        title_stem=title_stem,
        author_key=author_key,
        work_blocking_key=work_blocking_key,
        raw_payload=raw_bytes,
    )


def _make_agent_report_row(
    target_record: Any,
    agent: Any,
    field_tag: str,
    field_occurrence: int,
    uri: str = "",
    action: str = "unmatched",
    is_novel: bool = False,
    discovered_map: Optional[dict[str, Any]] = None,
    accepted_candidates: Optional[list[Any]] = None,
    match_tier: str = "TIER_0",
    match_score: float = 0.0,
    dense_score: Optional[float] = None,
    rerank_score: Optional[float] = None,
) -> AgentReportRow:
    target_isbn = (
        target_record.identifiers.isbn[0]
        if getattr(target_record, "identifiers", None) and target_record.identifiers.isbn
        else None
    )
    target_role = (
        ", ".join(agent.role_counts.keys()) if getattr(agent, "role_counts", None) else None
    )

    if not uri:
        return AgentReportRow(
            target_record_id=target_record.source_id,
            target_title=target_record.title,
            target_year=target_record.publication_year,
            target_isbn=target_isbn,
            field_tag=field_tag,
            field_occurrence=field_occurrence,
            subfield_code="0",
            target_heading=agent.name,
            target_role=target_role,
            uri="",
            vocabulary="",
            authority_label=None,
            authority_record_id="",
            authority_source="",
            match_tier=match_tier,
            match_score=match_score,
            dense_score=dense_score,
            rerank_score=rerank_score,
            agent_name_score=None,
            action=action,
            is_novel=is_novel,
            dates=agent.dates,
            fuller_name=agent.fuller_name,
            birth_year=agent.birth_year,
            death_year=agent.death_year,
        )

    subfield_code = "1" if "wikidata.org" in uri.lower() else "0"
    disc_uri = discovered_map.get(uri) if discovered_map else None
    cand_source_id = (
        disc_uri.source_record_id
        if disc_uri
        else (accepted_candidates[0].source_id if accepted_candidates else "")
    )
    cand_source = (
        disc_uri.source
        if disc_uri
        else (accepted_candidates[0].source if accepted_candidates else "")
    )
    agent_name_score = disc_uri.alignment_score if disc_uri else 100.0

    return AgentReportRow(
        target_record_id=target_record.source_id,
        target_title=target_record.title,
        target_year=target_record.publication_year,
        target_isbn=target_isbn,
        field_tag=field_tag,
        field_occurrence=field_occurrence,
        subfield_code=subfield_code,
        target_heading=agent.name,
        target_role=target_role,
        uri=uri,
        vocabulary=detect_vocabulary(uri) or "OTHER",
        authority_label=agent.name,
        authority_record_id=cand_source_id,
        authority_source=cand_source,
        match_tier=match_tier,
        match_score=match_score,
        dense_score=dense_score,
        rerank_score=rerank_score,
        agent_name_score=agent_name_score,
        action=action,
        is_novel=is_novel,
    )


def reconcile_cmd(
    config: Path = typer.Option(
        Path("config.yaml"), "--config", "-c", help="Path to YAML configuration file"
    ),
) -> None:
    """Reconcile target MARCXML records offline, enrich URIs, and write dual TSV reports."""
    resolved_config = _resolve_cli_config(config)
    cfg = load_config(resolved_config)
    configure_logging(cfg.logging)
    if not cfg.reconcile:
        console.print("[red]No reconcile section found in configuration.[/red]")
        raise typer.Exit(code=1)

    r_cfg = cfg.reconcile
    if not r_cfg.target_file.exists():
        console.print(f"[red]Target MARCXML file not found: {r_cfg.target_file}[/red]")
        raise typer.Exit(code=1)

    vocab_cfg = (
        cfg.ingestion.vocab.normalization if cfg.ingestion.vocab else None
    ) or cfg.vocabularies
    vocab_store = VocabStore(cfg.storage.database_url, config=vocab_cfg)
    vocab_folder = VocabFolder(vocab_store, config=vocab_cfg)
    from reconciliator.embedding import create_vector_embedder
    from reconciliator.embedding.template import RecordTemplateEngine

    embedder = create_vector_embedder(cfg)
    template_path = (
        cfg.embedding.template_path
        if cfg.embedding
        else Path("reconciliator/embedding/templates/default_serialization.j2")
    )
    template_engine = getattr(embedder, "template_engine", None) or RecordTemplateEngine(
        template_path
    )
    rec_norm_cfg = r_cfg.normalization or cfg.record_normalization
    normalizer = RecordNormalizer(
        vocab_folder=vocab_folder,
        template_engine=template_engine,
        config=rec_norm_cfg,
    )
    # The embedder is passed so the work and manifestation indexes are live:
    # without them the matcher's dense tier cannot fire at all.
    wemi_repo = _wemi_repo(cfg, embedder=embedder)

    # Matching is about to run, and it depends on `record_origin` agreeing with
    # the `sources` registry. When they disagree the failure is silent and total:
    # the allow-list filters out every candidate, so nothing matches and nothing
    # errors. Report it here rather than leaving it to be debugged from empty
    # output.
    for finding in wemi_repo.check_source_registry():
        console.print(f"[yellow]Warning: {finding}[/yellow]")

    # Tier 3 standalone agent fallback needs the dedicated agent index; it is
    # optional, so only load it when build-agent-index has produced one.
    agent_indexer = None
    if r_cfg.fallback_agent_reconciliation and cfg.storage.agent_index:
        from reconciliator.indexer import create_vector_engine

        try:
            agent_indexer = create_vector_engine(
                cfg.storage.agent_index,
                dimension=embedder.dimension,
                config=cfg.storage.vector_engine,
            )
            if getattr(agent_indexer, "total_vectors", 0) == 0:
                agent_indexer = None
        except Exception as exc:
            logger.warning("Agent index unavailable, skipping agent fallback: %s", exc)
            agent_indexer = None

    if r_cfg.rerank and r_cfg.rerank.enabled and r_cfg.rerank.judge is None and cfg.llm:
        r_cfg.rerank.judge = cfg.llm

    work_matcher = WorkMatcher(
        wemi_repo=wemi_repo,
        embedder=embedder,
        config=r_cfg,
        agent_indexer=agent_indexer,
    )
    enricher = MARCXMLEnricher(vocab_folder=vocab_folder)
    reporter = ReportGenerator(config=cfg)

    tracker = ProgressTracker(
        db_path=cfg.storage.database_url,
        task_id=f"reconciliation-{int(time.time())}",
        task_type="reconciliation",
        heartbeat_interval=cfg.telemetry.heartbeat_interval_seconds,
    )
    bytes_total = r_cfg.target_file.stat().st_size if r_cfg.target_file.exists() else None
    tracker.start_task(stage="reconciling", bytes_total=bytes_total)

    start_time_sec = time.time()
    console.print(f"[cyan]Reconciling {r_cfg.target_file}...[/cyan]")

    # Format detection
    target_format = r_cfg.format
    if target_format == "auto":
        suffix = r_cfg.target_file.suffix.lower()
        if suffix in (".ttl", ".turtle", ".nt", ".n3", ".jsonld"):
            target_format = "bibframe"
        elif suffix in (".json", ".jsonl", ".ndjson"):
            try:
                with open(r_cfg.target_file, "rb") as probe_f:
                    sample = probe_f.read(2048).decode("utf-8", errors="replace")
                if (
                    "@context" in sample
                    or "@graph" in sample
                    or "http://id.loc.gov" in sample
                    or "bffi" in sample
                ):
                    target_format = "bibframe"
                else:
                    target_format = "bibra"
            except Exception:
                target_format = "bibra"
        elif suffix in (".rdf",):
            target_format = "bibframe"
        elif suffix in (".xml",):
            try:
                with open(r_cfg.target_file, "rb") as probe_f:
                    sample = probe_f.read(2048).decode("utf-8", errors="replace")
                if "RDF" in sample or "bibframe" in sample:
                    target_format = "bibframe"
                else:
                    target_format = "marcxml"
            except Exception:
                target_format = "marcxml"
        else:
            target_format = "marcxml"

    bf_profile: Optional[BIBFRAMESchemaProfile] = (
        get_profile(r_cfg.schema_profile) if r_cfg.schema_profile else None
    )

    is_stream_json = target_format == "bibra" or (
        target_format == "bibframe"
        and r_cfg.target_file.suffix.lower() in (".json", ".jsonl", ".ndjson", ".jsonld")
    )
    is_marcxml = target_format == "marcxml"

    out_f: Optional[IO[bytes]] = None
    if r_cfg.output_file:
        r_cfg.output_file.parent.mkdir(parents=True, exist_ok=True)
        if is_stream_json or is_marcxml:
            out_f = open(r_cfg.output_file, "wb")
            if is_marcxml:
                out_f.write(
                    b'<?xml version="1.0" encoding="UTF-8"?>\n'
                    b'<collection xmlns="http://www.loc.gov/MARC21/slim">\n'
                )

    active_graph: Optional[Graph] = None

    def _iter_records() -> Iterator[tuple[str, Any, dict[str, Any], int]]:
        nonlocal active_graph
        if target_format == "bibra":
            for raw_rec_item, b_offset, bib_item in iter_bibra_records(r_cfg.target_file):
                yield "bibra", bib_item, raw_rec_item, b_offset
        elif target_format == "bibframe":
            suffix = r_cfg.target_file.suffix.lower()
            if suffix in (".json", ".jsonl", ".ndjson", ".jsonld"):
                for raw_rec_item, b_offset, doc_item in iter_jsonld_bibframe_records(
                    r_cfg.target_file, profile=bf_profile, source="target"
                ):
                    yield "bibframe_jsonld", doc_item, raw_rec_item, b_offset
            else:
                for raw_rec_item, b_offset, (g, nodes) in iter_bibframe_records(
                    r_cfg.target_file, profile=bf_profile, source="target"
                ):
                    active_graph = g
                    yield "bibframe_rdf", (g, nodes), raw_rec_item, b_offset
        else:
            for r_elem, b_offset in iter_marc_record_elements(r_cfg.target_file):
                raw_rec_item = parse_marc_record_elem(
                    r_elem,
                    source="target",
                    supported_subject_vocabularies=getattr(
                        vocab_cfg, "supported_subject_vocabularies", None
                    ),
                    legacy_to_modern=getattr(vocab_cfg, "legacy_to_modern", None),
                    vocab_store=vocab_store,
                )
                raw_xml = etree.tostring(r_elem, encoding="utf-8")
                yield "marcxml", raw_xml, raw_rec_item, b_offset

    linkage_map: dict[str, CorpusLinkage] = {}
    work_cluster_map: dict[str, WorkCluster] = {}
    expression_cluster_map: dict[str, ExpressionCluster] = {}
    manifestation_cluster_map: dict[str, ManifestationCluster] = {}

    if r_cfg.cluster and r_cfg.cluster.enabled:
        console.print(
            "[cyan]Staging target records and running intra-corpus graph clusterer...[/cyan]"
        )
        corpus_repo = wemi_repo

        # The corpus is ingested exactly like any other source: WEMI rows carrying
        # its own record_origin, registered as non-authoritative. There is no
        # staging table, so clustering and reconciliation read the same store.
        corpus_source = r_cfg.corpus_source_name or r_cfg.target_file.stem
        corpus_repo.register_source(corpus_source, is_authoritative=False)
        staged = _ingest_corpus_as_wemi(
            corpus_repo,
            _iter_records(),
            corpus_source,
            bf_profile,
            strip_isbd_punctuation=cfg.record_normalization.strip_isbd_punctuation,
            isbd_punctuation=cfg.record_normalization.isbd_punctuation,
            config=r_cfg,
            tracker=tracker,
        )

        if staged:
            clusterer = CorpusClusterer(
                corpus_repo=corpus_repo,
                wemi_repo=wemi_repo,
                matcher=work_matcher,
                config=r_cfg.cluster,
                corpus_origins=[corpus_source],
            )
            clusterer.cluster_corpus()
            for lnk in corpus_repo.get_all_corpus_linkages():
                linkage_map[lnk.record_id] = lnk
            for wc in corpus_repo.get_all_work_clusters():
                work_cluster_map[wc.cluster_id] = wc
            for ec in corpus_repo.get_all_expression_clusters():
                expression_cluster_map[ec.cluster_id] = ec
            for mc in corpus_repo.get_all_manifestation_clusters():
                manifestation_cluster_map[mc.cluster_id] = mc

    target_const_prof = None
    if r_cfg.constituents and r_cfg.constituents.mode != "off":
        from reconciliator.constituents import get_constituent_profile

        try:
            if bf_profile and bf_profile.name == "bffi_1":
                target_const_prof = get_constituent_profile(
                    r_cfg.constituents.bffi_profile or "bffi_haspart"
                )
            else:
                target_const_prof = get_constituent_profile(
                    r_cfg.constituents.bibframe_profile or "bibframe_haspart"
                )
        except Exception:
            pass

    reconcile_batch_size = max(1, getattr(r_cfg, "batch_size", 32))
    queue_cap = max(1, int(getattr(r_cfg, "pipeline_queue_capacity", 2)))

    def _chunked_iter(iterable: Iterator[Any], size: int) -> Iterator[list[Any]]:
        it = iter(iterable)
        while True:
            chunk = list(islice(it, size))
            if not chunk:
                break
            yield chunk

    def _process_output_item(
        rec_fmt: str,
        orig_payload: Any,
        raw_rec: dict[str, Any],
        byte_offset: int,
        target_record: Any,
        target_wemi: Any,
        wemi_res: Any,
        res: Any,
    ) -> None:
        nonlocal total_records
        total_records += 1
        if res.accepted_candidates:
            reporter.matched_records += 1

        # Record agent report rows
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

            # Field tag determination: use recorded field_tag, or fallback by is_personal / position
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
                    _make_agent_report_row(
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
                    _make_agent_report_row(
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

            # An agent with no candidate URI is left untouched in the record,
            # but must still appear in the report: without a row there is no
            # way to tell an agent that failed to link from one never seen,
            # and no way to measure link coverage over a corpus.
            if not selected and not suppressed:
                reporter.add_agent_row(
                    _make_agent_report_row(
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

        # Record subject report rows
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

        # Modern WEMI resolution and reporting, from the single match above.
        linkage = linkage_map.get(target_wemi.record_id)
        w_cluster = work_cluster_map.get(linkage.work_cluster_id) if linkage else None
        e_cluster = expression_cluster_map.get(linkage.expression_cluster_id) if linkage else None
        m_cluster = (
            manifestation_cluster_map.get(linkage.manifestation_cluster_id) if linkage else None
        )

        # Add WorkReportRow
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

        # Add ExpressionReportRow
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

        # Mutate / serialize and stream write if output_file requested
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
                        corpus_repo.get_sibling_manifestation_record_ids(target_wemi.record_id)
                        if is_clustered and corpus_repo
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

    total_records = 0
    try:
        in_queue: queue.Queue[Any] = queue.Queue(maxsize=queue_cap)
        out_queue: queue.Queue[Any] = queue.Queue(maxsize=queue_cap)
        stop_event = threading.Event()
        reader_error: list[Exception] = []
        writer_error: list[Exception] = []

        def _reader_worker() -> None:
            try:
                for batch in _chunked_iter(_iter_records(), reconcile_batch_size):
                    if stop_event.is_set():
                        break
                    batch_items = []
                    wemi_targets = []
                    for rec_fmt, orig_payload, raw_rec, byte_offset in batch:
                        target_record = normalizer.normalize(raw_rec)
                        target_wemi = _to_target_record(
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
                        _process_output_item(*rec_item)
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
    finally:
        if out_f is not None:
            if is_marcxml:
                out_f.write(b"</collection>\n")
            out_f.close()
            console.print(f"[green]Wrote output to {r_cfg.output_file}[/green]")
        elif r_cfg.output_file and active_graph is not None:
            fmt = "turtle"
            suffix = r_cfg.output_file.suffix.lower()
            if suffix in (".ttl", ".turtle"):
                fmt = "turtle"
            elif suffix in (".rdf", ".xml"):
                fmt = "xml"
            elif suffix in (".nt", ".ntriples"):
                fmt = "nt"
            elif suffix in (".jsonld", ".json"):
                fmt = "json-ld"
            active_graph.serialize(destination=str(r_cfg.output_file), format=fmt)
            console.print(f"[green]Wrote output to {r_cfg.output_file}[/green]")

    reporter.total_records = total_records
    tracker.update_progress(
        items_total=total_records,
        bytes_processed=bytes_total,
        force=True,
    )

    # Write TSV Reports
    reporter.elapsed_seconds = time.time() - start_time_sec
    reporter.write_agent_detailed_report(r_cfg.agent_reports.detailed_file)
    reporter.write_agent_summary_report(r_cfg.agent_reports.summary_file)
    reporter.write_subject_detailed_report(r_cfg.subject_reports.detailed_file)
    reporter.write_subject_summary_report(r_cfg.subject_reports.summary_file)

    if r_cfg.work_reports:
        reporter.write_work_report(r_cfg.work_reports.detailed_file)
        if r_cfg.work_reports.summary_file:
            reporter.write_wemi_summary_report(r_cfg.work_reports.summary_file)
    if r_cfg.expression_reports:
        reporter.write_expression_report(r_cfg.expression_reports.detailed_file)
        if r_cfg.expression_reports.summary_file and (
            not r_cfg.work_reports
            or r_cfg.expression_reports.summary_file != r_cfg.work_reports.summary_file
        ):
            reporter.write_wemi_summary_report(r_cfg.expression_reports.summary_file)

    tracker.complete_task(
        metadata={
            "matched_records": reporter.matched_records,
            "total_records": reporter.total_records,
        }
    )
    console.print("[green]Reconciliation complete! Emitted dual TSV report sets.[/green]")
