"""Coordinator for report generation, metric aggregation, and file writing."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from reconciliator.config import AppConfig
from reconciliator.reports.metrics import (
    AgentMetricsCalculator,
    SubjectMetricsCalculator,
    WEMIMetricsCalculator,
)
from reconciliator.reports.models import (
    AgentReportRow,
    ExpressionReportRow,
    SubjectReportRow,
    WorkReportRow,
)
from reconciliator.reports.writer import TSVReportWriter


class ReportGenerator:
    """Generates dual decoupled TSV report sets for Agent, Subject, and WEMI reconciliation."""

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

        self.writer = TSVReportWriter()

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
        self.writer.write_agent_detailed(file_path, self.agent_rows)

    def write_agent_summary_report(self, file_path: Path) -> None:
        """Write agent_reconciliation_summary.tsv in tidy 5-column schema."""
        metric_rows = AgentMetricsCalculator.calculate(
            agent_rows=self.agent_rows,
            config=self.config,
            total_records=self.total_records,
            matched_records=self.matched_records,
            total_agent_fields=self.total_agent_fields,
            reconciled_agent_fields=self.reconciled_agent_fields,
            start_time=self.start_time,
            elapsed_seconds=self.elapsed_seconds,
        )
        self.writer.write_agent_summary(file_path, metric_rows)

    def write_subject_detailed_report(self, file_path: Path) -> None:
        """Write subject_reconciliation_uris.tsv with exact 16 columns."""
        self.writer.write_subject_detailed(file_path, self.subject_rows)

    def write_subject_summary_report(self, file_path: Path) -> None:
        """Write subject_reconciliation_summary.tsv in tidy 5-column schema."""
        metric_rows = SubjectMetricsCalculator.calculate(
            subject_rows=self.subject_rows,
            config=self.config,
            total_records=self.total_records,
            start_time=self.start_time,
            elapsed_seconds=self.elapsed_seconds,
        )
        self.writer.write_subject_summary(file_path, metric_rows)

    def write_work_report(self, file_path: Path) -> None:
        """Write work_reconciliation.tsv containing work-level links across 42 domains."""
        self.writer.write_work(file_path, self.work_rows)

    def write_expression_report(self, file_path: Path) -> None:
        """Write expression_reconciliation.tsv containing expression-level realization links."""
        self.writer.write_expression(file_path, self.expression_rows)

    def write_wemi_summary_report(self, file_path: Path) -> None:
        """Write wemi_reconciliation_summary.tsv summarizing work & expression reconciliation."""
        metric_rows = WEMIMetricsCalculator.calculate(
            work_rows=self.work_rows,
            expression_rows=self.expression_rows,
        )
        self.writer.write_wemi_summary(file_path, metric_rows)
