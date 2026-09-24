"""Cross-source work identity consolidation.

Each source mints its own work URI, so one work held by several catalogues
arrives as several rows. Consolidation elects one canonical row per equivalence
group and records same_work_as edges explaining why, without deleting anything.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pytest
from lxml import etree
from sqlalchemy import func, select

from reconciliator.commands import (
    consolidate_works_cmd,
    ingest_oai_cmd,
    ingest_vocab_cmd,
    reconcile_cmd,
    reset_cmd,
)
from reconciliator.config import VectorEngineConfig, WorkConsolidationConfig, load_config
from reconciliator.database import WEMIRepository, works_table
from reconciliator.models import (
    AgentEntity,
    ContributionEntity,
    ExpressionEntity,
    IdentifierEntity,
    ManifestationEntity,
    WorkEntity,
)
from reconciliator.reconciliation.consolidation import WorkConsolidator

FIXTURES = Path(__file__).parent / "fixtures"
MARC = "{http://www.loc.gov/MARC21/slim}"


def _repo(tmp_path: Path) -> WEMIRepository:
    return WEMIRepository(
        db_path=tmp_path / "authority.db",
        work_index_path=tmp_path / "work.index",
        dimension=8,
        vector_engine_config=VectorEngineConfig(backend="usearch", device="cpu", mode="embedded"),
    )


def _add_work(
    repo: WEMIRepository,
    source: str,
    source_id: str,
    title: str,
    author: str | None = "Waltari, Mika",
    isbn: str | None = None,
    qid: str | None = None,
    work_type: str = "text",
) -> int:
    """Insert one authority work with its manifestation, creator and identifiers."""
    curie = f"{source}:{source_id}"
    agents = []
    contributions = []
    if author:
        agents.append(
            AgentEntity(
                source=source,
                source_id=f"agent-{source_id}",
                name=author,
                name_normalized=author.lower(),
                surname=author.split(",")[0],
            )
        )
        contributions.append(
            ContributionEntity(
                entity_type="work",
                entity_id=0,
                agent_id=0,
                entity_curie=curie,
                agent_curie=f"{source}:agent-{source_id}",
                role="aut",
                is_primary=True,
            )
        )
    identifiers = []
    if isbn:
        identifiers.append(
            IdentifierEntity(
                entity_type="manifestation",
                entity_id=0,
                entity_curie=curie,
                source="isbn",
                source_id=isbn,
                curie=f"isbn:{isbn}",
                source_id_normalized=isbn,
            )
        )
    if qid:
        identifiers.append(
            IdentifierEntity(
                entity_type="work",
                entity_id=0,
                entity_curie=curie,
                source="wikidata",
                source_id=qid,
                curie=f"wikidata:{qid}",
                source_id_normalized=qid.lower(),
            )
        )
    repo.insert_wemi_batch(
        works=[
            WorkEntity(
                source=source,
                source_id=source_id,
                record_origin="authority",
                title=title,
                title_normalized=title.strip().lower(),
                work_type=work_type,  # type: ignore[arg-type]
            )
        ],
        expressions=[
            ExpressionEntity(
                work_id=0,
                work_curie=curie,
                source=source,
                source_id=source_id,
                language="fin",
                content_type="text",
                title=title,
            )
        ],
        manifestations=[
            ManifestationEntity(
                work_id=0,
                work_curie=curie,
                source=source,
                source_id=source_id,
                title=title,
                title_normalized=title.strip().lower(),
                carrier_type="volume",
            )
        ],
        agents=agents,
        identifiers=identifiers,
        contributions=contributions,
        record_origin="authority",
    )
    work = repo.get_work_by_source(source, source_id)
    assert work is not None and work.id is not None
    return work.id


# ---------------------------------------------------------------------------
# Evidence tiers
# ---------------------------------------------------------------------------


def test_consolidates_on_shared_wikidata_qid(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    # Titles differ (one is a translation heading) but the QID is authoritative.
    a = _add_work(repo, "source_a", "A1", "Sinuhe egyptiläinen", qid="Q123")
    b = _add_work(repo, "source_b", "B1", "The Egyptian", qid="Q123")

    result = WorkConsolidator(repo).consolidate()
    assert result.groups == [[a, b]]
    assert set(result.evidence.values()) == {"wikidata_qid"}
    assert repo.get_work_equivalence_map() == {b: a}


def test_distinct_qids_are_never_merged(tmp_path: Path) -> None:
    """An explicit QID disagreement outranks identical metadata."""
    repo = _repo(tmp_path)
    _add_work(repo, "source_a", "A1", "Sinuhe egyptiläinen", qid="Q123")
    _add_work(repo, "source_b", "B1", "Sinuhe egyptiläinen", qid="Q999")

    assert WorkConsolidator(repo).consolidate().groups == []


def test_consolidates_on_shared_identifier(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    a = _add_work(repo, "source_a", "A1", "Sinuhe egyptiläinen", isbn="9789510000011")
    b = _add_work(repo, "source_b", "B1", "Sinuhe Egyptiläinen", isbn="9789510000011")

    result = WorkConsolidator(repo).consolidate()
    assert result.groups == [[a, b]]
    assert set(result.evidence.values()) == {"identifier:isbn"}


def test_consolidates_on_metadata_without_any_identifier(tmp_path: Path) -> None:
    """The identifier-free case: title plus creator plus work type carry it."""
    repo = _repo(tmp_path)
    a = _add_work(repo, "source_a", "A1", "Sinuhe egyptiläinen")
    b = _add_work(repo, "source_b", "B1", "Sinuhe egyptiläinen")

    with repo.get_connection() as conn:
        assert conn.exec_driver_sql("SELECT COUNT(*) FROM identifiers").scalar() == 0

    result = WorkConsolidator(repo).consolidate()
    assert result.groups == [[a, b]]
    assert all(e.startswith("metadata:") for e in result.evidence.values())


def test_different_works_by_the_same_author_are_not_merged(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _add_work(repo, "source_a", "A1", "Sinuhe egyptiläinen")
    _add_work(repo, "source_b", "B1", "Mikael Karvajalka")

    assert WorkConsolidator(repo).consolidate().groups == []


def test_same_title_different_author_is_not_merged(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _add_work(repo, "source_a", "A1", "Muistelmat", author="Waltari, Mika")
    _add_work(repo, "source_b", "B1", "Muistelmat", author="Linna, Väinö")

    assert WorkConsolidator(repo).consolidate().groups == []


def test_same_title_different_work_type_is_not_merged(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _add_work(repo, "source_a", "A1", "Sinuhe egyptiläinen", work_type="text")
    _add_work(repo, "source_b", "B1", "Sinuhe egyptiläinen", work_type="music")

    assert WorkConsolidator(repo).consolidate().groups == []


def test_same_source_duplicates_are_left_alone_by_default(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _add_work(repo, "source_a", "A1", "Sinuhe egyptiläinen")
    _add_work(repo, "source_a", "A2", "Sinuhe egyptiläinen")

    assert WorkConsolidator(repo).consolidate().groups == []

    repo.clear_canonical_works()
    opted_in = WorkConsolidator(repo, WorkConsolidationConfig(include_same_source=True))
    assert len(opted_in.consolidate().groups) == 1


# ---------------------------------------------------------------------------
# Grouping, canonical election, persistence
# ---------------------------------------------------------------------------


def test_three_sources_form_one_transitive_group(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    a = _add_work(repo, "source_a", "A1", "Sinuhe egyptiläinen", isbn="9789510000011")
    b = _add_work(repo, "source_b", "B1", "Sinuhe egyptiläinen", isbn="9789510000022")
    c = _add_work(repo, "source_c", "C1", "Sinuhe egyptiläinen", isbn="9789510000033")

    result = WorkConsolidator(repo).consolidate()
    assert result.groups == [[a, b, c]]
    assert result.consolidated_works == 2
    mapping = repo.get_work_equivalence_map()
    assert set(mapping) == {b, c}
    assert set(mapping.values()) == {a}


def test_wikidata_backed_work_is_elected_canonical(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    # source_a sorts first by URI, but source_b carries the QID.
    _add_work(repo, "source_a", "A1", "Sinuhe egyptiläinen", isbn="9789510000011")
    b = _add_work(repo, "source_b", "B1", "Sinuhe egyptiläinen", isbn="9789510000011", qid="Q123")

    result = WorkConsolidator(repo).consolidate()
    assert set(result.canonical_by_work.values()) == {b}


def test_canonical_source_priority_is_honoured(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _add_work(repo, "source_a", "A1", "Sinuhe egyptiläinen", isbn="9789510000011")
    b = _add_work(repo, "source_b", "B1", "Sinuhe egyptiläinen", isbn="9789510000011")

    cfg = WorkConsolidationConfig(canonical_source_priority=["source_b", "source_a"])
    result = WorkConsolidator(repo, cfg).consolidate()
    assert set(result.canonical_by_work.values()) == {b}
    # The repository agrees, because the election is stored rather than recomputed.
    assert set(repo.get_work_equivalence_map().values()) == {b}


def test_edges_explain_the_grouping_and_nothing_is_deleted(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    a = _add_work(repo, "source_a", "A1", "Sinuhe egyptiläinen", isbn="9789510000011")
    b = _add_work(repo, "source_b", "B1", "Sinuhe egyptiläinen", isbn="9789510000011")

    WorkConsolidator(repo).consolidate()
    with repo.get_connection() as conn:
        assert conn.exec_driver_sql("SELECT COUNT(*) FROM works").scalar() == 2, (
            "consolidation must not delete work rows"
        )
        edges = (
            conn.exec_driver_sql(
                "SELECT source_work_id, target_work_id FROM work_relationships "
                "WHERE relationship_type = 'same_work_as' ORDER BY source_work_id;"
            )
            .mappings()
            .fetchall()
        )
    assert [(e["source_work_id"], e["target_work_id"]) for e in edges] == [(a, b), (b, a)], (
        "same_work_as is symmetric"
    )


def test_consolidation_is_idempotent(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _add_work(repo, "source_a", "A1", "Sinuhe egyptiläinen", isbn="9789510000011")
    _add_work(repo, "source_b", "B1", "Sinuhe egyptiläinen", isbn="9789510000011")

    first = WorkConsolidator(repo).consolidate()
    second = WorkConsolidator(repo).consolidate()
    assert first.groups == second.groups
    assert second.edges_written == 0, "re-running must not duplicate edges"


def test_dry_run_writes_nothing(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _add_work(repo, "source_a", "A1", "Sinuhe egyptiläinen", isbn="9789510000011")
    _add_work(repo, "source_b", "B1", "Sinuhe egyptiläinen", isbn="9789510000011")

    result = WorkConsolidator(repo).consolidate(dry_run=True)
    assert len(result.groups) == 1, "a dry run still reports what it would do"
    assert result.edges_written == 0
    assert repo.get_work_equivalence_map() == {}


def test_clear_undoes_consolidation(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _add_work(repo, "source_a", "A1", "Sinuhe egyptiläinen", isbn="9789510000011")
    _add_work(repo, "source_b", "B1", "Sinuhe egyptiläinen", isbn="9789510000011")

    WorkConsolidator(repo).consolidate()
    assert repo.get_work_equivalence_map()

    repo.clear_canonical_works()
    assert repo.get_work_equivalence_map() == {}
    with repo.get_connection() as conn:
        assert (
            conn.exec_driver_sql(
                "SELECT COUNT(*) FROM work_relationships WHERE relationship_type='same_work_as';"
            ).scalar()
            == 0
        )


def test_disabled_config_is_a_no_op(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _add_work(repo, "source_a", "A1", "Sinuhe egyptiläinen", isbn="9789510000011")
    _add_work(repo, "source_b", "B1", "Sinuhe egyptiläinen", isbn="9789510000011")

    cfg = WorkConsolidationConfig(enabled=False)
    assert WorkConsolidator(repo, cfg).consolidate().groups == []


# ---------------------------------------------------------------------------
# End to end through the CLI
# ---------------------------------------------------------------------------


def _marc(rec_id: str, title: str, year: str, isbn: str, org: str) -> str:
    return f"""  <record>
    <controlfield tag="001">{rec_id}</controlfield>
    <controlfield tag="003">{org}</controlfield>
    <controlfield tag="008">210101s{year}    fi |||||||||||||||||fin||</controlfield>
    <datafield tag="020" ind1=" " ind2=" "><subfield code="a">{isbn}</subfield></datafield>
    <datafield tag="100" ind1="1" ind2=" "><subfield code="a">Waltari, Mika,</subfield></datafield>
    <datafield tag="245" ind1="1" ind2="0"><subfield code="a">{title}</subfield></datafield>
  </record>
