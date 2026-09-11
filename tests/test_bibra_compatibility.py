"""Unit and integration tests for NatLibFi/BIBRA compatibility."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import numpy as np
from lxml import etree

from reconciliator.cli import ingest_oai_cmd, ingest_vocab_cmd, reconcile_cmd, reset_cmd
from reconciliator.loaders.bibra import (
    BibraPublicationMetadata,
    bibra_to_marcxml_elem,
    bibra_to_raw_record,
    enrich_bibra_json,
    iter_bibra_records,
)
from reconciliator.loaders.marcxml import _get_subfields
from reconciliator.matcher import ReconciliationResult
from reconciliator.models import Agent, BibliographicRecord
from reconciliator.normalizers.record import RecordNormalizer


def test_bibra_publication_metadata_model() -> None:
    payload = {
        "language": "fi",
        "title": "Kustannus- ja jakeluketjun vaiheet",
        "alt_title": "Stages of Publishing",
        "creator": ["Laitinen, Antti", "Virtanen, Maija"],
        "year": "2024",
        "publisher": ["Suomen yliopistojen kirjatoimisto"],
        "doi": "10.1234/test.5678",
        "e-isbn": ["978-952-7159-75-1"],
        "p-isbn": ["978-952-7159-74-4"],
        "e-issn": "2345-6789",
        "p-issn": "1234-5678",
        "type_coar": "research report",
    }
    model = BibraPublicationMetadata.model_validate(payload)
    assert model.language == "fi"
    assert model.title == "Kustannus- ja jakeluketjun vaiheet"
    assert model.alt_title == "Stages of Publishing"
    assert model.creator == ["Laitinen, Antti", "Virtanen, Maija"]
    assert model.year == "2024"
    assert model.publisher == ["Suomen yliopistojen kirjatoimisto"]
    assert model.doi == "10.1234/test.5678"
    assert model.e_isbn == ["978-952-7159-75-1"]
    assert model.p_isbn == ["978-952-7159-74-4"]
    assert model.e_issn == "2345-6789"
    assert model.p_issn == "1234-5678"
    assert model.type_coar == "research report"


def test_bibra_to_raw_record_and_normalization() -> None:
    payload = {
        "language": "fi",
        "title": "Kustannus- ja jakeluketjun vaiheet",
        "alt_title": "Stages of Publishing",
        "creator": ["Laitinen, Antti", "Mika Waltari"],
        "year": "c2024",
        "publisher": ["Suomen yliopistojen kirjatoimisto"],
        "doi": "10.1234/test.5678",
        "e-isbn": ["978-952-7159-75-1"],
        "type_coar": "research report",
    }
    raw = bibra_to_raw_record(payload)
    assert raw["source_id"] == "10.1234/test.5678"
    assert raw["title"] == "Kustannus- ja jakeluketjun vaiheet"
    assert raw["variant_titles"] == ["Stages of Publishing"]
    assert raw["publication_year"] == "c2024"
    assert raw["publisher"] == "Suomen yliopistojen kirjatoimisto"
    assert len(raw["agents"]) == 2
    assert "doi:10.1234/test.5678" in raw["identifiers"]["other_ids"]

    normalizer = RecordNormalizer()
    record = normalizer.normalize(raw)
    assert record.title == "Kustannus- ja jakeluketjun vaiheet"
    assert record.publication_year == 2024
    assert len(record.agents) == 2
    assert record.agents[0].name == "Laitinen, Antti"
    assert record.agents[0].surname == "Laitinen"
    assert record.agents[1].name == "Waltari, Mika"
    assert record.agents[1].surname == "Waltari"
    assert "9789527159751" in record.identifiers.isbn
    assert len(record.subjects.genre_form) == 1
    assert record.subjects.genre_form[0].pref_label == "research report"


def test_iter_bibra_records_jsonl(tmp_path: Path) -> None:
    file_path = tmp_path / "records.jsonl"
    lines = [
        json.dumps({"title": "Doc 1", "creator": ["Author One"], "year": "2021"}),
        json.dumps({"title": "Doc 2", "creator": ["Author Two"], "year": "2022"}),
        json.dumps({"title": "Doc 3", "creator": ["Author Three"], "year": "2023"}),
    ]
    file_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    records = list(iter_bibra_records(file_path))
    assert len(records) == 3
    assert records[0][0]["title"] == "Doc 1"
    assert records[1][0]["title"] == "Doc 2"
    assert records[2][0]["title"] == "Doc 3"
    # Offsets should increase monotonically
    assert records[0][1] > 0
    assert records[1][1] > records[0][1]
    assert records[2][1] > records[1][1]


def test_iter_bibra_records_single_json_and_array(tmp_path: Path) -> None:
    # Single object
    single_path = tmp_path / "single.json"
    single_path.write_text(
        json.dumps({"title": "Single Doc", "creator": ["Author"], "year": "2020"}),
        encoding="utf-8",
    )
    records = list(iter_bibra_records(single_path))
    assert len(records) == 1
    assert records[0][0]["title"] == "Single Doc"

    # JSON Array
    array_path = tmp_path / "array.json"
    array_path.write_text(
        json.dumps(
            [
                {"title": "Doc A", "creator": ["Author A"], "year": "2019"},
                {"title": "Doc B", "creator": ["Author B"], "year": "2020"},
            ]
        ),
        encoding="utf-8",
    )
    records_arr = list(iter_bibra_records(array_path))
    assert len(records_arr) == 2
    assert records_arr[0][0]["title"] == "Doc A"
    assert records_arr[1][0]["title"] == "Doc B"


def test_bibra_to_marcxml_synthesis() -> None:
    rec = BibliographicRecord(
        source_id="10.1234/test.5678",
        source="target",
        title="Sinuhe egyptiläinen",
        publication_year=1945,
        publisher="WSOY",
        language="fi",
        agents=[Agent(name="Waltari, Mika", surname="Waltari", role="aut")],
    )
    res = ReconciliationResult(
        target_record=rec,
        selected_agent_uris={
            0: [
                "https://urn.fi/URN:NBN:fi:au:pn:000123456",
                "http://www.wikidata.org/entity/Q273115",
            ]
        },
    )
    bibra_data = {
        "title": "Sinuhe egyptiläinen",
        "creator": ["Waltari, Mika"],
        "year": "1945",
        "publisher": ["WSOY"],
        "doi": "10.1234/test.5678",
        "e-isbn": ["978-951-0-00001-0"],
    }

    elem = bibra_to_marcxml_elem(bibra_data, res, target_record=rec)
    assert elem.tag == "{http://www.loc.gov/MARC21/slim}record"

    # Controlfield 001
    c001 = elem.find("{http://www.loc.gov/MARC21/slim}controlfield[@tag='001']")
    assert c001 is not None and c001.text == "10.1234/test.5678"

    # Field 100 with injected $0 and $1
    f100 = elem.findall("{http://www.loc.gov/MARC21/slim}datafield[@tag='100']")[0]
    u0 = _get_subfields(f100, "0")
    u1 = _get_subfields(f100, "1")
    assert "https://urn.fi/URN:NBN:fi:au:pn:000123456" in u0
    assert "http://www.wikidata.org/entity/Q273115" in u1

    # Field 245
    f245 = elem.findall("{http://www.loc.gov/MARC21/slim}datafield[@tag='245']")[0]
    titles = _get_subfields(f245, "a")
    assert "Sinuhe egyptiläinen" in titles

    # Field 024 (DOI)
    f024 = elem.findall("{http://www.loc.gov/MARC21/slim}datafield[@tag='024']")[0]
    assert "10.1234/test.5678" in _get_subfields(f024, "a")
    assert "doi" in _get_subfields(f024, "2")


def test_enrich_bibra_json() -> None:
    rec = BibliographicRecord(
        source_id="BIBRA-001",
        source="target",
        title="Sinuhe",
        agents=[Agent(name="Waltari, Mika", surname="Waltari", role="aut")],
    )
    res = ReconciliationResult(
        target_record=rec,
        selected_agent_uris={
            0: [
                "https://urn.fi/URN:NBN:fi:au:pn:000123456",
                "http://www.wikidata.org/entity/Q273115",
            ]
        },
    )
    bibra_data = {
        "title": "Sinuhe",
        "creator": ["Waltari, Mika"],
        "year": "1945",
    }
    enriched = enrich_bibra_json(bibra_data, res, target_record=rec)
    assert enriched["title"] == "Sinuhe"
    assert "reconciliation" in enriched
    recon = enriched["reconciliation"]
    assert len(recon["agents"]) == 1
    assert recon["agents"][0]["name"] == "Waltari, Mika"
    assert recon["agents"][0]["kanto_uri"] == "https://urn.fi/URN:NBN:fi:au:pn:000123456"
    assert recon["agents"][0]["wikidata_uri"] == "http://www.wikidata.org/entity/Q273115"


def test_offline_reconciliation_bibra_e2e(tmp_path: Path) -> None:
    """End-to-end reconciliation of a BIBRA JSONL file generating both MARCXML and TSV reports."""
    config_file = tmp_path / "test_config.yaml"
    reports_dir = tmp_path / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)

    sample_finto = Path(__file__).parent / "fixtures" / "sample_finto.ttl"
    sample_melinda = Path(__file__).parent / "fixtures" / "sample_melinda.xml"
    bibra_input = tmp_path / "bibra_input.jsonl"
    enriched_xml = tmp_path / "enriched_from_bibra.xml"
    enriched_json = tmp_path / "enriched_from_bibra.jsonl"

    # Write realistic BIBRA JSONL records matching test fixture Melinda records
    bibra_records = [
        {
            "language": "fi",
            "title": "Sinuhe egyptiläinen",
            "creator": ["Waltari, Mika"],
            "year": "1945",
            "publisher": ["WSOY"],
            "e-isbn": ["978-951-0-00001-0"],
            "type_coar": "book",
        },
        {
            "language": "fi",
            "title": "Tuntematon sotilas",
            "creator": ["Linna, Väinö"],
            "year": "1954",
            "publisher": ["WSOY"],
            "e-isbn": ["978-951-0-00002-7"],
            "type_coar": "book",
        },
    ]
    with open(bibra_input, "w", encoding="utf-8") as f:
        for r in bibra_records:
            f.write(json.dumps(r) + "\n")

    config_yaml = f"""
