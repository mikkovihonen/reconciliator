"""Work linking: intra-corpus clustering, and global linking across authority sources.

Internal linking must group manifestations of one work and must not merge distinct
works. Global linking must give every member of a work cluster the same authority
work URI, and the work report must state the URI that was actually written.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pytest
from lxml import etree

from reconciliator.cli import ingest_oai_cmd, ingest_vocab_cmd, reconcile_cmd, reset_cmd
from reconciliator.config import load_config
from reconciliator.repository import WEMIRepository

FIXTURES = Path(__file__).parent / "fixtures"
MARC = "{http://www.loc.gov/MARC21/slim}"


def _marc(rec_id: str, title: str, author: str, year: str, isbn: str, org: str) -> str:
    return f"""  <record>
    <controlfield tag="001">{rec_id}</controlfield>
    <controlfield tag="003">{org}</controlfield>
    <controlfield tag="008">210101s{year}    fi |||||||||||||||||fin||</controlfield>
    <datafield tag="020" ind1=" " ind2=" "><subfield code="a">{isbn}</subfield></datafield>
    <datafield tag="100" ind1="1" ind2=" "><subfield code="a">{author}</subfield></datafield>
    <datafield tag="245" ind1="1" ind2="0"><subfield code="a">{title}</subfield></datafield>
    <datafield tag="260" ind1=" " ind2=" "><subfield code="b">WSOY</subfield><subfield code="c">{year}</subfield></datafield>
  </record>
"""


def _collection(records: list[str]) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<collection xmlns="http://www.loc.gov/MARC21/slim">\n' + "".join(records) + "</collection>\n"
    )


# Three manifestations of one work, plus two distinct works (one sharing the author).
TARGET = _collection(
    [
        _marc("T-SIN-1", "Sinuhe egyptiläinen", "Waltari, Mika,", "1945", "9789510000011", "FI-T"),
        _marc("T-SIN-2", "Sinuhe egyptiläinen", "Waltari, Mika,", "1949", "9789510000022", "FI-T"),
        _marc("T-SIN-3", "Sinuhe egyptiläinen", "Waltari, Mika,", "1983", "9789510000033", "FI-T"),
        _marc("T-KARV-1", "Mikael Karvajalka", "Waltari, Mika,", "1948", "9789510000044", "FI-T"),
        _marc("T-TUNT-1", "Tuntematon sotilas", "Linna, Väinö,", "1954", "9789510000055", "FI-T"),
    ]
)

# Two authority sources each holding the same work, reachable by different ISBNs.
AUTH_A = _collection(
    [_marc("A-001", "Sinuhe egyptiläinen", "Waltari, Mika,", "1945", "9789510000011", "FI-SRCA")]
)
AUTH_B = _collection(
    [_marc("B-777", "Sinuhe egyptiläinen", "Waltari, Mika,", "1949", "9789510000022", "FI-SRCB")]
)


@pytest.fixture(scope="module")
def linked(tmp_path_factory: pytest.TempPathFactory) -> dict[str, object]:
    """Run the pipeline once over two authority sources; return the artefacts."""
    tmp = tmp_path_factory.mktemp("work_linking")
    reports = tmp / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    (tmp / "target.xml").write_text(TARGET, encoding="utf-8")
    (tmp / "auth_a.xml").write_text(AUTH_A, encoding="utf-8")
    (tmp / "auth_b.xml").write_text(AUTH_B, encoding="utf-8")
    out_xml = tmp / "out.xml"
    cfg = tmp / "config.yaml"
    templates = Path(__file__).parent.parent / "src" / "reconciliator" / "templates"

    cfg.write_text(
        f"""
storage:
  data_dir: "{tmp}"
embedding:
  model_name: "TurkuNLP/sbert-cased-finnish-paraphrase"
  template_path: "{templates / 'default_serialization.j2'}"
  device: "cpu"
ingest_vocab:
  input_path: "{FIXTURES / 'sample_finto.ttl'}"
  vocabularies: ["yso", "kauno", "muso", "slm", "seko"]
  legacy_thesauri: ["ysa", "allars", "kaunokki", "musa"]