"""


def _collection(records: list[str]) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<collection xmlns="http://www.loc.gov/MARC21/slim">\n'
        + "".join(records)
        + "</collection>\n"
    )


@pytest.fixture
def consolidated_run(tmp_path: Path) -> dict[str, object]:
    """Ingest two sources holding one work, consolidate, then reconcile a corpus."""
    reports = tmp_path / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    (tmp_path / "auth_a.xml").write_text(
        _collection([_marc("A-001", "Sinuhe egyptiläinen", "1945", "9789510000011", "FI-SRCA")]),
        encoding="utf-8",
    )
    (tmp_path / "auth_b.xml").write_text(
        _collection([_marc("B-777", "Sinuhe egyptiläinen", "1949", "9789510000022", "FI-SRCB")]),
        encoding="utf-8",
    )
    # Two corpus records, each matching a different source on its own ISBN.
    (tmp_path / "target.xml").write_text(
        _collection(
            [
                _marc("T-1", "Sinuhe egyptiläinen", "1945", "9789510000011", "FI-T"),
                _marc("T-2", "Sinuhe egyptiläinen", "1949", "9789510000022", "FI-T"),
            ]
        ),
        encoding="utf-8",
    )
    out_xml = tmp_path / "out.xml"
    cfg = tmp_path / "config.yaml"
    templates = Path(__file__).parent.parent / "src" / "reconciliator" / "templates"
    cfg.write_text(
        f"""
