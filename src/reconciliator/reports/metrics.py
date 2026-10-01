"""Metric calculation strategies for reconciliation reports."""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from reconciliator.config import AppConfig, ReconcileConfig, VocabIngestConfig
from reconciliator.reports.descriptions import (
    _get_model_field_default,
    derive_quality_brackets,
    describe_agent_vocabulary,
    describe_source_thesaurus,
    describe_tag_migration,
    describe_target_ontology,
    normalize_vocab_code,
)
from reconciliator.reports.models import (
    AgentReportRow,
    ExpressionReportRow,
    SubjectReportRow,
    WorkReportRow,
)

if TYPE_CHECKING:
    pass


class AgentMetricsCalculator:
    """Calculates tidy summary metrics for agent reconciliation."""

    @staticmethod
    def calculate(
        agent_rows: list[AgentReportRow],
        config: Optional[AppConfig] = None,
        total_records: int = 0,
        matched_records: int = 0,
        total_agent_fields: int = 0,
        reconciled_agent_fields: int = 0,
        start_time: str = "",
        elapsed_seconds: float = 0.0,
    ) -> list[tuple[str, str, str, str, str]]:
        reconcile_cfg = config.reconcile if config else None

        total_uris = len(agent_rows)
        novel_injected = 0
        existing_verified = 0
        suppressed_priority = 0
        filtered_vocab = 0
        agent_fields: set[tuple[str, int]] = set()
        reconciled_fields: set[tuple[str, int]] = set()
        tier1 = 0
        tier2 = 0
        tier3 = 0
        vocab_counts: dict[str, int] = {}
        source_counts: dict[str, int] = {}

        for r in agent_rows:
            act = r.action
            if act == "injected":
                novel_injected += 1
                reconciled_fields.add((r.target_record_id, r.field_occurrence))
            elif act == "existing":
                existing_verified += 1
                reconciled_fields.add((r.target_record_id, r.field_occurrence))
            elif act == "proposed":
                reconciled_fields.add((r.target_record_id, r.field_occurrence))
            elif act == "suppressed_by_priority":
                suppressed_priority += 1
            elif act == "filtered_vocab":
                filtered_vocab += 1

            agent_fields.add((r.target_record_id, r.field_occurrence))

            mt = r.match_tier
            if mt == "tier1_exact_id":
                tier1 += 1
            elif mt == "tier2_dense":
                tier2 += 1
            elif mt == "tier3_hybrid_rerank":
                tier3 += 1

            v_name = r.vocabulary.upper() if r.vocabulary else "OTHER"
            vocab_counts[v_name] = vocab_counts.get(v_name, 0) + 1

            s_name = r.authority_source.lower() if r.authority_source else "unknown"
            source_counts[s_name] = source_counts.get(s_name, 0) + 1

        total_fields_count = len(agent_fields) or total_agent_fields
        reconciled_fields_count = len(reconciled_fields) or reconciled_agent_fields
        tier_denom = max(1, tier1 + tier2 + tier3)
        vocab_denom = max(1, total_uris)
        source_denom = max(1, sum(source_counts.values()))

        if reconcile_cfg and reconcile_cfg.agent_priority:
            configured_vocabs = [v.upper() for v in reconcile_cfg.agent_priority]
        else:
            default_prio = _get_model_field_default(ReconcileConfig, "agent_priority")
            configured_vocabs = [v.upper() for v in default_prio]

        reporting_vocabs: list[str] = list(configured_vocabs)
        for v in sorted(vocab_counts.keys()):
            if v not in reporting_vocabs:
                reporting_vocabs.append(v)

        vocab_metric_rows = [
            (
                "vocabularies",
                v,
                str(vocab_counts.get(v, 0)),
                f"{(vocab_counts.get(v, 0) / vocab_denom) * 100:.1f}%",
                describe_agent_vocabulary(v),
            )
            for v in reporting_vocabs
        ]

        sc_threshold_str = (
            f"{reconcile_cfg.rerank.short_circuit.threshold:.4f}"
            if reconcile_cfg
            and reconcile_cfg.rerank
            and reconcile_cfg.rerank.short_circuit
            and reconcile_cfg.rerank.short_circuit.enabled
            else (
                "disabled"
                if reconcile_cfg
                and reconcile_cfg.rerank
                and reconcile_cfg.rerank.short_circuit
                and not reconcile_cfg.rerank.short_circuit.enabled
                else "0.9800"
            )
        )
        dense_weight_str = (
            f"{reconcile_cfg.rerank.dense_weight:.4f}"
            if reconcile_cfg and reconcile_cfg.rerank
            else "0.6000"
        )
        rerank_weight_str = (
            f"{reconcile_cfg.rerank.rerank_weight:.4f}"
            if reconcile_cfg and reconcile_cfg.rerank
            else "0.4000"
        )

        rerank_cfg = reconcile_cfg.rerank if reconcile_cfg else None
        rerank_enabled = rerank_cfg.enabled if rerank_cfg else True
        rerank_engine = rerank_cfg.engine if rerank_cfg else "cascade"

        if not rerank_enabled:
            rerank_engine_str = "disabled"
            rerank_engine_desc = "disabled"
        else:
            rerank_engine_str = rerank_engine
            if rerank_engine == "cascade":
                rerank_engine_desc = "cascading dual-route (heuristic + LLM judge)"
            elif rerank_engine == "llm_judge":
                rerank_engine_desc = "autoregressive LLM judge"
            elif rerank_engine == "bm25":
                rerank_engine_desc = "BM25 sparse"
            elif rerank_engine == "cross_encoder":
                rerank_engine_desc = "neural cross-encoder"
            elif rerank_engine == "fuzzy":
                rerank_engine_desc = "fuzzy lexical"
            elif rerank_engine == "noop":
                rerank_engine_desc = "noop"
            else:
                rerank_engine_desc = str(rerank_engine)

        brackets = derive_quality_brackets(reconcile_cfg)
        quality_denom = max(1, len(agent_rows))
        quality_metric_rows = []
        for b in brackets:
            count = sum(1 for r in agent_rows if b.matches(r.match_score))
            pct = f"{(count / quality_denom) * 100:.1f}%"
            quality_metric_rows.append(("quality", b.metric, str(count), pct, b.description))

        rows = [
            ("execution", "timestamp", start_time, "", "Execution start timestamp"),
            (
                "execution",
                "target_file",
                str(reconcile_cfg.target_file if reconcile_cfg else ""),
                "",
                "Input target MARCXML file",
            ),
            (
                "execution",
                "output_file",
                str(reconcile_cfg.output_file if reconcile_cfg else ""),
                "",
                "Output enriched MARCXML file",
            ),
            (
                "execution",
                "enrich_mode",
                str(reconcile_cfg.enrich if reconcile_cfg else True).lower(),
                "",
                "MARCXML enrichment toggle",
            ),
            (
                "execution",
                "threshold",
                f"{reconcile_cfg.threshold:.4f}" if reconcile_cfg else "0.8500",
                "",
                "Minimum hybrid score threshold",
            ),
            (
                "execution",
                "short_circuit_threshold",
                sc_threshold_str,
                "",
                "Dense short-circuit high-confidence bypass threshold",
            ),
            (
                "execution",
                "rerank_engine",
                rerank_engine_str,
                "",
                "Candidate reranking engine backend",
            ),
            (
                "execution",
                "dense_weight",
                dense_weight_str,
                "",
                "Hybrid scoring dense vector weight",
            ),
            (
                "execution",
                "rerank_weight",
                rerank_weight_str,
                "",
                f"Hybrid scoring {rerank_engine_desc} reranking weight",
            ),
            (
                "execution",
                "link_policy",
                reconcile_cfg.link_policy if reconcile_cfg else "priority_fallback",
                "",
                "Link prioritization policy",
            ),
            (
                "execution",
                "elapsed_seconds",
                f"{elapsed_seconds:.1f}",
                "",
                "Agent reconciliation runtime in seconds",
            ),
            (
                "records",
                "total_records",
                str(total_records),
                "100.0%",
                "Total target MARCXML records processed",
            ),
            (
                "records",
                "records_matched",
                str(matched_records),
                f"{(matched_records / max(1, total_records)) * 100:.1f}%",
                "Records with accepted authority match",
            ),
            (
                "records",
                "records_unmatched",
                str(total_records - matched_records),
                f"{((total_records - matched_records) / max(1, total_records)) * 100:.1f}%",
                "Records with zero candidate matches above threshold",
            ),
            (
                "fields",
                "total_agent_fields",
                str(total_fields_count),
                "100.0%",
                "Total personal/corporate agent fields",
            ),
            (
                "fields",
                "fields_with_reconciled_uri",
                str(reconciled_fields_count),
                f"{(reconciled_fields_count / max(1, total_fields_count)) * 100:.1f}%",
                "Agent fields linked to authority URIs",
            ),
            (
                "uris",
                "total_uris_discovered",
                str(total_uris),
                "100.0%",
                "Total candidate agent URIs aggregated across matches",
            ),
            (
                "uris",
                "uris_novel_injected",
                str(novel_injected),
                f"{(novel_injected / max(1, total_uris)) * 100:.1f}%",
                "Novel authority URIs injected",
            ),
            (
                "uris",
                "uris_existing_verified",
                str(existing_verified),
                f"{(existing_verified / max(1, total_uris)) * 100:.1f}%",
                "Existing URIs confirmed present",
            ),
            (
                "uris",
                "uris_suppressed_priority",
                str(suppressed_priority),
                f"{(suppressed_priority / max(1, total_uris)) * 100:.1f}%",
                "Lower-priority URIs suppressed",
            ),
            (
                "uris",
                "uris_filtered_vocab",
                str(filtered_vocab),
                f"{(filtered_vocab / max(1, total_uris)) * 100:.1f}%",
                "URIs omitted due to vocabulary rules",
            ),
            (
                "tiers",
                "tier1_exact_id",
                str(tier1),
                f"{(tier1 / tier_denom) * 100:.1f}%",
                "Matches resolved via exact identifier lookup",
            ),
            (
                "tiers",
                "tier2_dense",
                str(tier2),
                f"{(tier2 / tier_denom) * 100:.1f}%",
                "Matches accepted directly from dense vector similarity",
            ),
            (
                "tiers",
                "tier3_hybrid_rerank",
                str(tier3),
                f"{(tier3 / tier_denom) * 100:.1f}%",
                f"Matches resolved via hybrid dense + {rerank_engine_desc} reranking",
            ),
            *vocab_metric_rows,
        ]

        source_metric_rows = [
            (
                "sources",
                src,
                str(cnt),
                f"{(cnt / source_denom) * 100:.1f}%",
                f"Agent links sourced from {src}",
            )
            for src, cnt in sorted(source_counts.items())
        ]
        if not source_metric_rows:
            source_metric_rows = [
                ("sources", "total", "0", "0.0%", "Agent links sourced from authority records")
            ]

        rows.extend(source_metric_rows)
        rows.extend(quality_metric_rows)
        return rows


