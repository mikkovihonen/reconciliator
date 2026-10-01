"""Dual decoupled TSV report generators adhering to strict TSV hygiene standards."""

from __future__ import annotations

from reconciliator.reports.descriptions import (
    DEFAULT_AGENT_VOCAB_DESCRIPTIONS,
    DEFAULT_SOURCE_THESAURUS_DESCRIPTIONS,
    DEFAULT_TAG_MIGRATION_DESCRIPTIONS,
    DEFAULT_TARGET_ONTOLOGY_DESCRIPTIONS,
    derive_quality_brackets,
    describe_agent_vocabulary,
    describe_source_thesaurus,
    describe_tag_migration,
    describe_target_ontology,
    normalize_vocab_code,
)
from reconciliator.reports.generator import ReportGenerator
from reconciliator.reports.metrics import (
    AgentMetricsCalculator,
    SubjectMetricsCalculator,
    WEMIMetricsCalculator,
)
from reconciliator.reports.models import (
    AGENT_TSV_COLUMNS,
    EXPRESSION_TSV_COLUMNS,
    SUBJECT_TSV_COLUMNS,
    SUMMARY_COLUMNS,
    WEMI_SUMMARY_COLUMNS,
    WORK_TSV_COLUMNS,
    AgentReportRow,
    ExpressionReportRow,
    QualityBracket,
    SubjectReportRow,
    WorkReportRow,
)
from reconciliator.reports.writer import TSVReportWriter
from reconciliator.utils.tsv import sanitize_tsv_cell, write_summary_tsv, write_tsv_file

__all__ = [
    "AGENT_TSV_COLUMNS",
    "AgentMetricsCalculator",
    "AgentReportRow",
    "DEFAULT_AGENT_VOCAB_DESCRIPTIONS",
    "DEFAULT_SOURCE_THESAURUS_DESCRIPTIONS",
    "DEFAULT_TAG_MIGRATION_DESCRIPTIONS",
    "DEFAULT_TARGET_ONTOLOGY_DESCRIPTIONS",
    "EXPRESSION_TSV_COLUMNS",
    "ExpressionReportRow",
    "QualityBracket",
    "ReportGenerator",
    "SUBJECT_TSV_COLUMNS",
    "SUMMARY_COLUMNS",
    "SubjectMetricsCalculator",
    "SubjectReportRow",
    "TSVReportWriter",
    "WEMIMetricsCalculator",
    "WEMI_SUMMARY_COLUMNS",
    "WORK_TSV_COLUMNS",
    "WorkReportRow",
    "derive_quality_brackets",
    "describe_agent_vocabulary",
    "describe_source_thesaurus",
    "describe_tag_migration",
    "describe_target_ontology",
    "normalize_vocab_code",
    "sanitize_tsv_cell",
    "write_summary_tsv",
    "write_tsv_file",
]
