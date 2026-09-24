"""Guards for the ingest -> cluster -> reconcile -> enrich -> export round trip.

The exported corpus must retain every field it arrived with. Enrichment may add
subfields, retag a field when a vocabulary folds (650 -> 655), and replace a
legacy heading with its ontology preferred label, but it must never drop data and
must never relabel a heading it failed to resolve.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import numpy as np
from lxml import etree

from reconciliator.commands import ingest_oai_cmd, ingest_vocab_cmd, reconcile_cmd, reset_cmd

FIXTURES = Path(__file__).parent / "fixtures"
MARC = "{http://www.loc.gov/MARC21/slim}"

CORPUS = """<?xml version="1.0" encoding="UTF-8"?>
<collection xmlns="http://www.loc.gov/MARC21/slim">
  <record>
    <leader>00000nam a2200000 i 4500</leader>
    <controlfield tag="001">LOSS-001</controlfield>
    <controlfield tag="003">FI-TEST</controlfield>
    <controlfield tag="005">20240101120000.0</controlfield>
    <controlfield tag="008">210101s1945    fi |||||||||||||||||fin||</controlfield>
    <datafield tag="020" ind1=" " ind2=" ">
      <subfield code="a">978-951-0-00001-0</subfield>
      <subfield code="q">nidottu</subfield>
    </datafield>
    <datafield tag="041" ind1="0" ind2=" ">
      <subfield code="a">fin</subfield>
      <subfield code="a">swe</subfield>
    </datafield>
    <datafield tag="100" ind1="1" ind2=" ">
      <subfield code="a">Waltari, Mika,</subfield>
      <subfield code="d">1908-1979.</subfield>
    </datafield>
    <datafield tag="245" ind1="1" ind2="0">
      <subfield code="a">Sinuhe egyptiläinen :</subfield>
      <subfield code="b">viisitoista kirjaa</subfield>
    </datafield>
    <datafield tag="500" ind1=" " ind2=" ">
      <subfield code="a">Erikoismerkit: &amp; &lt; &gt; åäö — "lainaus".</subfield>
    </datafield>
    <datafield tag="650" ind1=" " ind2=" ">
      <subfield code="a">lääkärit</subfield>
      <subfield code="2">ysa</subfield>
    </datafield>
    <datafield tag="650" ind1=" " ind2=" ">
      <subfield code="a">historialliset romaanit</subfield>
      <subfield code="2">kaunokki</subfield>
    </datafield>
    <datafield tag="650" ind1=" " ind2=" ">
      <subfield code="a">täysin tuntematon aihe</subfield>
      <subfield code="2">ysa</subfield>
    </datafield>
    <datafield tag="700" ind1="1" ind2=" ">
      <subfield code="a">Tuntematon, Tekijä,</subfield>
    </datafield>
    <datafield tag="880" ind1="1" ind2="0">
      <subfield code="6">245-01/Cyrl</subfield>
      <subfield code="a">Синухе</subfield>
    </datafield>
    <datafield tag="999" ind1=" " ind2=" ">
      <subfield code="a">paikallinen arvo</subfield>
      <subfield code="b">toinen</subfield>
    </datafield>
  </record>
</collection>
"""


def _mock_transformer() -> MagicMock:
    model = MagicMock()
    model.get_embedding_dimension.return_value = 128
    model.get_sentence_embedding_dimension.return_value = 128
    model.encode.side_effect = lambda texts, **kw: np.zeros((len(texts), 128), dtype=np.float32)
    return model


def _run_pipeline(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Run reset -> ingest-vocab -> ingest-oai -> reconcile; return (input, output, reports)."""
    reports = tmp_path / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    target = tmp_path / "corpus.xml"
    target.write_text(CORPUS, encoding="utf-8")
    out_xml = tmp_path / "exported.xml"
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
  batch_size: 16

ingestion:
  vocab:
    input_path: "{FIXTURES / "sample_finto.ttl"}"
    vocabularies: ["yso", "kauno", "muso", "slm", "seko"]
    legacy_thesauri: ["ysa", "allars", "kaunokki", "musa"]

  oai:
    melinda:
      enabled: true
      source_type: "file"
      input_path: "{FIXTURES / "sample_melinda.xml"}"
      workers: 1
      batch_size: 10