class SubjectMetricsCalculator:
    """Calculates tidy summary metrics for subject reconciliation."""

    @staticmethod
    def calculate(
        subject_rows: list[SubjectReportRow],
        config: Optional[AppConfig] = None,
        total_records: int = 0,
        start_time: str = "",
        elapsed_seconds: float = 0.0,
    ) -> list[tuple[str, str, str, str, str]]:
        reconcile_cfg = config.reconcile if config else None

        total_headings = len(subject_rows)
        mapped = 0
        unmapped = 0
        already_canonical = 0
        ambiguous = 0
        exact_match = 0
        pref_label = 0
        is_replaced = 0
        alt_label = 0
        unspec_count = 0
        records_with_subs: dict[str, list[SubjectReportRow]] = {}
        source_thesauri_counts: dict[str, int] = {}

        for r in subject_rows:
            records_with_subs.setdefault(r.target_record_id, []).append(r)
            if r.target_uri:
                mapped += 1
            if not r.target_uri or r.action == "unmapped":
                unmapped += 1
            if r.action == "existing":
                already_canonical += 1
            elif r.action == "ambiguous":
                ambiguous += 1

            m_type = r.mapping_type
            if m_type == "exact_match":
                exact_match += 1
            elif m_type == "pref_label_lookup":
                pref_label += 1
            elif m_type == "is_replaced_by":
                is_replaced += 1
            elif m_type == "alt_label_lookup":
                alt_label += 1

            norm_s = normalize_vocab_code(r.source_vocab)
            if not norm_s:
                unspec_count += 1
            else:
                source_thesauri_counts[norm_s] = source_thesauri_counts.get(norm_s, 0) + 1

        total_rec_count = max(total_records, len(records_with_subs))
        rec_with_subs_count = len(records_with_subs)
        fully_reconciled = 0
        partially_reconciled = 0
        with_unmapped = 0

        for _r_id, rows_list in records_with_subs.items():
            mapped_in_rec = sum(1 for r in rows_list if r.target_uri)
            if mapped_in_rec == len(rows_list):
                fully_reconciled += 1
            elif mapped_in_rec > 0:
                partially_reconciled += 1
            if mapped_in_rec < len(rows_list):
                with_unmapped += 1

        mapped_denom = max(1, mapped)
        thesauri_denom = max(1, total_headings)

        vocab_norm_cfg = config.vocabularies if config else None
        if vocab_norm_cfg and vocab_norm_cfg.legacy_to_modern:
            legacy_thesauri = list(vocab_norm_cfg.legacy_to_modern.keys())
        elif (
            config
            and config.ingestion.vocab
            and config.ingestion.vocab.legacy_thesauri
        ):
            legacy_thesauri = [th.lower() for th in config.ingestion.vocab.legacy_thesauri]
        else:
            default_thesauri = _get_model_field_default(VocabIngestConfig, "legacy_thesauri")
            legacy_thesauri = [th.lower() for th in default_thesauri]

        source_thesauri_keys: list[str] = []
        for k in legacy_thesauri:
            if k not in source_thesauri_keys:
                source_thesauri_keys.append(k)
        for k in sorted(source_thesauri_counts.keys()):
            if k not in source_thesauri_keys:
                source_thesauri_keys.append(k)

        source_thesauri_metric_rows = [
            (
                "source_thesauri",
                th,
                str(source_thesauri_counts.get(th, 0)),
                f"{(source_thesauri_counts.get(th, 0) / thesauri_denom) * 100:.1f}%",
                describe_source_thesaurus(th),
            )
            for th in source_thesauri_keys
        ]
        source_thesauri_metric_rows.append(
            (
                "source_thesauri",
                "unspecified_legacy",
                str(unspec_count),
                f"{(unspec_count / thesauri_denom) * 100:.1f}%",
                describe_source_thesaurus("unspecified_legacy"),
            )
        )

        target_ontology_counts: dict[str, int] = {}
        for r in subject_rows:
            norm_t = normalize_vocab_code(r.target_vocab)
            if norm_t:
                target_ontology_counts[norm_t] = target_ontology_counts.get(norm_t, 0) + 1
        target_denom = max(1, mapped)

        if reconcile_cfg and reconcile_cfg.subject_priority:
            configured_target_vocabs = [v.lower() for v in reconcile_cfg.subject_priority]
        else:
            default_prio = _get_model_field_default(ReconcileConfig, "subject_priority")
            configured_target_vocabs = [v.lower() for v in default_prio]

        target_ontology_keys: list[str] = []
        for k in configured_target_vocabs:
            if k not in target_ontology_keys:
                target_ontology_keys.append(k)
        for k in sorted(target_ontology_counts.keys()):
            if k not in target_ontology_keys:
                target_ontology_keys.append(k)

        target_ontology_metric_rows = [
            (
                "target_ontologies",
                ont,
                str(target_ontology_counts.get(ont, 0)),
                f"{(target_ontology_counts.get(ont, 0) / target_denom) * 100:.1f}%",
                describe_target_ontology(ont),
            )
            for ont in target_ontology_keys
        ]

        tag_migrations_counts: dict[str, int] = {}
        for r in subject_rows:
            src_tag = r.source_field_tag
            tgt_tag = r.target_field_tag
            src_v = normalize_vocab_code(r.source_vocab)
            tgt_v = normalize_vocab_code(r.target_vocab)

            if src_tag != tgt_tag:
                mig_key = f"{src_tag}_to_{tgt_tag}_{tgt_v}" if tgt_v else f"{src_tag}_to_{tgt_tag}"
                tag_migrations_counts[mig_key] = tag_migrations_counts.get(mig_key, 0) + 1
            elif src_v and tgt_v and src_v != tgt_v:
                fold_key = f"{src_tag}_{src_v}_to_{tgt_v}"
                tag_migrations_counts[fold_key] = tag_migrations_counts.get(fold_key, 0) + 1

        tag_migration_metric_rows = [
            (
                "tag_migrations",
                k,
                str(tag_migrations_counts[k]),
                "--",
                describe_tag_migration(k),
            )
            for k in sorted(tag_migrations_counts.keys())
        ]
        if not tag_migration_metric_rows:
            tag_migration_metric_rows = [
                (
                    "tag_migrations",
                    "none",
                    "0",
                    "--",
                    "No tag migrations or vocabulary foldings detected",
                )
            ]

        throughput = round(total_headings / max(0.001, elapsed_seconds), 1)

        rows = [
            ("execution", "timestamp", start_time, "", "Execution start timestamp"),
            (
                "execution",
                "target_file",
                str(reconcile_cfg.target_file if reconcile_cfg else ""),
                "",
                "Input target MARCXML file",
            ),
            (
                "execution",
                "enrich_mode",
                str(reconcile_cfg.enrich if reconcile_cfg else True).lower(),
                "",
                "MARCXML enrichment toggle",
            ),
            (
                "execution",
                "fold_legacy_enabled",
                str(reconcile_cfg.fold_legacy if reconcile_cfg else True).lower(),
                "",
                "Legacy thesauri folding enabled",
            ),
            (
                "execution",
                "database_url",
                str(config.storage.database_url if config and config.storage else ""),
                "",
                "PostgreSQL database connection URL",
            ),
            (
                "execution",
                "elapsed_seconds",
                f"{elapsed_seconds:.1f}",
                "",
                "Subject reconciliation runtime in seconds",
            ),
            (
                "execution",
                "throughput_headings_sec",
                str(throughput),
                "",
                "Processing throughput (headings/second)",
            ),
            (
                "records",
                "total_records_processed",
                str(total_rec_count),
                "100.0%",
                "Total MARCXML records evaluated",
            ),
            (
                "records",
                "records_with_subjects",
                str(rec_with_subs_count),
                f"{(rec_with_subs_count / max(1, total_rec_count)) * 100:.1f}%",
                "Records containing at least one subject/genre field",
            ),
            (
                "records",
                "records_fully_reconciled",
                str(fully_reconciled),
                f"{(fully_reconciled / max(1, rec_with_subs_count)) * 100:.1f}%",
                "Records where 100% of subject terms mapped",
            ),
            (
                "records",
                "records_partially_reconciled",
                str(partially_reconciled),
                f"{(partially_reconciled / max(1, rec_with_subs_count)) * 100:.1f}%",
                "Records where some terms mapped and some unmapped",
            ),
            (
                "records",
                "records_with_unmapped_terms",
                str(with_unmapped),
                f"{(with_unmapped / max(1, rec_with_subs_count)) * 100:.1f}%",
                "Records containing at least one unmapped term",
            ),
            (
                "headings",
                "total_headings_evaluated",
                str(total_headings),
                "100.0%",
                "Total subject headings evaluated",
            ),
            (
                "headings",
                "headings_successfully_mapped",
                str(mapped),
                f"{(mapped / max(1, total_headings)) * 100:.1f}%",
                "Headings resolved to modern ontology URIs",
            ),
            (
                "headings",
                "headings_already_canonical",
                str(already_canonical),
                f"{(already_canonical / max(1, total_headings)) * 100:.1f}%",
                "Headings already possessing modern canonical URI",
            ),
            (
                "headings",
                "headings_unmapped",
                str(unmapped),
                f"{(unmapped / max(1, total_headings)) * 100:.1f}%",
                "Headings with no matching concept in vocab.db",
            ),
            (
                "headings",
                "headings_ambiguous",
                str(ambiguous),
                f"{(ambiguous / max(1, total_headings)) * 100:.1f}%",
                "Headings with multiple conflicting concept matches",
            ),
            (
                "mapping_types",
                "exact_match",
                str(exact_match),
                f"{(exact_match / mapped_denom) * 100:.1f}%",
                "Mappings resolved via SKOS exactMatch tables",
            ),
            (
                "mapping_types",
                "pref_label_lookup",
                str(pref_label),
                f"{(pref_label / mapped_denom) * 100:.1f}%",
                "Mappings resolved via direct modern skos:prefLabel match",
            ),
            (
                "mapping_types",
                "is_replaced_by",
                str(is_replaced),
                f"{(is_replaced / mapped_denom) * 100:.1f}%",
                "Mappings resolved via dct:isReplacedBy chain",
            ),
            (
                "mapping_types",
                "alt_label_lookup",
                str(alt_label),
                f"{(alt_label / mapped_denom) * 100:.1f}%",
                "Mappings resolved via modern lead-in / skos:altLabel match",
            ),
            *source_thesauri_metric_rows,
            *target_ontology_metric_rows,
            *tag_migration_metric_rows,
        ]
        return rows


