"""Dual decoupled TSV report generators adhering to strict TSV hygiene standards."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from reconciliator.config import AppConfig, ReconcileConfig, VocabIngestConfig
from reconciliator.utils.tsv import sanitize_tsv_cell, write_summary_tsv, write_tsv_file
from reconciliator.vocabularies import strip_vocabulary_subcode


def _get_model_field_default(model_cls: type, field_name: str) -> list[str]:
    """Extract default list from a Pydantic model field definition if available."""
    field_info = getattr(model_cls, "model_fields", {}).get(field_name)
    if field_info is not None:
        val = field_info.get_default(call_default_factory=True)
        if isinstance(val, (list, tuple)):
            return [str(x) for x in val]
    return []


def describe_agent_vocabulary(vocab: str) -> str:
    """Dynamically format human-readable description for an agent vocabulary."""
    if not vocab:
        return "Unspecified agent vocabulary"
    v_clean = vocab.strip().upper()
    if v_clean in ("OTHER", "UNCLASSIFIED", "UNKNOWN"):
        return "Unclassified or external agent identifier URIs"
    if v_clean in ("WIKIDATA", "WD"):
        return "Wikidata entity QID URIs ($1)"
    return f"{v_clean} agent identifier URIs ($0)"


def describe_source_thesaurus(thesaurus: str) -> str:
    """Dynamically format human-readable description for a source thesaurus."""
    if not thesaurus:
        return "Legacy terms with no $2 or local tags"
    norm = thesaurus.strip().lower()
    if norm in ("unspecified_legacy", "unspecified", "none", "legacy"):
        return "Legacy terms with no $2 or local tags"

    if norm == "allars":
        label = "Allärs"
    elif len(norm) <= 4:
        label = norm.upper()
    else:
        label = norm.capitalize()

    return f"Terms originated from {label}"


def describe_target_ontology(ontology: str) -> str:
    """Dynamically format human-readable description for a target ontology."""
    if not ontology:
        return "Unspecified target ontology"
    norm = ontology.strip().lower()
    if norm in ("wikidata", "wd"):
        return "Mapped to Wikidata Entity URIs"

    if "-" in norm:
        prefix, *rest = norm.split("-")
        ont_label = f"{prefix.upper()}-{'-'.join(rest)}"
    else:
        ont_label = norm.upper()

    return f"Mapped to {ont_label}"


def describe_tag_migration(key: str) -> str:
    """Dynamically generate human-readable description based on what happened in what field."""
    parts = key.split("_to_")
    if len(parts) != 2:
        return f"Subject migration/folding {key}"

    left_tokens = parts[0].split("_")
    right_tokens = parts[1].split("_")

    src_tag = left_tokens[0]
    src_v = left_tokens[1].upper() if len(left_tokens) > 1 else None

    if len(right_tokens) == 2:
        tgt_tag, tgt_v = right_tokens[0], right_tokens[1].upper()
    elif len(right_tokens) == 1:
        if right_tokens[0].isdigit():
            tgt_tag, tgt_v = right_tokens[0], None
        else:
            tgt_tag, tgt_v = src_tag, right_tokens[0].upper()
    else:
        return f"Subject migration/folding {key}"

    # 1. Intra-field vocabulary folding (same field, different vocabulary)
    if src_tag == tgt_tag and src_v and tgt_v:
        return f"In {src_tag}, {src_v} folded to {tgt_v}"

    # 2. Retagging with source vocabulary (tag change + vocab change)
    if src_tag != tgt_tag and src_v and tgt_v:
        return f"Terms in {src_tag} ({src_v}) retagged to {tgt_tag} {tgt_v}"

    # 3. Pure retagging (tag change, optional target vocabulary)
    if tgt_v:
        return f"Terms in {src_tag} retagged to {tgt_tag} {tgt_v}"
    return f"Terms in {src_tag} retagged to {tgt_tag}"


class _DynamicDescriptions(dict[str, str]):
    """Dynamic dict-like mapping that resolves keys via a generator function."""

    def __init__(self, generator: Any) -> None:
        super().__init__()
        self._generator = generator

    def __getitem__(self, key: str) -> str:
        return str(self._generator(key))

    def __contains__(self, key: object) -> bool:
        return True

    def get(self, key: str, default: Any = None) -> str:
        return str(self._generator(key))


DEFAULT_AGENT_VOCAB_DESCRIPTIONS: dict[str, str] = _DynamicDescriptions(describe_agent_vocabulary)
DEFAULT_SOURCE_THESAURUS_DESCRIPTIONS: dict[str, str] = _DynamicDescriptions(
    describe_source_thesaurus
)
DEFAULT_TARGET_ONTOLOGY_DESCRIPTIONS: dict[str, str] = _DynamicDescriptions(
    describe_target_ontology
)
DEFAULT_TAG_MIGRATION_DESCRIPTIONS: dict[str, str] = _DynamicDescriptions(describe_tag_migration)


def normalize_vocab_code(vocab: Optional[str]) -> str:
    """Normalize vocabulary code by stripping language subcodes (e.g. yso/fin -> yso)."""
    if not vocab:
        return ""
    clean = vocab.strip()
    if not clean.startswith("http"):
        clean = strip_vocabulary_subcode(clean)
    return clean.lower()


@dataclass
class AgentReportRow:
    target_record_id: str
    target_title: str
    target_year: Optional[int]
    target_isbn: Optional[str]
    field_tag: str
    field_occurrence: int
    subfield_code: str
    target_heading: str
    target_role: Optional[str]
    uri: str
    vocabulary: str
    authority_label: Optional[str]
    authority_record_id: str
    authority_source: str
    match_tier: str
    match_score: float
    dense_score: Optional[float]
    rerank_score: Optional[float]
    agent_name_score: Optional[float]
    action: str  # "injected", "proposed", "existing", "suppressed_by_priority", "filtered_vocab", "below_threshold"
    is_novel: bool
    qualifier: Optional[str] = None
    dates: Optional[str] = None
    fuller_name: Optional[str] = None
    affiliation: Optional[str] = None
    birth_year: Optional[int] = None
    death_year: Optional[int] = None


@dataclass
class SubjectReportRow:
    target_record_id: str
    target_title: str
    source_field_tag: str
    field_occurrence: int
    source_heading: str
    source_vocab: Optional[str]
    source_uri: Optional[str]
    language: str
    target_field_tag: str
    target_vocab: Optional[str]
    target_uri: Optional[str]
    target_pref_label: Optional[str]
    mapping_type: str
    action: str  # "injected", "proposed", "existing", "unmapped", "ambiguous"
    is_novel: bool
    tag_changed: bool


@dataclass
class WorkReportRow:
    target_record_id: str
    work_type: str
    target_title: str
    target_author: Optional[str]
    target_year: Optional[int]
    work_blocking_key: Optional[str]
    matched_work_uri: Optional[str]
    wikidata_qid: Optional[str]
    match_tier: str
    funnel_used: str
    confidence_score: float
    is_clustered: bool
    cluster_id: Optional[str]
    action: str  # "injected", "hoisted", "minted", "unmatched"
    # The URI this record resolved to on its own, before its work cluster's
    # canonical URI was applied. Differs from matched_work_uri when sibling
    # records in the same cluster matched different authority sources.
    record_level_work_uri: Optional[str] = None


@dataclass
class ExpressionReportRow:
    target_record_id: str
    work_uri: Optional[str]
    expression_uri: Optional[str]
    content_type: str
    language: str
    version_designation: Optional[str]
    translators: Optional[str]
    performers: Optional[str]
    match_tier: str
    confidence_score: float
    cluster_id: Optional[str]
    action: str  # "injected", "aligned", "synthesized", "minted"


AGENT_TSV_COLUMNS = [
    "target_record_id",
    "target_title",
    "target_year",
    "target_isbn",
    "field_tag",
    "field_occurrence",
    "subfield_code",
    "target_heading",
    "target_role",
    "uri",
    "vocabulary",
    "authority_label",
    "authority_record_id",
    "authority_source",
    "match_tier",
    "match_score",
    "dense_score",
    "rerank_score",
    "agent_name_score",
    "action",
    "is_novel",
]

SUBJECT_TSV_COLUMNS = [
    "target_record_id",
    "target_title",
    "source_field_tag",
    "field_occurrence",
    "source_heading",
    "source_vocab",
    "source_uri",
    "language",
    "target_field_tag",
    "target_vocab",
    "target_uri",
    "target_pref_label",
    "mapping_type",
    "action",
    "is_novel",
    "tag_changed",
]

WORK_TSV_COLUMNS = [
    "target_record_id",
    "work_type",
    "target_title",
    "target_author",
    "target_year",
    "work_blocking_key",
    "matched_work_uri",
    "wikidata_qid",
    "match_tier",
    "funnel_used",
    "confidence_score",
    "is_clustered",
    "cluster_id",
    "action",
    "record_level_work_uri",
]

EXPRESSION_TSV_COLUMNS = [
    "target_record_id",
    "work_uri",
    "expression_uri",
    "content_type",
    "language",
    "version_designation",
    "translators",
    "performers",
    "match_tier",
    "confidence_score",
    "cluster_id",
    "action",
]

SUMMARY_COLUMNS = ["section", "metric", "value", "percentage", "description"]
WEMI_SUMMARY_COLUMNS = ["section", "metric", "value", "percentage", "description"]


@dataclass
class QualityBracket:
    """Represents a score interval for quality reporting."""

    metric: str
    low: Optional[float]
    high: float
    is_top: bool
    description: str

    def matches(self, score: float) -> bool:
        if self.is_top:
            return score >= (self.low if self.low is not None else 0.0)
        if self.low is None:
            return score < self.high
        return self.low <= score < self.high


def derive_quality_brackets(
    reconcile_cfg: Optional[ReconcileConfig],
) -> list[QualityBracket]:
    """Derive quality score brackets based on application thresholds or custom brackets.

    Returns a list of QualityBracket objects ordered from highest to lowest score.
    """
    if reconcile_cfg and reconcile_cfg.quality_brackets:
        cutoffs = sorted(reconcile_cfg.quality_brackets)
        brackets: list[QualityBracket] = []
        top_cut = cutoffs[-1]
        brackets.append(
            QualityBracket(
                metric=f"score_{top_cut:.2f}_to_1.00",
                low=top_cut,
                high=1.0,
                is_top=True,
                description=f"Candidate matches with score in [{top_cut:.2f}, 1.00]",
            )
        )
        for i in range(len(cutoffs) - 1, 0, -1):
            low = cutoffs[i - 1]
            high = cutoffs[i]
            brackets.append(
                QualityBracket(
                    metric=f"score_{low:.2f}_to_{high:.2f}",
                    low=low,
                    high=high,
                    is_top=False,
                    description=f"Candidate matches with score in [{low:.2f}, {high:.2f})",
                )
            )
        lowest = cutoffs[0]
        if lowest > 0.0:
            brackets.append(
                QualityBracket(
                    metric=f"score_below_{lowest:.2f}",
                    low=None,
                    high=lowest,
                    is_top=False,
                    description=f"Candidate matches with score below {lowest:.2f}",
                )
            )
        return brackets

    t_accept = round(reconcile_cfg.threshold, 2) if reconcile_cfg else 0.85
    if (
        reconcile_cfg
        and reconcile_cfg.rerank
        and reconcile_cfg.rerank.short_circuit
        and reconcile_cfg.rerank.short_circuit.enabled
    ):
        t_short = round(reconcile_cfg.rerank.short_circuit.threshold, 2)
    elif reconcile_cfg is None:
        t_short = 0.98
    else:
        t_short = 0.95

    t_short = min(1.0, max(0.0, t_short))
    t_accept = min(1.0, max(0.0, t_accept))

    brackets = []
    if t_short > t_accept:
        t_mid = round((t_accept + t_short) / 2, 2)
        if t_mid > t_accept and t_mid < t_short:
            brackets.append(
                QualityBracket(
                    metric=f"score_{t_short:.2f}_to_1.00",
                    low=t_short,
                    high=1.0,
                    is_top=True,
                    description=f"Candidate matches with score in [{t_short:.2f}, 1.00] (high-confidence / short-circuit)",
                )
            )
            brackets.append(
                QualityBracket(
                    metric=f"score_{t_mid:.2f}_to_{t_short:.2f}",
                    low=t_mid,
                    high=t_short,
                    is_top=False,
                    description=f"Candidate matches with score in [{t_mid:.2f}, {t_short:.2f}) (robust hybrid reranked)",
                )
            )
            brackets.append(
                QualityBracket(
                    metric=f"score_{t_accept:.2f}_to_{t_mid:.2f}",
                    low=t_accept,
                    high=t_mid,
                    is_top=False,
                    description=f"Candidate matches with score in [{t_accept:.2f}, {t_mid:.2f}) (meeting acceptance threshold)",
                )
            )
        else:
            brackets.append(
                QualityBracket(
                    metric=f"score_{t_short:.2f}_to_1.00",
                    low=t_short,
                    high=1.0,
                    is_top=True,
                    description=f"Candidate matches with score in [{t_short:.2f}, 1.00] (high-confidence)",
                )
            )
            brackets.append(
                QualityBracket(
                    metric=f"score_{t_accept:.2f}_to_{t_short:.2f}",
                    low=t_accept,
                    high=t_short,
                    is_top=False,
                    description=f"Candidate matches with score in [{t_accept:.2f}, {t_short:.2f}) (meeting acceptance threshold)",
                )
            )
    else:
        brackets.append(
            QualityBracket(
                metric=f"score_{t_accept:.2f}_to_1.00",
                low=t_accept,
                high=1.0,
                is_top=True,
                description=f"Candidate matches with score in [{t_accept:.2f}, 1.00] (meeting acceptance threshold)",
            )
        )

    if t_accept > 0.0:
        brackets.append(
            QualityBracket(
                metric=f"score_below_{t_accept:.2f}",
                low=None,
                high=t_accept,
                is_top=False,
                description=f"Candidate matches with score below {t_accept:.2f} (below acceptance threshold)",
            )
        )

    return brackets


class ReportGenerator:
    """Generates dual decoupled TSV report sets for Agent and Subject reconciliation."""

    def __init__(self, config: Optional[AppConfig] = None) -> None:
        self.config = config or AppConfig()
        self.agent_rows: list[AgentReportRow] = []
        self.subject_rows: list[SubjectReportRow] = []
        self.work_rows: list[WorkReportRow] = []
        self.expression_rows: list[ExpressionReportRow] = []

        self.total_records = 0
        self.matched_records = 0
        self.total_agent_fields = 0
        self.reconciled_agent_fields = 0
        self.start_time = datetime.now(timezone.utc).isoformat()
        self.elapsed_seconds = 0.0

    def add_agent_row(self, row: AgentReportRow) -> None:
        self.agent_rows.append(row)

    def add_subject_row(self, row: SubjectReportRow) -> None:
        self.subject_rows.append(row)

    def add_work_row(self, row: WorkReportRow) -> None:
        self.work_rows.append(row)

    def add_expression_row(self, row: ExpressionReportRow) -> None:
        self.expression_rows.append(row)

    def write_agent_detailed_report(self, file_path: Path) -> None:
        """Write agent_reconciliation_uris.tsv with exact 21 columns."""
        write_tsv_file(file_path, AGENT_TSV_COLUMNS, self.agent_rows)

    def write_agent_summary_report(self, file_path: Path) -> None:
        """Write agent_reconciliation_summary.tsv in tidy 5-column schema."""
        file_path.parent.mkdir(parents=True, exist_ok=True)
        reconcile_cfg = self.config.reconcile if self.config else None

        total_uris = len(self.agent_rows)
        novel_injected = sum(1 for r in self.agent_rows if r.action == "injected")
        existing_verified = sum(1 for r in self.agent_rows if r.action == "existing")
        suppressed_priority = sum(
            1 for r in self.agent_rows if r.action == "suppressed_by_priority"
        )
        filtered_vocab = sum(1 for r in self.agent_rows if r.action == "filtered_vocab")

        # Field metrics
        agent_fields = {(r.target_record_id, r.field_occurrence) for r in self.agent_rows}
        reconciled_fields = {
            (r.target_record_id, r.field_occurrence)
            for r in self.agent_rows
            if r.action in ("injected", "existing", "proposed")
        }
        total_fields_count = len(agent_fields) or self.total_agent_fields
        reconciled_fields_count = len(reconciled_fields) or self.reconciled_agent_fields

        # Tier metrics
        tier1 = sum(1 for r in self.agent_rows if r.match_tier == "tier1_exact_id")
        tier2 = sum(1 for r in self.agent_rows if r.match_tier == "tier2_dense")
        tier3 = sum(1 for r in self.agent_rows if r.match_tier == "tier3_hybrid_rerank")
        tier_denom = max(1, tier1 + tier2 + tier3)

        # Vocabulary metrics - dynamically aggregated from rows and ordered by config
        vocab_counts: dict[str, int] = {}
        for r in self.agent_rows:
            v_name = r.vocabulary.upper() if r.vocabulary else "OTHER"
            vocab_counts[v_name] = vocab_counts.get(v_name, 0) + 1
        vocab_denom = max(1, total_uris)

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

        # Source metrics
        source_counts: dict[str, int] = {}
        for r in self.agent_rows:
            s_name = r.authority_source.lower() if r.authority_source else "unknown"
            source_counts[s_name] = source_counts.get(s_name, 0) + 1
        source_denom = max(1, sum(source_counts.values()))

        # Execution threshold metadata
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
        rerank_engine = rerank_cfg.engine if rerank_cfg else "bm25"

        if not rerank_enabled:
            rerank_engine_str = "disabled"
            rerank_engine_desc = "disabled"
        else:
            rerank_engine_str = rerank_engine
            if rerank_engine == "bm25":
                rerank_engine_desc = "BM25 sparse"
            elif rerank_engine == "cross_encoder":
                rerank_engine_desc = "neural cross-encoder"
            elif rerank_engine == "fuzzy":
                rerank_engine_desc = "fuzzy lexical"
            elif rerank_engine == "noop":
                rerank_engine_desc = "noop"
            else:
                rerank_engine_desc = str(rerank_engine)

        # Quality score brackets - derived dynamically from application thresholds or config
        brackets = derive_quality_brackets(reconcile_cfg)
        quality_denom = max(1, len(self.agent_rows))
        quality_metric_rows = []
        for b in brackets:
            count = sum(1 for r in self.agent_rows if b.matches(r.match_score))
            pct = f"{(count / quality_denom) * 100:.1f}%"
            quality_metric_rows.append(("quality", b.metric, str(count), pct, b.description))

        rows = [
            ("execution", "timestamp", self.start_time, "", "Execution start timestamp"),
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
                f"{self.elapsed_seconds:.1f}",
                "",
                "Agent reconciliation runtime in seconds",
            ),
            (
                "records",
                "total_records",
                str(self.total_records),
                "100.0%",
                "Total target MARCXML records processed",
            ),
            (
                "records",
                "records_matched",
                str(self.matched_records),
                f"{(self.matched_records / max(1, self.total_records)) * 100:.1f}%",
                "Records with accepted authority match",
            ),
            (
                "records",
                "records_unmatched",
                str(self.total_records - self.matched_records),
                f"{((self.total_records - self.matched_records) / max(1, self.total_records)) * 100:.1f}%",
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

        write_summary_tsv(file_path, SUMMARY_COLUMNS, rows)

    def write_subject_detailed_report(self, file_path: Path) -> None:
        """Write subject_reconciliation_uris.tsv with exact 16 columns."""
        write_tsv_file(file_path, SUBJECT_TSV_COLUMNS, self.subject_rows)

    def write_subject_summary_report(self, file_path: Path) -> None:
        """Write subject_reconciliation_summary.tsv in tidy 5-column schema."""
        file_path.parent.mkdir(parents=True, exist_ok=True)
        reconcile_cfg = self.config.reconcile if self.config else None

        total_headings = len(self.subject_rows)
        mapped = sum(1 for r in self.subject_rows if r.target_uri)
        unmapped = sum(1 for r in self.subject_rows if not r.target_uri or r.action == "unmapped")
        already_canonical = sum(1 for r in self.subject_rows if r.action == "existing")
        ambiguous = sum(1 for r in self.subject_rows if r.action == "ambiguous")

        # Record level breakdowns
        records_with_subs: dict[str, list[SubjectReportRow]] = {}
        for r in self.subject_rows:
            records_with_subs.setdefault(r.target_record_id, []).append(r)

        total_rec_count = max(self.total_records, len(records_with_subs))
        rec_with_subs_count = len(records_with_subs)
        fully_reconciled = 0
        partially_reconciled = 0
        with_unmapped = 0

        for r_id, rows_list in records_with_subs.items():
            mapped_in_rec = sum(1 for r in rows_list if r.target_uri)
            if mapped_in_rec == len(rows_list):
                fully_reconciled += 1
            elif mapped_in_rec > 0:
                partially_reconciled += 1
            if mapped_in_rec < len(rows_list):
                with_unmapped += 1

        # Mapping type counts
        exact_match = sum(1 for r in self.subject_rows if r.mapping_type == "exact_match")
        pref_label = sum(1 for r in self.subject_rows if r.mapping_type == "pref_label_lookup")
        is_replaced = sum(1 for r in self.subject_rows if r.mapping_type == "is_replaced_by")
        alt_label = sum(1 for r in self.subject_rows if r.mapping_type == "alt_label_lookup")
        mapped_denom = max(1, mapped)

        # Dynamic source thesauri breakdown
        source_thesauri_counts: dict[str, int] = {}
        unspec_count = 0
        for r in self.subject_rows:
            norm_s = normalize_vocab_code(r.source_vocab)
            if not norm_s:
                unspec_count += 1
            else:
                source_thesauri_counts[norm_s] = source_thesauri_counts.get(norm_s, 0) + 1
        thesauri_denom = max(1, total_headings)

        vocab_norm_cfg = (
            self.config.normalization.vocab if self.config and self.config.normalization else None
        )
        if vocab_norm_cfg and vocab_norm_cfg.legacy_to_modern:
            legacy_thesauri = list(vocab_norm_cfg.legacy_to_modern.keys())
        elif self.config and self.config.ingest_vocab and self.config.ingest_vocab.legacy_thesauri:
            legacy_thesauri = [th.lower() for th in self.config.ingest_vocab.legacy_thesauri]
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

        # Dynamic target ontologies distribution
        target_ontology_counts: dict[str, int] = {}
        for r in self.subject_rows:
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

        # Dynamic tag migrations and foldings
        tag_migrations_counts: dict[str, int] = {}

        for r in self.subject_rows:
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

        throughput = round(total_headings / max(0.001, self.elapsed_seconds), 1)

        rows = [
            ("execution", "timestamp", self.start_time, "", "Execution start timestamp"),
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
                str(self.config.storage.database_url if self.config and self.config.storage else ""),
                "",
                "PostgreSQL database connection URL",
            ),
            (
                "execution",
                "elapsed_seconds",
                f"{self.elapsed_seconds:.1f}",
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

        write_summary_tsv(file_path, SUMMARY_COLUMNS, rows)

    def write_work_report(self, file_path: Path) -> None:
        """Write work_reconciliation.tsv containing work-level links across 42 domains."""
        write_tsv_file(file_path, WORK_TSV_COLUMNS, self.work_rows)

    def write_expression_report(self, file_path: Path) -> None:
        """Write expression_reconciliation.tsv containing expression-level realization links."""
        write_tsv_file(file_path, EXPRESSION_TSV_COLUMNS, self.expression_rows)

    def write_wemi_summary_report(self, file_path: Path) -> None:
        """Write wemi_reconciliation_summary.tsv summarizing work & expression reconciliation."""
        file_path.parent.mkdir(parents=True, exist_ok=True)
        total_works = len(self.work_rows)
        matched_works = sum(1 for r in self.work_rows if r.matched_work_uri)
        clustered_works = sum(1 for r in self.work_rows if r.is_clustered)
        total_exps = len(self.expression_rows)

        # Content type breakdown
        ct_counts: dict[str, int] = {}
        for er in self.expression_rows:
            ct = er.content_type or "text"
            ct_counts[ct] = ct_counts.get(ct, 0) + 1

        # Work type breakdown across 42 domains
        wt_counts: dict[str, int] = {}
        for wr in self.work_rows:
            wt = wr.work_type or "text"
            wt_counts[wt] = wt_counts.get(wt, 0) + 1

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

        write_summary_tsv(file_path, WEMI_SUMMARY_COLUMNS, rows)