storage:
  authority_db: "{tmp_path}/authority.db"
  vocab_db: "{tmp_path}/vocab.db"
  progress_db: "{tmp_path}/progress.db"
  index_path: "{tmp_path}/authority.faiss"

embedding:
  model_name: "TurkuNLP/sbert-cased-finnish-paraphrase"
  template_path: "{Path(__file__).parent.parent}/src/reconciliator/templates/default_serialization.j2"
  device: "cpu"
  batch_size: 16
  force_template: false

ingest_vocab:
  input_path: "{sample_finto}"
  vocabularies: ["yso", "kauno", "muso", "slm", "seko"]
  legacy_thesauri: ["ysa", "allars", "kaunokki", "musa"]

ingest_oai:
  melinda:
    enabled: true
    source_type: "file"
    input_path: "{sample_melinda}"
    workers: 1
    batch_size: 10

reconcile:
  target_file: "{bibra_input}"
  target_format: "bibra"
  output_file: "{enriched_xml}"
  output_format: "marcxml"
  enrich: true
  fold_legacy: true
  threshold: 0.85
  agent_reports:
    detailed_file: "{reports_dir}/agent_uris.tsv"
    summary_file: "{reports_dir}/agent_summary.tsv"
  subject_reports:
    detailed_file: "{reports_dir}/subject_uris.tsv"
    summary_file: "{reports_dir}/subject_summary.tsv"
  sparse_rerank:
    enabled: true
    dense_weight: 0.6
    sparse_weight: 0.4
    top_k: 10
    short_circuit:
      enabled: true
      threshold: 0.98
    require_volume_match: true
    require_surname_match: true
    max_year_difference: 5
    year_penalty_threshold: 1
  link_policy: "priority_fallback"
  vocabularies: ["KANTO", "YSO", "MUSO", "KAUNO", "SLM", "SEKO", "VIAF", "ISNI", "WIKIDATA"]
  agent_priority: ["KANTO", "ISNI", "VIAF", "WIKIDATA"]
  subject_priority: ["YSO", "KAUNO", "MUSO", "SLM", "SEKO"]

