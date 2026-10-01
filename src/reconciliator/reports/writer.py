"""TSV report writers adhering to strict TSV hygiene standards."""

from __future__ import annotations

from pathlib import Path

from reconciliator.reports.models import (
    AGENT_TSV_COLUMNS,
    EXPRESSION_TSV_COLUMNS,
    SUBJECT_TSV_COLUMNS,
    SUMMARY_COLUMNS,
    WEMI_SUMMARY_COLUMNS,
    WORK_TSV_COLUMNS,
    AgentReportRow,
    ExpressionReportRow,
    SubjectReportRow,
    WorkReportRow,
)
from reconciliator.utils.tsv import write_summary_tsv, write_tsv_file


class TSVReportWriter:
    """Writes detailed and summary TSV reports to disk."""

    @staticmethod
    def write_agent_detailed(file_path: Path, rows: list[AgentReportRow]) -> None:
        """Write agent_reconciliation_uris.tsv with exact 21 columns."""
        write_tsv_file(file_path, AGENT_TSV_COLUMNS, rows)

    @staticmethod
    def write_agent_summary(file_path: Path, rows: list[tuple[str, str, str, str, str]]) -> None:
        """Write agent_reconciliation_summary.tsv in tidy 5-column schema."""
        file_path.parent.mkdir(parents=True, exist_ok=True)
        write_summary_tsv(file_path, SUMMARY_COLUMNS, rows)

    @staticmethod
    def write_subject_detailed(file_path: Path, rows: list[SubjectReportRow]) -> None:
        """Write subject_reconciliation_uris.tsv with exact 16 columns."""
        write_tsv_file(file_path, SUBJECT_TSV_COLUMNS, rows)

    @staticmethod
    def write_subject_summary(
        file_path: Path, rows: list[tuple[str, str, str, str, str]]
    ) -> None:
        """Write subject_reconciliation_summary.tsv in tidy 5-column schema."""
        file_path.parent.mkdir(parents=True, exist_ok=True)
        write_summary_tsv(file_path, SUMMARY_COLUMNS, rows)

    @staticmethod
    def write_work(file_path: Path, rows: list[WorkReportRow]) -> None:
        """Write work_reconciliation.tsv containing work-level links across 42 domains."""
        write_tsv_file(file_path, WORK_TSV_COLUMNS, rows)

    @staticmethod
    def write_expression(file_path: Path, rows: list[ExpressionReportRow]) -> None:
        """Write expression_reconciliation.tsv containing expression-level realization links."""
        write_tsv_file(file_path, EXPRESSION_TSV_COLUMNS, rows)

    @staticmethod
    def write_wemi_summary(file_path: Path, rows: list[tuple[str, str, str, str, str]]) -> None:
        """Write wemi_reconciliation_summary.tsv summarizing work & expression reconciliation."""
        file_path.parent.mkdir(parents=True, exist_ok=True)
        write_summary_tsv(file_path, WEMI_SUMMARY_COLUMNS, rows)