reconcile:
  target_file: "{target}"
  output_file: "{out_xml}"
  enrich: true
  fold_legacy: true
  threshold: 0.85
  cluster:
    enabled: true
  agent_reports:
    detailed_file: "{reports}/agent_uris.tsv"
    summary_file: "{reports}/agent_summary.tsv"
  subject_reports:
    detailed_file: "{reports}/subject_uris.tsv"
    summary_file: "{reports}/subject_summary.tsv"
  link_policy: "priority_fallback"
  vocabularies: ["KANTO_PN", "KANTO_CN", "YSO", "MUSO", "KAUNO", "SLM", "SEKO", "VIAF", "ISNI", "WIKIDATA"]
  agent_priority: ["KANTO_PN", "KANTO_CN", "ISNI", "VIAF", "WIKIDATA"]
  subject_priority: ["YSO", "KAUNO", "MUSO", "SLM", "SEKO"]

telemetry:
  heartbeat_interval_seconds: 0.1
  watch_interval_seconds: 0.1
""",
        encoding="utf-8",
    )

    with patch(
        "reconciliator.embedding.embedder.SentenceTransformer", return_value=_mock_transformer()
    ):
        reset_cmd(config=cfg, yes=True)
        ingest_vocab_cmd(config=cfg)
        ingest_oai_cmd(config=cfg)
        reconcile_cmd(config=cfg)

    return target, out_xml, reports


def _fields(path: Path) -> dict[str, dict[str, Any]]:
    """record_id -> {leader, controlfields, datafields as (tag, ind1, ind2, subfields)}."""
    root = etree.parse(str(path)).getroot()
    out: dict[str, dict[str, Any]] = {}
    for rec in root.findall(f".//{MARC}record"):
        controls = {
            c.get("tag"): (c.text or "").strip() for c in rec.findall(f"{MARC}controlfield")
        }
        leader = rec.find(f"{MARC}leader")
        data = [
            (
                d.get("tag"),
                d.get("ind1"),
                d.get("ind2"),
                tuple(
                    (s.get("code"), (s.text or "").strip()) for s in d.findall(f"{MARC}subfield")
                ),
            )
            for d in rec.findall(f"{MARC}datafield")
        ]
        out[controls["001"]] = {
            "leader": (leader.text or "").strip() if leader is not None else None,
            "controls": controls,
            "data": data,
        }
    return out


def test_export_preserves_every_input_field(tmp_path: Path) -> None:
    target, out_xml, _ = _run_pipeline(tmp_path)
    src, dst = _fields(target), _fields(out_xml)

    assert set(src) == set(dst), "record set changed"
    s, d = src["LOSS-001"], dst["LOSS-001"]

    assert s["leader"] == d["leader"]
    assert s["controls"] == d["controls"], "controlfields must survive untouched"

    out_subfield_values = {(code, text) for _, _, _, subs in d["data"] for code, text in subs}
    out_tags = [t for t, _, _, _ in d["data"]]

    # The invariant: every subfield value that arrived must still be somewhere in
    # the export. Enrichment may add $0/$1 and retag a folded field, so field
    # identity is not required — but no catalogued value may vanish.
    #
    # The only permitted exceptions are the two deliberate rewrites: a resolved
    # heading's $2 is restated in the target vocabulary, and its $a in the
    # ontology's preferred label. Both are preserved in subject_uris.tsv.
    permitted_rewrites = {("2", "ysa"), ("2", "kaunokki")}
    for tag, _i1, _i2, subs in s["data"]:
        for code, text in subs:
            if (code, text) in permitted_rewrites:
                continue
            assert (code, text) in out_subfield_values, (
                f"subfield lost from export: {tag} ${code} {text!r}"
            )

    # Fields untouched by folding must survive with tag, indicators and subfields intact.
    for tag, i1, i2, subs in s["data"]:
        if any((c, t) in permitted_rewrites for c, t in subs):
            continue
        if tag in ("100", "700", "110", "710"):
            continue  # agent fields legitimately gain $0/$1
        assert (tag, i1, i2, subs) in d["data"], f"field lost or altered: {tag} {subs}"

    # Specifically: local, alternate-graphic and repeated subfields survive.
    assert ("999", " ", " ", (("a", "paikallinen arvo"), ("b", "toinen"))) in d["data"]
    assert ("880", "1", "0", (("6", "245-01/Cyrl"), ("a", "Синухе"))) in d["data"]
    assert ("041", "0", " ", (("a", "fin"), ("a", "swe"))) in d["data"]
    assert ("a", 'Erikoismerkit: & < > åäö — "lainaus".') in out_subfield_values

    # And enrichment actually happened.
    assert ("0", "http://www.yso.fi/onto/yso/p2591") in out_subfield_values
    assert ("0", "https://urn.fi/URN:NBN:fi:au:slm:s123") in out_subfield_values
    assert ("0", "https://urn.fi/URN:NBN:fi:au:pn:000123456") in out_subfield_values
    assert "655" in out_tags, "kaunokki heading should fold from 650 to 655"


def test_unresolved_heading_is_left_exactly_as_catalogued(tmp_path: Path) -> None:
    """A heading with no mapping keeps its original $a and $2 and gains no $0.

    Stamping a default target vocabulary on an unresolved heading would assert
    membership of a vocabulary that does not contain the term, and would destroy
    the original $2 needed to correct it by hand.
    """
    _, out_xml, _ = _run_pipeline(tmp_path)
    dst = _fields(out_xml)["LOSS-001"]

    unresolved = [
        f for f in dst["data"] if any(c == "a" and t == "täysin tuntematon aihe" for c, t in f[3])
    ]
    assert len(unresolved) == 1
    tag, _, _, subs = unresolved[0]
    assert tag == "650", "unresolved heading must not be retagged"
    assert subs == (("a", "täysin tuntematon aihe"), ("2", "ysa")), (
        f"unresolved heading was modified: {subs}"
    )


def test_every_agent_appears_in_the_agent_report(tmp_path: Path) -> None:
    """Unlinked agents still get a row, so link coverage is measurable."""
    _, _, reports = _run_pipeline(tmp_path)
    lines = (reports / "agent_uris.tsv").read_text(encoding="utf-8").splitlines()
    header = lines[0].split("\t")
    rows = [dict(zip(header, ln.split("\t"))) for ln in lines[1:]]

    by_heading = {r["target_heading"]: r for r in rows}
    assert "Waltari, Mika" in by_heading
    assert "Tuntematon, Tekijä" in by_heading, "unlinked agent missing from report"

    linked = [r for r in rows if r["target_heading"] == "Waltari, Mika"]
    assert any(r["action"] == "injected" and r["uri"] for r in linked)
    # A URI the link policy declined is still recorded rather than silently dropped.
    assert any(r["action"] == "suppressed_by_priority" for r in linked)

    unlinked = by_heading["Tuntematon, Tekijä"]
    assert unlinked["action"] == "unmatched"
    assert unlinked["uri"] == ""


def test_reconcile_is_idempotent(tmp_path: Path) -> None:
    """Re-running on an exported corpus must not duplicate or drop anything."""
    _, out_xml, reports = _run_pipeline(tmp_path)

    second_out = tmp_path / "exported_twice.xml"
    cfg2 = tmp_path / "config2.yaml"
    original = (tmp_path / "config.yaml").read_text(encoding="utf-8")
    cfg2.write_text(
        original.replace(
            f'target_file: "{tmp_path / "corpus.xml"}"', f'target_file: "{out_xml}"'
        ).replace(f'output_file: "{out_xml}"', f'output_file: "{second_out}"'),
        encoding="utf-8",
    )

    with patch(
        "reconciliator.embedding.embedder.SentenceTransformer", return_value=_mock_transformer()
    ):
        reconcile_cmd(config=cfg2)

    once, twice = _fields(out_xml), _fields(second_out)
    assert set(once) == set(twice)
    for rid in once:
        assert sorted(once[rid]["data"]) == sorted(twice[rid]["data"]), (
            f"second pass changed record {rid}"
        )