telemetry:
  heartbeat_interval_seconds: 0.1
  watch_interval_seconds: 0.1
"""
    config_file.write_text(config_yaml, encoding="utf-8")

    # Mock SentenceTransformer
    mock_model = MagicMock()
    mock_model.get_embedding_dimension.return_value = 128
    mock_model.get_sentence_embedding_dimension.return_value = 128

    def mock_encode(texts: list[str], **kwargs: Any) -> np.ndarray[Any, Any]:
        res = np.zeros((len(texts), 128), dtype=np.float32)
        for i, t in enumerate(texts):
            val = hash(t) % 100 / 100.0
            res[i, 0] = val
            res[i, 1] = 1.0 - val
        return res

    mock_model.encode.side_effect = mock_encode

    with patch("reconciliator.embedder.SentenceTransformer", return_value=mock_model):
        reset_cmd(config=config_file, yes=True)
        ingest_vocab_cmd(config=config_file)
        ingest_oai_cmd(config=config_file)

        # 1. Run reconciliation producing MARCXML
        reconcile_cmd(config=config_file)

    assert enriched_xml.exists()
    tree = etree.parse(str(enriched_xml))
    root = tree.getroot()
    records = root.findall(".//{http://www.loc.gov/MARC21/slim}record") or root.findall(".//record")
    assert len(records) == 2

    # Check that Waltari got Asteri KANTO URN injected into synthesized MARCXML
    rec1 = records[0]
    f100 = rec1.findall(".//{http://www.loc.gov/MARC21/slim}datafield[@tag='100']") or rec1.findall(
        ".//datafield[@tag='100']"
    )
    assert len(f100) > 0
    u0_list = _get_subfields(f100[0], "0")
    assert "https://urn.fi/URN:NBN:fi:au:pn:000123456" in u0_list

    # Check TSV reports exist
    agent_detailed = reports_dir / "agent_uris.tsv"
    assert agent_detailed.exists()
    content = agent_detailed.read_text(encoding="utf-8")
    assert "https://urn.fi/URN:NBN:fi:au:pn:000123456" in content

    # 2. Now test JSON output format
    config_json_out = config_yaml.replace(
        f'output_file: "{enriched_xml}"', f'output_file: "{enriched_json}"'
    ).replace('output_format: "marcxml"', 'output_format: "bibra"')
    config_file.write_text(config_json_out, encoding="utf-8")

    with patch("reconciliator.embedder.SentenceTransformer", return_value=mock_model):
        reconcile_cmd(config=config_file)

    assert enriched_json.exists()
    json_lines = [
        json.loads(line)
        for line in enriched_json.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(json_lines) == 2
    assert "reconciliation" in json_lines[0]
    assert json_lines[0]["reconciliation"]["status"] == "matched"
    assert (
        json_lines[0]["reconciliation"]["agents"][0]["kanto_uri"]
        == "https://urn.fi/URN:NBN:fi:au:pn:000123456"
    )
