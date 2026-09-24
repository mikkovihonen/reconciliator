"""End-to-end offline integration test verifying the full reconciliation and enrichment pipeline."""

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import numpy as np
from lxml import etree

from reconciliator.commands import ingest_oai_cmd, ingest_vocab_cmd, reconcile_cmd, reset_cmd
from reconciliator.loaders.marcxml import _get_subfields


def test_offline_reconciliation_e2e(tmp_path: Path) -> None:
    # Prepare test configuration pointing to temporary databases
    config_file = tmp_path / "test_config.yaml"
    reports_dir = tmp_path / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)

    sample_finto = Path(__file__).parent / "fixtures" / "sample_finto.ttl"
    sample_melinda = Path(__file__).parent / "fixtures" / "sample_melinda.xml"
    sample_target = Path(__file__).parent / "fixtures" / "sample_target.xml"
    enriched_xml = tmp_path / "enriched.xml"

    config_yaml = f"""
storage:
  data_dir: "{tmp_path}"

embedding:
  model_name: "TurkuNLP/sbert-cased-finnish-paraphrase"
  template_path: "{Path(__file__).parent.parent}/src/reconciliator/templates/default_serialization.j2"
  device: "cpu"
  batch_size: 16
  force_template: false

ingestion:
  vocab:
    input_path: "{sample_finto}"
    vocabularies: ["yso", "kauno", "muso", "slm", "seko"]
    legacy_thesauri: ["ysa", "allars", "kaunokki", "musa"]

  oai:
    melinda:
      enabled: true
      source_type: "file"
      input_path: "{sample_melinda}"
      workers: 1
      batch_size: 10

reconcile:
  target_file: "{sample_target}"
  output_file: "{enriched_xml}"
  enrich: true
  fold_legacy: true
  threshold: 0.85
  agent_reports:
    detailed_file: "{reports_dir}/agent_uris.tsv"
    summary_file: "{reports_dir}/agent_summary.tsv"
  subject_reports:
    detailed_file: "{reports_dir}/subject_uris.tsv"
    summary_file: "{reports_dir}/subject_summary.tsv"
  rerank:
    enabled: true
    engine: "bm25"
    dense_weight: 0.6
    rerank_weight: 0.4
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

    # Mock SentenceTransformer to run deterministic offline tests without downloading 400MB models
    mock_model = MagicMock()
    mock_model.get_embedding_dimension.return_value = 128
    mock_model.get_sentence_embedding_dimension.return_value = 128

    def mock_encode(texts: list[str], **kwargs: Any) -> np.ndarray[Any, Any]:
        # Generate predictable normalized vectors
        res = np.zeros((len(texts), 128), dtype=np.float32)
        for i, t in enumerate(texts):
            # Non-zero based on text hash
            val = hash(t) % 100 / 100.0
            res[i, 0] = val
            res[i, 1] = 1.0 - val
        return res

    mock_model.encode.side_effect = mock_encode

    with patch("reconciliator.embedding.embedder.SentenceTransformer", return_value=mock_model):
        # 1. Reset storage
        reset_cmd(config=config_file, yes=True)

        # 2. Ingest vocabularies
        ingest_vocab_cmd(config=config_file)

        # 3. Ingest authority records from OAI/file
        ingest_oai_cmd(config=config_file)

        # 4. Run offline reconciliation and enrichment
        reconcile_cmd(config=config_file)

    # 5. Verify Enriched Output XML
    assert enriched_xml.exists()
    tree = etree.parse(str(enriched_xml))
    root = tree.getroot()
    records = root.findall(".//{http://www.loc.gov/MARC21/slim}record") or root.findall(".//record")
    assert len(records) == 2

    # Record 1 (TARGET-001) checks:
    rec1 = records[0]
    f100 = rec1.findall(".//{http://www.loc.gov/MARC21/slim}datafield[@tag='100']") or rec1.findall(
        ".//datafield[@tag='100']"
    )
    assert len(f100) > 0
    u0_list = _get_subfields(f100[0], "0")
    # Asteri KANTO URN injected
    assert "https://urn.fi/URN:NBN:fi:au:pn:000123456" in u0_list or "kanto:000123456" in u0_list

    # Subject checks for Rec 1:
    # 650 lääkärit folded to YSO
    f650 = rec1.findall(".//{http://www.loc.gov/MARC21/slim}datafield[@tag='650']") or rec1.findall(
        ".//datafield[@tag='650']"
    )
    f650_uris = []
    for f in f650:
        f650_uris.extend(_get_subfields(f, "0"))
    assert "http://www.yso.fi/onto/yso/p2591" in f650_uris

    # 650 kaunokki 'historialliset romaanit' folded to 655 SLM
    f655 = rec1.findall(".//{http://www.loc.gov/MARC21/slim}datafield[@tag='655']") or rec1.findall(
        ".//datafield[@tag='655']"
    )
    assert len(f655) > 0
    f655_uris = _get_subfields(f655[0], "0")
    assert (
        "http://urn.fi/URN:NBN:fi:au:slm:s123" in f655_uris
        or "https://urn.fi/URN:NBN:fi:au:slm:s123" in f655_uris
    )

    # 6. Verify TSV Reports
    agent_detailed_tsv = reports_dir / "agent_uris.tsv"
    agent_summary_tsv = reports_dir / "agent_summary.tsv"
    subj_detailed_tsv = reports_dir / "subject_uris.tsv"
    subj_summary_tsv = reports_dir / "subject_summary.tsv"

    assert agent_detailed_tsv.exists()
    assert agent_summary_tsv.exists()
    assert subj_detailed_tsv.exists()
    assert subj_summary_tsv.exists()

    agent_lines = agent_detailed_tsv.read_text(encoding="utf-8").splitlines()
    assert len(agent_lines) >= 2  # Header + rows
    assert "https://urn.fi/URN:NBN:fi:au:pn:000123456" in agent_detailed_tsv.read_text(encoding="utf-8") or "kanto:000123456" in agent_detailed_tsv.read_text(encoding="utf-8")