storage:
  data_dir: "{tmp_path}"
embedding:
  model_name: "TurkuNLP/sbert-cased-finnish-paraphrase"
  template_path: "{templates / "default_serialization.j2"}"
  device: "cpu"
ingestion:
  vocab:
    input_path: "{FIXTURES / "sample_finto.ttl"}"
    vocabularies: ["yso"]
    legacy_thesauri: ["ysa"]

  oai:
    source_a:
      enabled: true
      source_type: "file"
      input_path: "{tmp_path / "auth_a.xml"}"
      batch_size: 10
    source_b:
      enabled: true
      source_type: "file"
      input_path: "{tmp_path / "auth_b.xml"}"
      batch_size: 10
reconcile:
  target_file: "{tmp_path / "target.xml"}"
  output_file: "{out_xml}"
  enrich: true
  cluster:
    enabled: true
  agent_reports:
    detailed_file: "{reports}/agent.tsv"
    summary_file: "{reports}/agent_s.tsv"
  subject_reports:
    detailed_file: "{reports}/subject.tsv"
    summary_file: "{reports}/subject_s.tsv"
  work_reports:
    detailed_file: "{reports}/work.tsv"
    summary_file: "{reports}/work_s.tsv"
telemetry:
  heartbeat_interval_seconds: 0.1
