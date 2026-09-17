"""Unit tests for dual decoupled TSV report generators and TSV hygiene."""

from pathlib import Path

from reconciliator.config import (
    AppConfig,
    ReconcileConfig,
    ReportSetPathsConfig,
    RerankConfig,
    ShortCircuitConfig,
)
from reconciliator.reports import (
    AGENT_TSV_COLUMNS,
    DEFAULT_AGENT_VOCAB_DESCRIPTIONS,
    DEFAULT_SOURCE_THESAURUS_DESCRIPTIONS,
    DEFAULT_TAG_MIGRATION_DESCRIPTIONS,
    DEFAULT_TARGET_ONTOLOGY_DESCRIPTIONS,
    SUBJECT_TSV_COLUMNS,
    SUMMARY_COLUMNS,
    AgentReportRow,
    ReportGenerator,
    SubjectReportRow,
    derive_quality_brackets,
    describe_agent_vocabulary,
    describe_source_thesaurus,
    describe_tag_migration,
    describe_target_ontology,
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


def test_agent_reports_dynamic_vocabularies(tmp_path: Path) -> None:
    cfg = AppConfig()
    cfg.reconcile = ReconcileConfig(
        target_file=Path("data/test.xml"),
        agent_reports=ReportSetPathsConfig(detailed_file=Path("a.tsv"), summary_file=Path("b.tsv")),
        subject_reports=ReportSetPathsConfig(
            detailed_file=Path("c.tsv"), summary_file=Path("d.tsv")
        ),
        agent_priority=["GND", "KANTO", "CUSTOM_AUTH"],
    )

    reporter = ReportGenerator(cfg)
    reporter.add_agent_row(
        AgentReportRow(
            target_record_id="T1",
            target_title="Book 1",
            target_year=2020,
            target_isbn=None,
            field_tag="100",
            field_occurrence=1,
            subfield_code="0",
            target_heading="Author 1",
            target_role="aut",
            uri="http://d-nb.info/gnd/12345",
            vocabulary="GND",
            authority_label="Author 1",
            authority_record_id="GND123",
            authority_source="dnb",
            match_tier="tier1_exact_id",
            match_score=1.0,
            dense_score=None,
            rerank_score=None,
            agent_name_score=100.0,
            action="injected",
            is_novel=True,
        )
    )
    reporter.add_agent_row(
        AgentReportRow(
            target_record_id="T2",
            target_title="Book 2",
            target_year=2021,
            target_isbn=None,
            field_tag="700",
            field_occurrence=1,
            subfield_code="0",
            target_heading="Author 2",
            target_role="edt",
            uri="http://custom.org/123",
            vocabulary="CUSTOM_AUTH",
            authority_label="Author 2",
            authority_record_id="C123",
            authority_source="custom",
            match_tier="tier2_dense",
            match_score=0.92,
            dense_score=0.92,
            rerank_score=None,
            agent_name_score=90.0,
            action="injected",
            is_novel=True,
        )
    )
    reporter.add_agent_row(
        AgentReportRow(
            target_record_id="T3",
            target_title="Book 3",
            target_year=2022,
            target_isbn=None,
            field_tag="700",
            field_occurrence=2,
            subfield_code="0",
            target_heading="Author 3",
            target_role=None,
            uri="http://other.org/999",
            vocabulary="OTHER",
            authority_label=None,
            authority_record_id="O999",
            authority_source="external",
            match_tier="tier3_hybrid_rerank",
            match_score=0.86,
            dense_score=0.80,
            rerank_score=0.86,
            agent_name_score=85.0,
            action="proposed",
            is_novel=True,
        )
    )

    sum_path = tmp_path / "agent_summary_dynamic.tsv"
    reporter.write_agent_summary_report(sum_path)

    lines = sum_path.read_text(encoding="utf-8").splitlines()
    vocab_rows = [line.split("\t") for line in lines if line.startswith("vocabularies\t")]
    vocab_metrics = [r[1] for r in vocab_rows]

    # Verify order: configured priorities ("GND", "KANTO", "CUSTOM_AUTH") followed by unlisted ("OTHER")
    assert vocab_metrics == ["GND", "KANTO", "CUSTOM_AUTH", "OTHER"]

    # Verify counts & percentages (3 total rows)
    row_map = {r[1]: (r[2], r[3], r[4]) for r in vocab_rows}
    assert row_map["GND"][:2] == ("1", "33.3%")
    assert row_map["KANTO"][:2] == ("0", "0.0%")
    assert row_map["CUSTOM_AUTH"][:2] == ("1", "33.3%")
    assert row_map["OTHER"][:2] == ("1", "33.3%")

    # Verify descriptions: clean authority descriptions with MARC ($0) subfield
    assert "KANTO agent identifier URIs ($0)" in row_map["KANTO"][2]
    assert "CUSTOM_AUTH agent identifier URIs ($0)" in row_map["CUSTOM_AUTH"][2]


def test_subject_reports_suffixed_vocabularies(tmp_path: Path) -> None:
    cfg = AppConfig()
    reporter = ReportGenerator(cfg)
    # Add rows where target_vocab has language subcode (e.g. yso/fin, slm/swe)
    reporter.add_subject_row(
        SubjectReportRow(
            target_record_id="T1",
            target_title="Title 1",
            source_field_tag="650",
            field_occurrence=1,
            source_heading="term1",
            source_vocab="ysa",
            source_uri=None,
            language="fi",
            target_field_tag="650",
            target_vocab="yso/fin",
            target_uri="http://www.yso.fi/onto/yso/p1",
            target_pref_label="term1",
            mapping_type="exact_match",
            action="injected",
            is_novel=True,
            tag_changed=False,
        )
    )
    reporter.add_subject_row(
        SubjectReportRow(
            target_record_id="T2",
            target_title="Title 2",
            source_field_tag="655",
            field_occurrence=1,
            source_heading="term2",
            source_vocab="kaunokki",
            source_uri=None,
            language="sv",
            target_field_tag="655",
            target_vocab="slm/swe",
            target_uri="http://urn.fi/URN:NBN:fi:au:slm:s1",
            target_pref_label="term2",
            mapping_type="exact_match",
            action="injected",
            is_novel=True,
            tag_changed=False,
        )
    )

    sum_path = tmp_path / "subj_summary_suffixed.tsv"
    reporter.write_subject_summary_report(sum_path)

    lines = sum_path.read_text(encoding="utf-8").splitlines()
    target_rows = [line.split("\t") for line in lines if line.startswith("target_ontologies\t")]
    target_map = {r[1]: (r[2], r[3]) for r in target_rows}

    # Regression test: yso/fin and slm/swe must correctly count toward base ontologies 'yso' and 'slm'
    assert target_map["yso"] == ("1", "50.0%")
    assert target_map["slm"] == ("1", "50.0%")


def test_subject_reports_custom_thesauri_and_no_double_counting(tmp_path: Path) -> None:
    cfg = AppConfig()
    reporter = ReportGenerator(cfg)
    reporter.add_subject_row(
        SubjectReportRow(
            target_record_id="T1",
            target_title="Title",
            source_field_tag="650",
            field_occurrence=1,
            source_heading="h1",
            source_vocab="mesh",
            source_uri=None,
            language="en",
            target_field_tag="650",
            target_vocab="yso/fin",
            target_uri="http://www.yso.fi/onto/yso/p1",
            target_pref_label="h1",
            mapping_type="exact_match",
            action="injected",
            is_novel=True,
            tag_changed=False,
        )
    )
    reporter.add_subject_row(
        SubjectReportRow(
            target_record_id="T2",
            target_title="Title",
            source_field_tag="650",
            field_occurrence=1,
            source_heading="h2",
            source_vocab="cilla",
            source_uri=None,
            language="sv",
            target_field_tag="650",
            target_vocab="muso/swe",
            target_uri="http://www.yso.fi/onto/muso/m1",
            target_pref_label="h2",
            mapping_type="exact_match",
            action="injected",
            is_novel=True,
            tag_changed=False,
        )
    )
    reporter.add_subject_row(
        SubjectReportRow(
            target_record_id="T3",
            target_title="Title",
            source_field_tag="650",
            field_occurrence=1,
            source_heading="h3",
            source_vocab="bella",
            source_uri=None,
            language="sv",
            target_field_tag="650",
            target_vocab="kauno/swe",
            target_uri="http://www.yso.fi/onto/kauno/k1",
            target_pref_label="h3",
            mapping_type="exact_match",
            action="injected",
            is_novel=True,
            tag_changed=False,
        )
    )
    reporter.add_subject_row(
        SubjectReportRow(
            target_record_id="T4",
            target_title="Title",
            source_field_tag="650",
            field_occurrence=1,
            source_heading="h4",
            source_vocab=None,
            source_uri=None,
            language="fi",
            target_field_tag="650",
            target_vocab="yso/fin",
            target_uri="http://www.yso.fi/onto/yso/p4",
            target_pref_label="h4",
            mapping_type="exact_match",
            action="injected",
            is_novel=True,
            tag_changed=False,
        )
    )

    sum_path = tmp_path / "subj_summary_custom.tsv"
    reporter.write_subject_summary_report(sum_path)

    lines = sum_path.read_text(encoding="utf-8").splitlines()
    thesauri_rows = [line.split("\t") for line in lines if line.startswith("source_thesauri\t")]
    thesauri_map = {r[1]: r[2] for r in thesauri_rows}

    # Verify separate counts for cilla, bella, and custom mesh
    assert thesauri_map["mesh"] == "1"
    assert thesauri_map["cilla"] == "1"
    assert thesauri_map["bella"] == "1"
    # Verify unspecified_legacy only counts the term with source_vocab=None
    assert thesauri_map["unspecified_legacy"] == "1"
    # Ensure artificial double-counting bucket 'cilla_bella' does not exist
    assert "cilla_bella" not in thesauri_map


def test_subject_reports_dynamic_tag_migrations(tmp_path: Path) -> None:
    cfg = AppConfig()
    reporter = ReportGenerator(cfg)
    reporter.add_subject_row(
        SubjectReportRow(
            target_record_id="T1",
            target_title="Title",
            source_field_tag="651",
            field_occurrence=1,
            source_heading="Helsinki",
            source_vocab="local",
            source_uri=None,
            language="fi",
            target_field_tag="650",
            target_vocab="yso/fin",
            target_uri="http://www.yso.fi/onto/yso/p99",
            target_pref_label="Helsinki",
            mapping_type="exact_match",
            action="injected",
            is_novel=True,
            tag_changed=True,
        )
    )

    sum_path = tmp_path / "subj_summary_migrations.tsv"
    reporter.write_subject_summary_report(sum_path)

    lines = sum_path.read_text(encoding="utf-8").splitlines()
    mig_rows = [line.split("\t") for line in lines if line.startswith("tag_migrations\t")]
    mig_map = {r[1]: r[2] for r in mig_rows}

    # Verify novel 651_to_650_yso migration appears dynamically with count 1
    assert "651_to_650_yso" in mig_map
    assert mig_map["651_to_650_yso"] == "1"


def test_reports_none_config(tmp_path: Path) -> None:
    reporter = ReportGenerator(config=None)
    assert reporter.config is not None

    reporter.add_agent_row(
        AgentReportRow(
            target_record_id="T1",
            target_title="Title",
            target_year=2020,
            target_isbn=None,
            field_tag="100",
            field_occurrence=1,
            subfield_code="0",
            target_heading="Author",
            target_role="aut",
            uri="http://urn.fi/URN:NBN:fi:au:pn:0001",
            vocabulary="KANTO",
            authority_label="Author",
            authority_record_id="A1",
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
    reporter.add_subject_row(
        SubjectReportRow(
            target_record_id="T1",
            target_title="Title",
            source_field_tag="650",
            field_occurrence=1,
            source_heading="Heading",
            source_vocab="ysa",
            source_uri=None,
            language="fi",
            target_field_tag="650",
            target_vocab="yso/fin",
            target_uri="http://www.yso.fi/onto/yso/p1",
            target_pref_label="Heading",
            mapping_type="exact_match",
            action="injected",
            is_novel=True,
            tag_changed=False,
        )
    )

    agent_sum = tmp_path / "agent_sum.tsv"
    subj_sum = tmp_path / "subj_sum.tsv"

    reporter.write_agent_summary_report(agent_sum)
    reporter.write_subject_summary_report(subj_sum)

    assert agent_sum.exists()
    assert subj_sum.exists()


def test_derive_quality_brackets_default() -> None:
    # Default config: threshold 0.85, short_circuit 0.98
    brackets = derive_quality_brackets(None)
    metrics = [b.metric for b in brackets]
    assert metrics == [
        "score_0.98_to_1.00",
        "score_0.92_to_0.98",
        "score_0.85_to_0.92",
        "score_below_0.85",
    ]
    # Check boundary matching
    b_top, b_mid, b_low, b_under = brackets
    assert b_top.matches(1.00)
    assert b_top.matches(0.98)
    assert not b_top.matches(0.9799)

    assert b_mid.matches(0.9799)
    assert b_mid.matches(0.92)
    assert not b_mid.matches(0.9199)

    assert b_low.matches(0.9199)
    assert b_low.matches(0.85)
    assert not b_low.matches(0.8499)

    assert b_under.matches(0.8499)
    assert b_under.matches(0.0)
    assert not b_under.matches(0.85)


def test_derive_quality_brackets_custom_threshold() -> None:
    rec_cfg = ReconcileConfig(
        target_file=Path("data/test.xml"),
        agent_reports=ReportSetPathsConfig(detailed_file=Path("a.tsv"), summary_file=Path("b.tsv")),
        subject_reports=ReportSetPathsConfig(
            detailed_file=Path("c.tsv"), summary_file=Path("d.tsv")
        ),
        threshold=0.75,
    )
    brackets = derive_quality_brackets(rec_cfg)
    metrics = [b.metric for b in brackets]
    assert metrics == [
        "score_0.98_to_1.00",
        "score_0.86_to_0.98",
        "score_0.75_to_0.86",
        "score_below_0.75",
    ]


def test_derive_quality_brackets_disabled_short_circuit() -> None:
    rec_cfg = ReconcileConfig(
        target_file=Path("data/test.xml"),
        agent_reports=ReportSetPathsConfig(detailed_file=Path("a.tsv"), summary_file=Path("b.tsv")),
        subject_reports=ReportSetPathsConfig(
            detailed_file=Path("c.tsv"), summary_file=Path("d.tsv")
        ),
        rerank=RerankConfig(short_circuit=ShortCircuitConfig(enabled=False)),
    )
    # When short circuit disabled, defaults to 0.95 cutoff
    brackets = derive_quality_brackets(rec_cfg)
    metrics = [b.metric for b in brackets]
    assert metrics == [
        "score_0.95_to_1.00",
        "score_0.90_to_0.95",
        "score_0.85_to_0.90",
        "score_below_0.85",
    ]


def test_derive_quality_brackets_explicit_user_override() -> None:
    rec_cfg = ReconcileConfig(
        target_file=Path("data/test.xml"),
        agent_reports=ReportSetPathsConfig(detailed_file=Path("a.tsv"), summary_file=Path("b.tsv")),
        subject_reports=ReportSetPathsConfig(
            detailed_file=Path("c.tsv"), summary_file=Path("d.tsv")
        ),
        quality_brackets=[0.70, 0.85, 0.95],
    )
    brackets = derive_quality_brackets(rec_cfg)
    metrics = [b.metric for b in brackets]
    assert metrics == [
        "score_0.95_to_1.00",
        "score_0.85_to_0.95",
        "score_0.70_to_0.85",
        "score_below_0.70",
    ]


def test_agent_reports_quality_and_execution_metadata(tmp_path: Path) -> None:
    cfg = AppConfig()
    reporter = ReportGenerator(cfg)
    reporter.total_records = 4
    reporter.matched_records = 3

    # Add 4 rows across all 4 tiers
    rows_data = [
        (0.99, "injected"),  # score_0.98_to_1.00
        (0.95, "injected"),  # score_0.92_to_0.98
        (0.88, "injected"),  # score_0.85_to_0.92
        (0.60, "below_threshold"),  # score_below_0.85
    ]
    for idx, (score, action) in enumerate(rows_data, start=1):
        reporter.add_agent_row(
            AgentReportRow(
                target_record_id=f"T{idx}",
                target_title=f"Title {idx}",
                target_year=2020,
                target_isbn=None,
                field_tag="100",
                field_occurrence=1,
                subfield_code="0",
                target_heading=f"Author {idx}",
                target_role="aut",
                uri=f"http://urn.fi/URN:NBN:fi:au:pn:000{idx}",
                vocabulary="KANTO",
                authority_label=f"Author {idx}",
                authority_record_id=f"A{idx}",
                authority_source="melinda",
                match_tier="tier3_hybrid_rerank" if score < 0.98 else "tier2_dense",
                match_score=score,
                dense_score=score,
                rerank_score=score,
                agent_name_score=90.0,
                action=action,
                is_novel=True,
            )
        )

    sum_path = tmp_path / "agent_summary.tsv"
    reporter.write_agent_summary_report(sum_path)

    lines = sum_path.read_text(encoding="utf-8").splitlines()
    rows = [line.split("\t") for line in lines[1:]]
    exec_map = {r[1]: r[2] for r in rows if r[0] == "execution"}
    quality_map = {r[1]: (r[2], r[3]) for r in rows if r[0] == "quality"}

    # Verify execution metadata
    assert exec_map["threshold"] == "0.8500"
    assert exec_map["short_circuit_threshold"] == "0.9800"
    assert exec_map["rerank_engine"] == "bm25"
    assert exec_map["dense_weight"] == "0.6000"
    assert exec_map["rerank_weight"] == "0.4000"

    # Verify quality brackets complete accounting: 1 row in each bucket = 25.0% each
    assert quality_map["score_0.98_to_1.00"] == ("1", "25.0%")
    assert quality_map["score_0.92_to_0.98"] == ("1", "25.0%")
    assert quality_map["score_0.85_to_0.92"] == ("1", "25.0%")
    assert quality_map["score_below_0.85"] == ("1", "25.0%")


def test_agent_reports_custom_threshold_and_explicit_brackets(tmp_path: Path) -> None:
    cfg = AppConfig()
    cfg.reconcile = ReconcileConfig(
        target_file=Path("data/test.xml"),
        agent_reports=ReportSetPathsConfig(detailed_file=Path("a.tsv"), summary_file=Path("b.tsv")),
        subject_reports=ReportSetPathsConfig(
            detailed_file=Path("c.tsv"), summary_file=Path("d.tsv")
        ),
        threshold=0.75,
        quality_brackets=[0.70, 0.85, 0.95],
    )
    reporter = ReportGenerator(cfg)

    scores = [0.99, 0.90, 0.80, 0.50]
    for idx, s in enumerate(scores, start=1):
        reporter.add_agent_row(
            AgentReportRow(
                target_record_id=f"T{idx}",
                target_title="Title",
                target_year=2020,
                target_isbn=None,
                field_tag="100",
                field_occurrence=1,
                subfield_code="0",
                target_heading="Author",
                target_role="aut",
                uri=f"http://urn.fi/URN:NBN:fi:au:pn:000{idx}",
                vocabulary="KANTO",
                authority_label="Author",
                authority_record_id=f"A{idx}",
                authority_source="melinda",
                match_tier="tier2_dense",
                match_score=s,
                dense_score=s,
                rerank_score=None,
                agent_name_score=100.0,
                action="injected",
                is_novel=True,
            )
        )

    sum_path = tmp_path / "custom_agent_summary.tsv"
    reporter.write_agent_summary_report(sum_path)

    lines = sum_path.read_text(encoding="utf-8").splitlines()
    quality_rows = [line.split("\t") for line in lines if line.startswith("quality\t")]
    q_map = {r[1]: (r[2], r[3]) for r in quality_rows}

    assert "score_0.95_to_1.00" in q_map
    assert "score_0.85_to_0.95" in q_map
    assert "score_0.70_to_0.85" in q_map
    assert "score_below_0.70" in q_map

    assert q_map["score_0.95_to_1.00"] == ("1", "25.0%")
    assert q_map["score_0.85_to_0.95"] == ("1", "25.0%")
    assert q_map["score_0.70_to_0.85"] == ("1", "25.0%")
    assert q_map["score_below_0.70"] == ("1", "25.0%")


def test_agent_reports_acceptance_threshold_captures_borderline(tmp_path: Path) -> None:
    cfg = AppConfig()
    cfg.reconcile = ReconcileConfig(
        target_file=Path("data/test.xml"),
        agent_reports=ReportSetPathsConfig(detailed_file=Path("a.tsv"), summary_file=Path("b.tsv")),
        subject_reports=ReportSetPathsConfig(
            detailed_file=Path("c.tsv"), summary_file=Path("d.tsv")
        ),
        threshold=0.75,
    )
    reporter = ReportGenerator(cfg)
    reporter.add_agent_row(
        AgentReportRow(
            target_record_id="T1",
            target_title="Borderline Book",
            target_year=2020,
            target_isbn=None,
            field_tag="100",
            field_occurrence=1,
            subfield_code="0",
            target_heading="Author",
            target_role="aut",
            uri="http://urn.fi/URN:NBN:fi:au:pn:0001",
            vocabulary="KANTO",
            authority_label="Author",
            authority_record_id="A1",
            authority_source="melinda",
            match_tier="tier3_hybrid_rerank",
            match_score=0.78,
            dense_score=0.75,
            rerank_score=0.82,
            agent_name_score=80.0,
            action="injected",
            is_novel=True,
        )
    )

    sum_path = tmp_path / "borderline_summary.tsv"
    reporter.write_agent_summary_report(sum_path)

    lines = sum_path.read_text(encoding="utf-8").splitlines()
    quality_rows = [line.split("\t") for line in lines if line.startswith("quality\t")]
    q_map = {r[1]: (r[2], r[3]) for r in quality_rows}

    # Match with 0.78 must fall in score_0.75_to_0.86 (100.0%) and not below
    assert q_map["score_0.75_to_0.86"] == ("1", "100.0%")
    assert q_map["score_below_0.75"] == ("0", "0.0%")


def test_describe_tag_migration() -> None:
    # Retagging with target vocabulary
    assert describe_tag_migration("650_to_655_slm") == "Terms in 650 retagged to 655 SLM"
    assert describe_tag_migration("651_to_650_yso") == "Terms in 651 retagged to 650 YSO"

    # Intra-field vocabulary folding
    assert describe_tag_migration("655_kaunokki_to_slm") == "In 655, KAUNOKKI folded to SLM"
    assert describe_tag_migration("655_bella_to_slm") == "In 655, BELLA folded to SLM"
    assert describe_tag_migration("650_mesh_to_yso") == "In 650, MESH folded to YSO"
    assert describe_tag_migration("650_ysa_to_yso") == "In 650, YSA folded to YSO"
    assert describe_tag_migration("382_cilla_to_seko") == "In 382, CILLA folded to SEKO"

    # Pure tag retagging
    assert describe_tag_migration("650_to_655") == "Terms in 650 retagged to 655"

    # Dynamic generation for retagging + folding
    assert (
        describe_tag_migration("650_kaunokki_to_655_slm")
        == "Terms in 650 (KAUNOKKI) retagged to 655 SLM"
    )

    # Fallback for arbitrary key
    assert (
        describe_tag_migration("custom_migration") == "Subject migration/folding custom_migration"
    )


def test_subject_reports_dynamic_tag_migrations_and_foldings(tmp_path: Path) -> None:
    cfg = AppConfig()
    reporter = ReportGenerator(cfg)

    # 1. Retagging without source vocab: 650 -> 655 (SLM)
    reporter.add_subject_row(
        SubjectReportRow(
            target_record_id="T1",
            target_title="Title 1",
            source_field_tag="650",
            field_occurrence=1,
            source_heading="fantasiakirjallisuus",
            source_vocab=None,
            source_uri=None,
            language="fi",
            target_field_tag="655",
            target_vocab="slm",
            target_uri="http://urn.fi/URN:NBN:fi:au:slm:s1",
            target_pref_label="fantasiakirjallisuus",
            mapping_type="exact_match",
            action="injected",
            is_novel=True,
            tag_changed=True,
        )
    )

    # 2. Finnish Kaunokki folding within 655 -> SLM
    reporter.add_subject_row(
        SubjectReportRow(
            target_record_id="T2",
            target_title="Title 2",
            source_field_tag="655",
            field_occurrence=1,
            source_heading="sadut",
            source_vocab="kaunokki",
            source_uri=None,
            language="fi",
            target_field_tag="655",
            target_vocab="slm",
            target_uri="http://urn.fi/URN:NBN:fi:au:slm:s2",
            target_pref_label="sadut",
            mapping_type="exact_match",
            action="injected",
            is_novel=True,
            tag_changed=False,
        )
    )

    # 3. Swedish Bella folding within 655 -> SLM (must NOT be lumped into kaunokki!)
    reporter.add_subject_row(
        SubjectReportRow(
            target_record_id="T3",
            target_title="Title 3",
            source_field_tag="655",
            field_occurrence=1,
            source_heading="sagor",
            source_vocab="bella",
            source_uri=None,
            language="sv",
            target_field_tag="655",
            target_vocab="slm/swe",
            target_uri="http://urn.fi/URN:NBN:fi:au:slm:s2",
            target_pref_label="sagor",
            mapping_type="exact_match",
            action="injected",
            is_novel=True,
            tag_changed=False,
        )
    )

    # 4. Swedish Cilla folding in 382 -> SEKO (must NOT be lumped into musa!)
    reporter.add_subject_row(
        SubjectReportRow(
            target_record_id="T4",
            target_title="Title 4",
            source_field_tag="382",
            field_occurrence=1,
            source_heading="piano",
            source_vocab="cilla",
            source_uri=None,
            language="sv",
            target_field_tag="382",
            target_vocab="seko",
            target_uri="http://urn.fi/URN:NBN:fi:au:seko:p1",
            target_pref_label="piano",
            mapping_type="exact_match",
            action="injected",
            is_novel=True,
            tag_changed=False,
        )
    )

    # 5. External MESH folding in 650 -> YSO
    reporter.add_subject_row(
        SubjectReportRow(
            target_record_id="T5",
            target_title="Title 5",
            source_field_tag="650",
            field_occurrence=1,
            source_heading="Anatomy",
            source_vocab="mesh",
            source_uri=None,
            language="en",
            target_field_tag="650",
            target_vocab="yso",
            target_uri="http://www.yso.fi/onto/yso/p1234",
            target_pref_label="Anatomia",
            mapping_type="exact_match",
            action="injected",
            is_novel=True,
            tag_changed=False,
        )
    )

    sum_path = tmp_path / "dynamic_tag_migrations.tsv"
    reporter.write_subject_summary_report(sum_path)

    lines = sum_path.read_text(encoding="utf-8").splitlines()
    mig_rows = [line.split("\t") for line in lines if line.startswith("tag_migrations\t")]
    mig_map = {r[1]: (r[2], r[4]) for r in mig_rows}

    # Verify each migration/folding is distinctly tracked and accurately described
    assert mig_map["650_to_655_slm"][0] == "1"
    assert mig_map["655_kaunokki_to_slm"][0] == "1"
    assert mig_map["655_bella_to_slm"][0] == "1"
    assert mig_map["382_cilla_to_seko"][0] == "1"
    assert mig_map["650_mesh_to_yso"][0] == "1"
    assert "In 650, MESH folded to YSO" in mig_map["650_mesh_to_yso"][1]


def test_subject_reports_no_tag_migrations(tmp_path: Path) -> None:
    cfg = AppConfig()
    reporter = ReportGenerator(cfg)
    reporter.add_subject_row(
        SubjectReportRow(
            target_record_id="T1",
            target_title="Title",
            source_field_tag="650",
            field_occurrence=1,
            source_heading="Heading",
            source_vocab="yso",
            source_uri="http://www.yso.fi/onto/yso/p1",
            language="fi",
            target_field_tag="650",
            target_vocab="yso",
            target_uri="http://www.yso.fi/onto/yso/p1",
            target_pref_label="Heading",
            mapping_type="exact_match",
            action="existing",
            is_novel=False,
            tag_changed=False,
        )
    )

    sum_path = tmp_path / "no_mig_summary.tsv"
    reporter.write_subject_summary_report(sum_path)

    lines = sum_path.read_text(encoding="utf-8").splitlines()
    mig_rows = [line.split("\t") for line in lines if line.startswith("tag_migrations\t")]
    assert len(mig_rows) == 1
    mig_map = {r[1]: r[2] for r in mig_rows}
    assert mig_map.get("none") == "0"


def test_agent_reports_configurable_rerank_engine(tmp_path: Path) -> None:
    # 1. Neural cross-encoder engine
    cfg_ce = AppConfig()
    cfg_ce.reconcile = ReconcileConfig(
        target_file=Path("data/test.xml"),
        agent_reports=ReportSetPathsConfig(detailed_file=Path("a.tsv"), summary_file=Path("b.tsv")),
        subject_reports=ReportSetPathsConfig(
            detailed_file=Path("c.tsv"), summary_file=Path("d.tsv")
        ),
        rerank=RerankConfig(engine="cross_encoder"),
    )
    rep_ce = ReportGenerator(cfg_ce)
    rep_ce.add_agent_row(
        AgentReportRow(
            target_record_id="T1",
            target_title="Title",
            target_year=2020,
            target_isbn=None,
            field_tag="100",
            field_occurrence=1,
            subfield_code="0",
            target_heading="Author",
            target_role="aut",
            uri="http://urn.fi/URN:NBN:fi:au:pn:0001",
            vocabulary="KANTO",
            authority_label="Author",
            authority_record_id="A1",
            authority_source="melinda",
            match_tier="tier3_hybrid_rerank",
            match_score=0.90,
            dense_score=0.88,
            rerank_score=0.92,
            agent_name_score=90.0,
            action="injected",
            is_novel=True,
        )
    )

    sum_path_ce = tmp_path / "ce_agent_summary.tsv"
    rep_ce.write_agent_summary_report(sum_path_ce)
    lines_ce = sum_path_ce.read_text(encoding="utf-8").splitlines()
    exec_map_ce = {
        r[1]: (r[2], r[4])
        for r in [line.split("\t") for line in lines_ce if line.startswith("execution\t")]
    }
    tiers_map_ce = {
        r[1]: (r[2], r[4])
        for r in [line.split("\t") for line in lines_ce if line.startswith("tiers\t")]
    }

    assert exec_map_ce["rerank_engine"][0] == "cross_encoder"
    assert "neural cross-encoder" in exec_map_ce["rerank_weight"][1]
    assert "neural cross-encoder" in tiers_map_ce["tier3_hybrid_rerank"][1]

    # 2. Disabled rerank engine
    cfg_off = AppConfig()
    cfg_off.reconcile = ReconcileConfig(
        target_file=Path("data/test.xml"),
        agent_reports=ReportSetPathsConfig(detailed_file=Path("a.tsv"), summary_file=Path("b.tsv")),
        subject_reports=ReportSetPathsConfig(
            detailed_file=Path("c.tsv"), summary_file=Path("d.tsv")
        ),
        rerank=RerankConfig(enabled=False),
    )
    rep_off = ReportGenerator(cfg_off)
    rep_off.add_agent_row(
        AgentReportRow(
            target_record_id="T2",
            target_title="Title 2",
            target_year=2020,
            target_isbn=None,
            field_tag="100",
            field_occurrence=1,
            subfield_code="0",
            target_heading="Author",
            target_role="aut",
            uri="http://urn.fi/URN:NBN:fi:au:pn:0002",
            vocabulary="KANTO",
            authority_label="Author",
            authority_record_id="A2",
            authority_source="melinda",
            match_tier="tier2_dense",
            match_score=0.92,
            dense_score=0.92,
            rerank_score=None,
            agent_name_score=90.0,
            action="injected",
            is_novel=True,
        )
    )

    sum_path_off = tmp_path / "off_agent_summary.tsv"
    rep_off.write_agent_summary_report(sum_path_off)
    lines_off = sum_path_off.read_text(encoding="utf-8").splitlines()
    exec_map_off = {
        r[1]: (r[2], r[4])
        for r in [line.split("\t") for line in lines_off if line.startswith("execution\t")]
    }
    tiers_map_off = {
        r[1]: (r[2], r[4])
        for r in [line.split("\t") for line in lines_off if line.startswith("tiers\t")]
    }

    assert exec_map_off["rerank_engine"][0] == "disabled"
    assert "disabled" in exec_map_off["rerank_weight"][1]
    assert "disabled" in tiers_map_off["tier3_hybrid_rerank"][1]


def test_dynamic_vocabulary_descriptions() -> None:
    # 1. Agent vocabulary descriptions (no verbose expansions; readers know the abbreviations)
    assert describe_agent_vocabulary("KANTO") == "KANTO agent identifier URIs ($0)"
    assert describe_agent_vocabulary("kanto") == "KANTO agent identifier URIs ($0)"
    assert describe_agent_vocabulary("ASTERI") == "ASTERI agent identifier URIs ($0)"
    assert describe_agent_vocabulary("WIKIDATA") == "Wikidata entity QID URIs ($1)"
    assert describe_agent_vocabulary("wd") == "Wikidata entity QID URIs ($1)"
    assert describe_agent_vocabulary("ISNI") == "ISNI agent identifier URIs ($0)"
    assert describe_agent_vocabulary("VIAF") == "VIAF agent identifier URIs ($0)"
    assert describe_agent_vocabulary("OTHER") == "Unclassified or external agent identifier URIs"
    assert (
        describe_agent_vocabulary("unclassified")
        == "Unclassified or external agent identifier URIs"
    )
    assert describe_agent_vocabulary("GND") == "GND agent identifier URIs ($0)"
    assert (
        describe_agent_vocabulary("custom_authority")
        == "CUSTOM_AUTHORITY agent identifier URIs ($0)"
    )
    assert describe_agent_vocabulary("") == "Unspecified agent vocabulary"

    # Backwards compatibility mapping for agent vocabs
    assert DEFAULT_AGENT_VOCAB_DESCRIPTIONS["KANTO"] == "KANTO agent identifier URIs ($0)"
    assert DEFAULT_AGENT_VOCAB_DESCRIPTIONS["GND"] == "GND agent identifier URIs ($0)"
    assert "CUSTOM_AUTHORITY" in DEFAULT_AGENT_VOCAB_DESCRIPTIONS

    # 2. Source thesaurus descriptions
    assert describe_source_thesaurus("ysa") == "Terms originated from YSA"
    assert describe_source_thesaurus("YSA") == "Terms originated from YSA"
    assert describe_source_thesaurus("kaunokki") == "Terms originated from Kaunokki"
    assert describe_source_thesaurus("allars") == "Terms originated from Allärs"
    assert describe_source_thesaurus("musa") == "Terms originated from MUSA"
    assert describe_source_thesaurus("cilla") == "Terms originated from Cilla"
    assert describe_source_thesaurus("bella") == "Terms originated from Bella"
    assert describe_source_thesaurus("mesh") == "Terms originated from MESH"
    assert describe_source_thesaurus("custom") == "Terms originated from Custom"
    assert (
        describe_source_thesaurus("unspecified_legacy") == "Legacy terms with no $2 or local tags"
    )
    assert describe_source_thesaurus("") == "Legacy terms with no $2 or local tags"

    # Backwards compatibility mapping for source thesauri
    assert DEFAULT_SOURCE_THESAURUS_DESCRIPTIONS["ysa"] == "Terms originated from YSA"
    assert DEFAULT_SOURCE_THESAURUS_DESCRIPTIONS["mesh"] == "Terms originated from MESH"
    assert (
        DEFAULT_SOURCE_THESAURUS_DESCRIPTIONS["unspecified_legacy"]
        == "Legacy terms with no $2 or local tags"
    )

    # 3. Target ontology descriptions (no verbose expansions; readers know the abbreviations)
    assert describe_target_ontology("yso") == "Mapped to YSO"
    assert describe_target_ontology("YSO") == "Mapped to YSO"
    assert describe_target_ontology("slm") == "Mapped to SLM"
    assert describe_target_ontology("kauno") == "Mapped to KAUNO"
    assert describe_target_ontology("muso") == "Mapped to MUSO"
    assert describe_target_ontology("seko") == "Mapped to SEKO"
    assert describe_target_ontology("yso-paikat") == "Mapped to YSO-paikat"
    assert describe_target_ontology("wikidata") == "Mapped to Wikidata Entity URIs"
    assert describe_target_ontology("wd") == "Mapped to Wikidata Entity URIs"
    assert describe_target_ontology("lcsh") == "Mapped to LCSH"
    assert describe_target_ontology("custom_onto") == "Mapped to CUSTOM_ONTO"
    assert describe_target_ontology("") == "Unspecified target ontology"

    # Backwards compatibility mapping for target ontologies
    assert DEFAULT_TARGET_ONTOLOGY_DESCRIPTIONS["yso"] == "Mapped to YSO"
    assert DEFAULT_TARGET_ONTOLOGY_DESCRIPTIONS["lcsh"] == "Mapped to LCSH"
    assert (
        DEFAULT_TAG_MIGRATION_DESCRIPTIONS["650_to_655_slm"] == "Terms in 650 retagged to 655 SLM"
    )