class WEMIMetricsCalculator:
    """Calculates tidy summary metrics for WEMI work and expression reconciliation."""

    @staticmethod
    def calculate(
        work_rows: list[WorkReportRow],
        expression_rows: list[ExpressionReportRow],
    ) -> list[tuple[str, str, str, str, str]]:
        total_works = len(work_rows)
        matched_works = 0
        clustered_works = 0
        wt_counts: dict[str, int] = {}
        for wr in work_rows:
            if wr.matched_work_uri:
                matched_works += 1
            if wr.is_clustered:
                clustered_works += 1
            wt = wr.work_type or "text"
            wt_counts[wt] = wt_counts.get(wt, 0) + 1

        total_exps = len(expression_rows)
        ct_counts: dict[str, int] = {}
        for er in expression_rows:
            ct = er.content_type or "text"
            ct_counts[ct] = ct_counts.get(ct, 0) + 1

        rows = [
            (
                "overview",
                "total_works_reconciled",
                str(total_works),
                "100.0%",
                "Total target works evaluated",
            ),
            (
                "overview",
                "matched_works_count",
                str(matched_works),
                f"{(matched_works / max(1, total_works)) * 100:.1f}%",
                "Works resolved to canonical authority URI",
            ),
            (
                "overview",
                "clustered_works_count",
                str(clustered_works),
                f"{(clustered_works / max(1, total_works)) * 100:.1f}%",
                "Works grouped via intra-corpus co-reference graph",
            ),
            (
                "overview",
                "total_expressions_linked",
                str(total_exps),
                "--",
                "Total expression realizations linked",
            ),
        ]

        for wt, count in sorted(wt_counts.items()):
            rows.append(
                (
                    "work_types",
                    wt,
                    str(count),
                    f"{(count / max(1, total_works)) * 100:.1f}%",
                    f"Target records of work type {wt}",
                )
            )

        for ct, count in sorted(ct_counts.items()):
            rows.append(
                (
                    "content_types",
                    ct,
                    str(count),
                    f"{(count / max(1, total_exps)) * 100:.1f}%",
                    f"Expressions realized as {ct}",
                )
            )

        return rows