""",
        encoding="utf-8",
    )

    model = MagicMock()
    model.get_embedding_dimension.return_value = 128
    model.get_sentence_embedding_dimension.return_value = 128
    model.encode.side_effect = lambda t, **k: np.zeros((len(t), 128), dtype=np.float32)

    with patch("reconciliator.embedding.embedder.SentenceTransformer", return_value=model):
        reset_cmd(config=cfg, yes=True)
        ingest_vocab_cmd(config=cfg)
        ingest_oai_cmd(config=cfg, all_sources=True)
        consolidate_works_cmd(config=cfg, dry_run=False, reset=False)
        reconcile_cmd(config=cfg)

    conf = load_config(cfg)
    repo = WEMIRepository(conf.storage.database_url, fts_config=conf.storage.fts)

    exported: dict[str, list[str]] = {}
    for rec in etree.parse(str(out_xml)).getroot().findall(f".//{MARC}record"):
        rid = rec.find(f"{MARC}controlfield[@tag='001']").text
        exported[rid] = [
            s.text
            for df in rec.findall(f"{MARC}datafield")
            if df.get("tag") == "758"
            for s in df.findall(f"{MARC}subfield")
            if s.get("code") == "0" and s.text and ":exp" not in s.text
        ]

    lines = (reports / "work.tsv").read_text(encoding="utf-8").splitlines()
    header = lines[0].split("\t")
    rows = [dict(zip(header, ln.split("\t"))) for ln in lines[1:]]
    return {"repo": repo, "exported": exported, "rows": rows}


def test_cli_consolidation_gives_both_sources_one_identity(consolidated_run: dict) -> None:
    repo: WEMIRepository = consolidated_run["repo"]  # type: ignore[assignment]
    mapping = repo.get_work_equivalence_map()
    assert len(mapping) == 1, "one of the two source works should fold into the other"
    with repo.get_connection() as conn:
        # Scoped to authority rows: the reconciled corpus is stored as WEMI in
        # the same database now and contributes works of its own.
        auth = repo.authoritative_sources()
        assert (
            conn.execute(
                select(func.count())
                .select_from(works_table)
                .where(works_table.c.record_origin.in_(auth))
            ).scalar()
            == 2
        )


def test_records_matching_different_sources_export_one_work_uri(
    consolidated_run: dict,
) -> None:
    """The point of consolidation: the source a record matched stops mattering."""
    exported = consolidated_run["exported"]
    assert exported["T-1"], "T-1 should carry a work link"
    assert exported["T-1"] == exported["T-2"], (
        f"records matching different sources disagree: {exported}"
    )


def test_work_report_agrees_with_the_record_after_consolidation(
    consolidated_run: dict,
) -> None:
    exported, rows = consolidated_run["exported"], consolidated_run["rows"]
    for row in rows:
        bare = row["target_record_id"].split(")")[-1]
        assert exported[bare] == [row["matched_work_uri"]]
        # With one identity there is no rival match left to record.
        assert row["record_level_work_uri"] == row["matched_work_uri"]
