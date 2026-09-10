"""Dual decoupled TSV report generators adhering to strict TSV hygiene standards."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from reconciliator.config import AppConfig


def sanitize_tsv_cell(val: Any) -> str:
    """Sanitize string for TSV cell, replacing tabs and newlines with spaces."""
    if val is None:
        return ""
    if isinstance(val, bool):
        return "true" if val else "false"
    if isinstance(val, float):
        return f"{val:.4f}"
    s = str(val)
    return s.replace("\t", " ").replace("\r", " ").replace("\n", " ").strip()


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
    sparse_score: Optional[float]
    agent_name_score: Optional[float]
    action: str  # "injected", "proposed", "existing", "suppressed_by_priority", "filtered_vocab", "below_threshold"
    is_novel: bool


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
    "sparse_score",
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

SUMMARY_COLUMNS = ["section", "metric", "value", "percentage", "description"]


class ReportGenerator:
    """Generates dual decoupled TSV report sets for Agent and Subject reconciliation."""

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.agent_rows: list[AgentReportRow] = []
        self.subject_rows: list[SubjectReportRow] = []

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

    def write_agent_detailed_report(self, file_path: Path) -> None:
        """Write agent_reconciliation_uris.tsv with exact 21 columns."""
        file_path.parent.mkdir(parents=True, exist_ok=True)
        with file_path.open("w", encoding="utf-8", newline="\n") as f:
            f.write("\t".join(AGENT_TSV_COLUMNS) + "\n")
            for r in self.agent_rows:
                vals = [
                    sanitize_tsv_cell(r.target_record_id),
                    sanitize_tsv_cell(r.target_title),
                    sanitize_tsv_cell(r.target_year),
                    sanitize_tsv_cell(r.target_isbn),
                    sanitize_tsv_cell(r.field_tag),
                    sanitize_tsv_cell(r.field_occurrence),
                    sanitize_tsv_cell(r.subfield_code),
                    sanitize_tsv_cell(r.target_heading),
                    sanitize_tsv_cell(r.target_role),
                    sanitize_tsv_cell(r.uri),
                    sanitize_tsv_cell(r.vocabulary),
                    sanitize_tsv_cell(r.authority_label),
                    sanitize_tsv_cell(r.authority_record_id),
                    sanitize_tsv_cell(r.authority_source),
                    sanitize_tsv_cell(r.match_tier),
                    sanitize_tsv_cell(r.match_score),
                    sanitize_tsv_cell(r.dense_score),
                    sanitize_tsv_cell(r.sparse_score),
                    sanitize_tsv_cell(r.agent_name_score),
                    sanitize_tsv_cell(r.action),
                    sanitize_tsv_cell(r.is_novel),
                ]
                f.write("\t".join(vals) + "\n")

    def write_agent_summary_report(self, file_path: Path) -> None:
        """Write agent_reconciliation_summary.tsv in tidy 5-column schema."""
        file_path.parent.mkdir(parents=True, exist_ok=True)
        reconcile_cfg = self.config.reconcile

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
        tier2 = sum(1 for r in self.agent_rows if r.match_tier == "tier2_dense_faiss")
        tier3 = sum(1 for r in self.agent_rows if r.match_tier == "tier3_hybrid_rerank")
        tier_denom = max(1, tier1 + tier2 + tier3)

        # Vocabulary metrics
        kanto_count = sum(1 for r in self.agent_rows if r.vocabulary.upper() == "KANTO")
        wikidata_count = sum(1 for r in self.agent_rows if r.vocabulary.upper() == "WIKIDATA")
        isni_count = sum(1 for r in self.agent_rows if r.vocabulary.upper() == "ISNI")
        viaf_count = sum(1 for r in self.agent_rows if r.vocabulary.upper() == "VIAF")
        vocab_denom = max(1, total_uris)

        # Source metrics
        melinda_count = sum(1 for r in self.agent_rows if r.authority_source.lower() == "melinda")
        wiki_source_count = sum(
            1 for r in self.agent_rows if r.authority_source.lower() == "wikidata"
        )
        source_denom = max(1, melinda_count + wiki_source_count)

        # Quality score brackets
        score_high = sum(1 for r in self.agent_rows if r.match_score >= 0.95)
        score_mid = sum(1 for r in self.agent_rows if 0.90 <= r.match_score < 0.95)
        score_low = sum(1 for r in self.agent_rows if 0.85 <= r.match_score < 0.90)
        quality_denom = max(1, score_high + score_mid + score_low)

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
                "tier2_dense_faiss",
                str(tier2),
                f"{(tier2 / tier_denom) * 100:.1f}%",
                "Matches accepted directly from dense vector similarity",
            ),
            (
                "tiers",
                "tier3_hybrid_rerank",
                str(tier3),
                f"{(tier3 / tier_denom) * 100:.1f}%",
                "Matches resolved via hybrid dense + BM25 sparse reranking",
            ),
            (
                "vocabularies",
                "KANTO",
                str(kanto_count),
                f"{(kanto_count / vocab_denom) * 100:.1f}%",
                "Finnish National Agent Authority Asteri URNs ($0)",
            ),
            (
                "vocabularies",
                "WIKIDATA",
                str(wikidata_count),
                f"{(wikidata_count / vocab_denom) * 100:.1f}%",
                "Wikidata entity QID URIs ($1)",
            ),
            (
                "vocabularies",
                "ISNI",
                str(isni_count),
                f"{(isni_count / vocab_denom) * 100:.1f}%",
                "International Standard Name Identifier URIs ($0)",
            ),
            (
                "vocabularies",
                "VIAF",
                str(viaf_count),
                f"{(viaf_count / vocab_denom) * 100:.1f}%",
                "Virtual International Authority File URIs ($0)",
            ),
            (
                "sources",
                "melinda",
                str(melinda_count),
                f"{(melinda_count / source_denom) * 100:.1f}%",
                "Agent links sourced from Melinda/Asteri",
            ),
            (
                "sources",
                "wikidata",
                str(wiki_source_count),
                f"{(wiki_source_count / source_denom) * 100:.1f}%",
                "Agent links sourced from Wikidata",
            ),
            (
                "quality",
                "score_0.95_to_1.00",
                str(score_high),
                f"{(score_high / quality_denom) * 100:.1f}%",
                "Candidate matches with score in [0.95, 1.00]",
            ),
            (
                "quality",
                "score_0.90_to_0.95",
                str(score_mid),
                f"{(score_mid / quality_denom) * 100:.1f}%",
                "Candidate matches with score in [0.90, 0.95)",
            ),
            (
                "quality",
                "score_0.85_to_0.90",
                str(score_low),
                f"{(score_low / quality_denom) * 100:.1f}%",
                "Candidate matches with score in [0.85, 0.90)",
            ),
        ]

        with file_path.open("w", encoding="utf-8", newline="\n") as f:
            f.write("\t".join(SUMMARY_COLUMNS) + "\n")
            for section, metric, value, pct, desc in rows:
                f.write(f"{section}\t{metric}\t{value}\t{pct}\t{desc}\n")

    def write_subject_detailed_report(self, file_path: Path) -> None:
        """Write subject_reconciliation_uris.tsv with exact 16 columns."""
        file_path.parent.mkdir(parents=True, exist_ok=True)
        with file_path.open("w", encoding="utf-8", newline="\n") as f:
            f.write("\t".join(SUBJECT_TSV_COLUMNS) + "\n")
            for r in self.subject_rows:
                vals = [
                    sanitize_tsv_cell(r.target_record_id),
                    sanitize_tsv_cell(r.target_title),
                    sanitize_tsv_cell(r.source_field_tag),
                    sanitize_tsv_cell(r.field_occurrence),
                    sanitize_tsv_cell(r.source_heading),
                    sanitize_tsv_cell(r.source_vocab),
                    sanitize_tsv_cell(r.source_uri),
                    sanitize_tsv_cell(r.language),
                    sanitize_tsv_cell(r.target_field_tag),
                    sanitize_tsv_cell(r.target_vocab),
                    sanitize_tsv_cell(r.target_uri),
                    sanitize_tsv_cell(r.target_pref_label),
                    sanitize_tsv_cell(r.mapping_type),
                    sanitize_tsv_cell(r.action),
                    sanitize_tsv_cell(r.is_novel),
                    sanitize_tsv_cell(r.tag_changed),
                ]
                f.write("\t".join(vals) + "\n")

    def write_subject_summary_report(self, file_path: Path) -> None:
        """Write subject_reconciliation_summary.tsv in tidy 5-column schema."""
        file_path.parent.mkdir(parents=True, exist_ok=True)
        reconcile_cfg = self.config.reconcile

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

        # Source thesauri breakdown
        ysa_count = sum(1 for r in self.subject_rows if (r.source_vocab or "").lower() == "ysa")
        kaunokki_count = sum(
            1 for r in self.subject_rows if (r.source_vocab or "").lower() in ("kaunokki", "bella")
        )
        allars_count = sum(
            1 for r in self.subject_rows if (r.source_vocab or "").lower() == "allars"
        )
        musa_count = sum(
            1 for r in self.subject_rows if (r.source_vocab or "").lower() in ("musa", "cilla")
        )
        cilla_bella_count = sum(
            1 for r in self.subject_rows if (r.source_vocab or "").lower() in ("cilla", "bella")
        )
        unspec_count = sum(
            1
            for r in self.subject_rows
            if not r.source_vocab
            or (r.source_vocab or "").lower()
            not in (
                "ysa",
                "kaunokki",
                "allars",
                "musa",
                "cilla",
                "bella",
                "yso",
                "slm",
                "kauno",
                "muso",
                "seko",
            )
        )
        thesauri_denom = max(1, total_headings)

        # Target ontologies distribution
        yso_count = sum(1 for r in self.subject_rows if (r.target_vocab or "").lower() == "yso")
        slm_count = sum(1 for r in self.subject_rows if (r.target_vocab or "").lower() == "slm")
        kauno_count = sum(1 for r in self.subject_rows if (r.target_vocab or "").lower() == "kauno")
        muso_count = sum(1 for r in self.subject_rows if (r.target_vocab or "").lower() == "muso")
        seko_count = sum(1 for r in self.subject_rows if (r.target_vocab or "").lower() == "seko")
        target_denom = max(1, mapped)

        # Tag migrations
        mig_650_655 = sum(
            1
            for r in self.subject_rows
            if r.source_field_tag == "650"
            and r.target_field_tag == "655"
            and (r.target_vocab or "").lower() == "slm"
        )
        mig_655_slm = sum(
            1
            for r in self.subject_rows
            if r.source_field_tag == "655"
            and (r.source_vocab or "").lower() in ("kaunokki", "bella")
            and (r.target_vocab or "").lower() == "slm"
        )
        mig_650_kauno = sum(
            1
            for r in self.subject_rows
            if r.source_field_tag == "650"
            and (r.source_vocab or "").lower() in ("kaunokki", "bella")
            and (r.target_vocab or "").lower() == "kauno"
        )
        mig_382_seko = sum(
            1
            for r in self.subject_rows
            if r.source_field_tag == "382" and (r.target_vocab or "").lower() == "seko"
        )

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
                "finto_db",
                str(self.config.storage.finto_db),
                "",
                "Path to FINTO SQLite reference database",
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
                "Headings with no matching concept in finto.db",
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
            (
                "source_thesauri",
                "ysa",
                str(ysa_count),
                f"{(ysa_count / thesauri_denom) * 100:.1f}%",
                "Terms originated from YSA",
            ),
            (
                "source_thesauri",
                "kaunokki",
                str(kaunokki_count),
                f"{(kaunokki_count / thesauri_denom) * 100:.1f}%",
                "Terms originated from Kaunokki",
            ),
            (
                "source_thesauri",
                "allars",
                str(allars_count),
                f"{(allars_count / thesauri_denom) * 100:.1f}%",
                "Terms originated from Allärs",
            ),
            (
                "source_thesauri",
                "musa",
                str(musa_count),
                f"{(musa_count / thesauri_denom) * 100:.1f}%",
                "Terms originated from MUSA",
            ),
            (
                "source_thesauri",
                "cilla_bella",
                str(cilla_bella_count),
                f"{(cilla_bella_count / thesauri_denom) * 100:.1f}%",
                "Terms originated from Cilla / Bella",
            ),
            (
                "source_thesauri",
                "unspecified_legacy",
                str(unspec_count),
                f"{(unspec_count / thesauri_denom) * 100:.1f}%",
                "Legacy terms with no $2 or local tags",
            ),
            (
                "target_ontologies",
                "yso",
                str(yso_count),
                f"{(yso_count / target_denom) * 100:.1f}%",
                "Mapped to YSO (General Finnish Ontology)",
            ),
            (
                "target_ontologies",
                "slm",
                str(slm_count),
                f"{(slm_count / target_denom) * 100:.1f}%",
                "Mapped to SLM (Finnish Genre/Form Vocabulary)",
            ),
            (
                "target_ontologies",
                "kauno",
                str(kauno_count),
                f"{(kauno_count / target_denom) * 100:.1f}%",
                "Mapped to KAUNO (Fiction Ontology)",
            ),
            (
                "target_ontologies",
                "muso",
                str(muso_count),
                f"{(muso_count / target_denom) * 100:.1f}%",
                "Mapped to MUSO (Music Ontology)",
            ),
            (
                "target_ontologies",
                "seko",
                str(seko_count),
                f"{(seko_count / target_denom) * 100:.1f}%",
                "Mapped to SEKO (Performance Vocabulary)",
            ),
            (
                "tag_migrations",
                "650_to_655_slm",
                str(mig_650_655),
                "--",
                "Genre terms in 650 retagged to 655 SLM",
            ),
            (
                "tag_migrations",
                "655_kaunokki_to_slm",
                str(mig_655_slm),
                "--",
                "Legacy 655 Kaunokki folded to 655 SLM",
            ),
            (
                "tag_migrations",
                "650_kaunokki_to_kauno",
                str(mig_650_kauno),
                "--",
                "Fiction topical terms in 650 folded to 650 KAUNO",
            ),
            (
                "tag_migrations",
                "382_musa_to_seko",
                str(mig_382_seko),
                "--",
                "Performance terms in 382 folded to 382 SEKO",
            ),
        ]

        with file_path.open("w", encoding="utf-8", newline="\n") as f:
            f.write("\t".join(SUMMARY_COLUMNS) + "\n")
            for section, metric, value, pct, desc in rows:
                f.write(f"{section}\t{metric}\t{value}\t{pct}\t{desc}\n")
