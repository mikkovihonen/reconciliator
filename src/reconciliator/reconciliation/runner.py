"""Orchestrator and execution pipeline runner for reconciliation workflows."""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import IO, Any, Iterator, Optional

from lxml import etree
from rdflib import Graph
from rich.console import Console

from reconciliator.clustering import CorpusClusterer
from reconciliator.config import AppConfig, ReconcileConfig
from reconciliator.database import WEMIRepository
from reconciliator.embedding import create_vector_embedder
from reconciliator.embedding.template import RecordTemplateEngine
from reconciliator.enrichment.marcxml import MARCXMLEnricher
from reconciliator.loaders.bibframe import (
    BIBFRAMESchemaProfile,
    get_profile,
    iter_bibframe_records,
    iter_jsonld_bibframe_records,
)
from reconciliator.loaders.bibra import iter_bibra_records
from reconciliator.loaders.marcxml import (
    iter_marc_record_elements,
    parse_marc_record_elem,
)
from reconciliator.models import (
    CorpusLinkage,
    ExpressionCluster,
    ManifestationCluster,
    WorkCluster,
)
from reconciliator.normalizers.record import RecordNormalizer
from reconciliator.progress import ProgressTracker
from reconciliator.reconciliation.matcher import WorkMatcher
from reconciliator.reconciliation.pipeline import (
    ingest_corpus_as_wemi,
    process_output_item,
    run_pipeline_workers,
)
from reconciliator.reports import ReportGenerator
from reconciliator.vocab import VocabFolder, VocabStore

logger = logging.getLogger(__name__)


class ReconciliationPipelineRunner:
    """Decoupled orchestrator for the multi-format, multi-stage reconciliation pipeline."""

    def __init__(
        self,
        config: AppConfig,
        wemi_repo: WEMIRepository,
        vocab_store: VocabStore,
        vocab_folder: VocabFolder,
        embedder: Any,
        normalizer: RecordNormalizer,
        work_matcher: WorkMatcher,
        enricher: MARCXMLEnricher,
        reporter: ReportGenerator,
        tracker: ProgressTracker,
        console: Optional[Console] = None,
    ) -> None:
        self.config = config
        self.reconcile_config = config.reconcile or ReconcileConfig()
        self.wemi_repo = wemi_repo
        self.vocab_store = vocab_store
        self.vocab_folder = vocab_folder
        self.embedder = embedder
        self.normalizer = normalizer
        self.work_matcher = work_matcher
        self.enricher = enricher
        self.reporter = reporter
        self.tracker = tracker
        self.console = console or Console()

    @classmethod
    def from_config(
        cls,
        cfg: AppConfig,
        tracker: Optional[ProgressTracker] = None,
        console: Optional[Console] = None,
    ) -> ReconciliationPipelineRunner:
        """Construct a configured runner instance from application configuration."""
        r_cfg = cfg.reconcile
        if not r_cfg:
            raise ValueError("No reconcile configuration block provided")

        vocab_cfg = (
            cfg.ingestion.vocab.normalization if cfg.ingestion.vocab else None
        ) or cfg.vocabularies
        vocab_store = VocabStore(cfg.storage.database_url, config=vocab_cfg)
        vocab_folder = VocabFolder(vocab_store, config=vocab_cfg)

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

        wemi_repo = WEMIRepository(
            cfg.storage.database_url,
            fts_config=cfg.storage.fts,
            work_index_path=cfg.storage.work_index,
            manifestation_index_path=cfg.storage.manifestation_index,
            dimension=embedder.dimension,
            vector_engine_config=cfg.storage.vector_engine,
        )

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

        if tracker is None:
            tracker = ProgressTracker(
                db_path=cfg.storage.database_url,
                task_id=f"reconciliation-{int(time.time())}",
                task_type="reconciliation",
                heartbeat_interval=cfg.telemetry.heartbeat_interval_seconds,
            )

        return cls(
            config=cfg,
            wemi_repo=wemi_repo,
            vocab_store=vocab_store,
            vocab_folder=vocab_folder,
            embedder=embedder,
            normalizer=normalizer,
            work_matcher=work_matcher,
            enricher=enricher,
            reporter=reporter,
            tracker=tracker,
            console=console,
        )

    def run(self) -> int:
        """Run the end-to-end reconciliation pipeline."""
        cfg = self.config
        r_cfg = self.reconcile_config
        wemi_repo = self.wemi_repo
        normalizer = self.normalizer
        work_matcher = self.work_matcher
        enricher = self.enricher
        reporter = self.reporter
        tracker = self.tracker
        console = self.console

        for finding in wemi_repo.check_source_registry():
            console.print(f"[yellow]Warning: {finding}[/yellow]")

        bytes_total = r_cfg.target_file.stat().st_size if r_cfg.target_file.exists() else None
        tracker.start_task(stage="reconciling", bytes_total=bytes_total)
        start_time_sec = time.time()
        console.print(f"[cyan]Reconciling {r_cfg.target_file}...[/cyan]")

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
        vocab_cfg = (
            cfg.ingestion.vocab.normalization if cfg.ingestion.vocab else None
        ) or cfg.vocabularies

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
                        vocab_store=self.vocab_store,
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
            corpus_source = r_cfg.corpus_source_name or r_cfg.target_file.stem
            corpus_repo.register_source(corpus_source, is_authoritative=False)
            staged = ingest_corpus_as_wemi(
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

        def _handle_output_item(
            rec_fmt: str,
            orig_payload: Any,
            raw_rec: dict[str, Any],
            byte_offset: int,
            target_record: Any,
            target_wemi: Any,
            wemi_res: Any,
            res: Any,
        ) -> None:
            process_output_item(
                rec_fmt,
                orig_payload,
                raw_rec,
                byte_offset,
                target_record,
                target_wemi,
                wemi_res,
                res,
                reporter=reporter,
                r_cfg=r_cfg,
                cfg=cfg,
                linkage_map=linkage_map,
                work_cluster_map=work_cluster_map,
                expression_cluster_map=expression_cluster_map,
                manifestation_cluster_map=manifestation_cluster_map,
                out_f=out_f,
                bf_profile=bf_profile,
                wemi_repo=wemi_repo,
                enricher=enricher,
                tracker=tracker,
            )

        try:
            total_records = run_pipeline_workers(
                record_iter_fn=_iter_records,
                normalizer=normalizer,
                work_matcher=work_matcher,
                bf_profile=bf_profile,
                target_const_prof=target_const_prof,
                reconcile_batch_size=reconcile_batch_size,
                queue_cap=queue_cap,
                output_processor=_handle_output_item,
            )
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
        return total_records
