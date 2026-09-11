"""Unit tests for dual decoupled TSV report generators and TSV hygiene."""

from pathlib import Path

from reconciliator.config import AppConfig
from reconciliator.reports import (
    AGENT_TSV_COLUMNS,
    SUBJECT_TSV_COLUMNS,
    SUMMARY_COLUMNS,
    AgentReportRow,
    ReportGenerator,
    SubjectReportRow,
    sanitize_tsv_cell,
)


def test_sanitize_tsv_cell() -> None:
    assert sanitize_tsv_cell("Line 1\nLine 2\tTabbed") == "Line 1 Line 2 Tabbed"
    assert sanitize_tsv_cell(True) == "true"
    assert sanitize_tsv_cell(False) == "false"
    assert sanitize_tsv_cell(0.950001) == "0.9500"
    assert sanitize_tsv_cell(None) == ""


def test_agent_reports(tmp_path: Path) -> None:
    cfg = AppConfig()
    reporter = ReportGenerator(cfg)
    reporter.total_records = 1
    reporter.matched_records = 1

    reporter.add_agent_row(
        AgentReportRow(
            target_record_id="T001",
            target_title="Sinuhe",
            target_year=1945,
            target_isbn="9789510000010",
            field_tag="100",
            field_occurrence=1,
            subfield_code="0",
            target_heading="Waltari, Mika",
            target_role="aut",
            uri="https://urn.fi/URN:NBN:fi:au:pn:000123456",
            vocabulary="KANTO",
            authority_label="Waltari, Mika",
            authority_record_id="MEL001",
            authority_source="melinda",
            match_tier="tier1_exact_id",
            match_score=1.0,
            dense_score=None,
            rerank_score=None,
            agent_name_score=100.0,
            action="injected",
            is_novel=True,
        )
    )

    det_path = tmp_path / "agent_uris.tsv"
    sum_path = tmp_path / "agent_summary.tsv"

    reporter.write_agent_detailed_report(det_path)
    reporter.write_agent_summary_report(sum_path)

    det_lines = det_path.read_text(encoding="utf-8").splitlines()
    assert len(det_lines) == 2
    header = det_lines[0].split("\t")
    assert header == AGENT_TSV_COLUMNS

    sum_lines = sum_path.read_text(encoding="utf-8").splitlines()
    assert sum_lines[0].split("\t") == SUMMARY_COLUMNS

    sections_found = set()
    for line in sum_lines[1:]:
        parts = line.split("\t")
        assert len(parts) == 5
        sections_found.add(parts[0])

    expected_agent_sections = {
        "execution",
        "records",
        "fields",
        "uris",
        "tiers",
        "vocabularies",
        "sources",
        "quality",
    }
    assert expected_agent_sections.issubset(sections_found)


def test_subject_reports(tmp_path: Path) -> None:
    cfg = AppConfig()
    reporter = ReportGenerator(cfg)
    reporter.total_records = 1

    reporter.add_subject_row(
        SubjectReportRow(
            target_record_id="T001",
            target_title="Sinuhe",
            source_field_tag="650",
            field_occurrence=1,
            source_heading="lääkärit",
            source_vocab="ysa",
            source_uri=None,
            language="fi",
            target_field_tag="650",
            target_vocab="yso",
            target_uri="http://www.yso.fi/onto/yso/p2591",
            target_pref_label="lääkärit",
            mapping_type="exact_match",
            action="injected",
            is_novel=True,
            tag_changed=False,
        )
    )

    det_path = tmp_path / "subj_uris.tsv"
    sum_path = tmp_path / "subj_summary.tsv"

    reporter.write_subject_detailed_report(det_path)
    reporter.write_subject_summary_report(sum_path)

    det_lines = det_path.read_text(encoding="utf-8").splitlines()
    assert len(det_lines) == 2
    assert det_lines[0].split("\t") == SUBJECT_TSV_COLUMNS

    sum_lines = sum_path.read_text(encoding="utf-8").splitlines()
    assert sum_lines[0].split("\t") == SUMMARY_COLUMNS

    sections_found = set()
    for line in sum_lines[1:]:
        parts = line.split("\t")
        assert len(parts) == 5
        sections_found.add(parts[0])

    expected_subject_sections = {
        "execution",
        "records",
        "headings",
        "mapping_types",
        "source_thesauri",
        "target_ontologies",
        "tag_migrations",
    }
    assert expected_subject_sections.issubset(sections_found)
