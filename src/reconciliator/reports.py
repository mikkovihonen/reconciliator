"""Dual decoupled TSV report generators adhering to strict TSV hygiene standards."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from reconciliator.config import AppConfig
from reconciliator.matcher import ReconciliationResult


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
        suppressed_priority = sum(1 for r in self.agent_rows if r.action == "suppressed_by_priority")
        filtered_vocab = sum(1 for r in self.agent_rows if r.action == "filtered_vocab")

        rows = [
            ("execution", "timestamp", self.start_time, "", "Execution start timestamp"),
            ("execution", "target_file", str(reconcile_cfg.target_file if reconcile_cfg else ""), "", "Input target MARCXML file"),
            ("execution", "output_file", str(reconcile_cfg.output_file if reconcile_cfg else ""), "", "Output enriched MARCXML file"),
            ("execution", "enrich_mode", str(reconcile_cfg.enrich if reconcile_cfg else True).lower(), "", "MARCXML enrichment toggle"),
            ("execution", "threshold", f"{reconcile_cfg.threshold:.4f}" if reconcile_cfg else "0.8500", "", "Minimum hybrid score threshold"),
            ("execution", "link_policy", reconcile_cfg.link_policy if reconcile_cfg else "priority_fallback", "", "Link prioritization policy"),
            ("execution", "elapsed_seconds", f"{self.elapsed_seconds:.1f}", "", "Agent reconciliation runtime in seconds"),
            ("records", "total_records", str(self.total_records), "100.0%", "Total target MARCXML records processed"),
            ("records", "records_matched", str(self.matched_records), f"{(self.matched_records / max(1, self.total_records))*100:.1f}%", "Records with accepted authority match"),
            ("records", "records_unmatched", str(self.total_records - self.matched_records), f"{((self.total_records - self.matched_records) / max(1, self.total_records))*100:.1f}%", "Records with zero candidate matches above threshold"),
            ("uris", "total_uris_discovered", str(total_uris), "100.0%", "Total candidate agent URIs aggregated across matches"),
            ("uris", "uris_novel_injected", str(novel_injected), f"{(novel_injected / max(1, total_uris))*100:.1f}%", "Novel authority URIs injected"),
            ("uris", "uris_existing_verified", str(existing_verified), f"{(existing_verified / max(1, total_uris))*100:.1f}%", "Existing URIs confirmed present"),
            ("uris", "uris_suppressed_priority", str(suppressed_priority), f"{(suppressed_priority / max(1, total_uris))*100:.1f}%", "Lower-priority URIs suppressed"),
            ("uris", "uris_filtered_vocab", str(filtered_vocab), f"{(filtered_vocab / max(1, total_uris))*100:.1f}%", "URIs omitted due to vocabulary rules"),
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
        unmapped = sum(1 for r in self.subject_rows if not r.target_uri)

        rows = [
            ("execution", "timestamp", self.start_time, "", "Execution start timestamp"),
            ("execution", "target_file", str(reconcile_cfg.target_file if reconcile_cfg else ""), "", "Input target MARCXML file"),
            ("execution", "enrich_mode", str(reconcile_cfg.enrich if reconcile_cfg else True).lower(), "", "MARCXML enrichment toggle"),
            ("execution", "fold_legacy_enabled", str(reconcile_cfg.fold_legacy if reconcile_cfg else True).lower(), "", "Legacy thesauri folding enabled"),
            ("execution", "finto_db", str(self.config.storage.finto_db), "", "Path to FINTO SQLite reference database"),
            ("execution", "elapsed_seconds", f"{self.elapsed_seconds:.1f}", "", "Subject reconciliation runtime in seconds"),
            ("headings", "total_headings_evaluated", str(total_headings), "100.0%", "Total subject headings evaluated"),
            ("headings", "headings_successfully_mapped", str(mapped), f"{(mapped / max(1, total_headings))*100:.1f}%", "Headings resolved to modern ontology URIs"),
            ("headings", "headings_unmapped", str(unmapped), f"{(unmapped / max(1, total_headings))*100:.1f}%", "Headings with no matching concept in finto.db"),
        ]

        with file_path.open("w", encoding="utf-8", newline="\n") as f:
            f.write("\t".join(SUMMARY_COLUMNS) + "\n")
            for section, metric, value, pct, desc in rows:
                f.write(f"{section}\t{metric}\t{value}\t{pct}\t{desc}\n")