ingest_oai:
  source_a:
    enabled: true
    source_type: "file"
    input_path: "{tmp / 'auth_a.xml'}"
    workers: 1
    batch_size: 10
  source_b:
    enabled: true
    source_type: "file"
    input_path: "{tmp / 'auth_b.xml'}"
    workers: 1
    batch_size: 10
reconcile:
  target_file: "{tmp / 'target.xml'}"
  output_file: "{out_xml}"
  enrich: true
  fold_legacy: true
  threshold: 0.85
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
  vocabularies: ["KANTO", "YSO", "SLM", "SEKO", "VIAF", "WIKIDATA"]
  agent_priority: ["KANTO", "ISNI", "VIAF", "WIKIDATA"]
  subject_priority: ["YSO", "KAUNO", "MUSO", "SLM", "SEKO"]
telemetry:
  heartbeat_interval_seconds: 0.1
""",
        encoding="utf-8",
    )

    model = MagicMock()
    model.get_embedding_dimension.return_value = 128
    model.get_sentence_embedding_dimension.return_value = 128
    model.encode.side_effect = lambda t, **k: np.zeros((len(t), 128), dtype=np.float32)

    with patch("reconciliator.embedder.SentenceTransformer", return_value=model):
        reset_cmd(config=cfg, yes=True)
        ingest_vocab_cmd(config=cfg)
        ingest_oai_cmd(config=cfg, all_sources=True)
        reconcile_cmd(config=cfg)

    conf = load_config(cfg)
    repo = WEMIRepository(conf.storage.authority_db, fts_config=conf.storage.fts)

    linkages = {lk.record_id: lk for lk in repo.get_all_corpus_linkages()}
    clusters = {c.cluster_id: c for c in repo.get_all_work_clusters()}

    lines = (reports / "work.tsv").read_text(encoding="utf-8").splitlines()
    header = lines[0].split("\t")
    work_rows = {
        (r := dict(zip(header, ln.split("\t"))))["target_record_id"]: r for ln in lines[1:]
    }

    exported: dict[str, list[str]] = {}
    for rec in etree.parse(str(out_xml)).getroot().findall(f".//{MARC}record"):
        rid = rec.find(f"{MARC}controlfield[@tag='001']").text
        uris = []
        for df in rec.findall(f"{MARC}datafield"):
            if df.get("tag") in ("758", "240", "787", "730"):
                uris += [
                    s.text
                    for s in df.findall(f"{MARC}subfield")
                    if s.get("code") == "0" and s.text and ":work:" in s.text
                ]
        exported[rid] = uris

    return {
        "repo": repo,
        "linkages": linkages,
        "clusters": clusters,
        "work_rows": work_rows,
        "exported": exported,
    }


def _cluster_of(linkages: dict, rec_id: str) -> str:
    key = next(k for k in linkages if rec_id in k)
    return linkages[key].work_cluster_id


# ---------------------------------------------------------------------------
# Internal linking, within the corpus
# ---------------------------------------------------------------------------


def test_manifestations_of_one_work_share_a_cluster(linked: dict) -> None:
    linkages = linked["linkages"]
    clusters = {_cluster_of(linkages, r) for r in ("T-SIN-1", "T-SIN-2", "T-SIN-3")}
    assert len(clusters) == 1, f"three manifestations of one work split across {clusters}"


def test_distinct_works_are_not_merged(linked: dict) -> None:
    linkages = linked["linkages"]
    sinuhe = _cluster_of(linkages, "T-SIN-1")
    karvajalka = _cluster_of(linkages, "T-KARV-1")  # same author, different work
    tuntematon = _cluster_of(linkages, "T-TUNT-1")  # different author and work

    assert karvajalka != sinuhe, "a different work by the same author was merged in"
    assert tuntematon != sinuhe, "an unrelated work was merged in"
    assert karvajalka != tuntematon, "two unrelated works were merged together"


def test_unmatched_work_still_gets_a_stable_internal_uri(linked: dict) -> None:
    """A work absent from every authority source is still linkable within the corpus."""
    clusters, linkages = linked["clusters"], linked["linkages"]
    cluster = clusters[_cluster_of(linkages, "T-TUNT-1")]
    assert cluster.canonical_work_uri.startswith("urn:reconciliator:work:")
    assert linked["exported"]["T-TUNT-1"] == [cluster.canonical_work_uri]


# ---------------------------------------------------------------------------
# Global linking, across authority sources
# ---------------------------------------------------------------------------


def test_cluster_adopts_an_authority_work_uri(linked: dict) -> None:
    clusters, linkages = linked["clusters"], linked["linkages"]
    cluster = clusters[_cluster_of(linkages, "T-SIN-1")]
    assert not cluster.canonical_work_uri.startswith("urn:reconciliator:"), (
        "a cluster matching an authority work should adopt its URI, not mint one"
    )
    assert cluster.canonical_work_uri.startswith("urn:work:source_")


def test_every_cluster_member_exports_the_same_work_uri(linked: dict) -> None:
    """The point of clustering: siblings must not disagree about their work."""
    clusters, linkages, exported = linked["clusters"], linked["linkages"], linked["exported"]
    canonical = clusters[_cluster_of(linkages, "T-SIN-1")].canonical_work_uri
    for rec_id in ("T-SIN-1", "T-SIN-2", "T-SIN-3"):
        assert exported[rec_id] == [canonical], (
            f"{rec_id} exported {exported[rec_id]}, expected {canonical}"
        )


def test_work_report_states_the_uri_that_was_written(linked: dict) -> None:
    """matched_work_uri must equal what landed in the record, not a rival match.

    Sibling records can match different authority sources on their own
    identifiers; the cluster's canonical URI is what gets written, so that is
    what the report has to say.
    """
    work_rows, exported = linked["work_rows"], linked["exported"]
    for rid, row in work_rows.items():
        bare = rid.split(")")[-1]
        assert exported[bare] == [row["matched_work_uri"]], (
            f"{rid}: report says {row['matched_work_uri']!r} but record has {exported[bare]}"
        )


def test_record_level_match_is_retained_as_provenance(linked: dict) -> None:
    """Where a sibling matched a different source, that resolution stays visible."""
    work_rows = linked["work_rows"]
    divergent = [
        r
        for r in work_rows.values()
        if r["record_level_work_uri"]
        and r["record_level_work_uri"] != r["matched_work_uri"]
    ]
    assert divergent, (
        "expected at least one record whose own match differed from its cluster's "
        "canonical URI, since the two authority sources hold the same work"
    )
    # Both sides of the divergence are real authority URIs, i.e. the same work
    # exists under two source identities.
    for row in divergent:
        assert row["record_level_work_uri"].startswith("urn:work:source_")
        assert row["matched_work_uri"].startswith("urn:work:source_")


def test_sources_keep_separate_identities_until_consolidation_runs(linked: dict) -> None:
    """Ingest alone leaves one work row per source; consolidation is a separate step.

    This fixture deliberately does not run `consolidate-works`, so it pins the
    pre-consolidation state: two sources holding the same work give two work
    rows with no equivalence edge between them. See test_work_consolidation.py
    for the behaviour once consolidation has run.
    """
    repo: WEMIRepository = linked["repo"]  # type: ignore[assignment]
    with repo.get_connection() as conn:
        works = conn.execute("SELECT source, work_uri FROM works ORDER BY id").fetchall()
        same_work_edges = conn.execute(
            "SELECT COUNT(*) c FROM work_relationships WHERE relationship_type = 'same_work_as';"
        ).fetchone()["c"]

    assert len(works) == 2, "fixture holds the same work in two sources"
    assert {w["source"] for w in works} == {"source_a", "source_b"}
    assert same_work_edges == 0, "consolidation was not run for this fixture"
    assert repo.get_work_equivalence_map() == {}
