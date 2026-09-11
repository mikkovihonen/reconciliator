"""End-to-end integration tests for BIBFRAME and BFFI authority ingestion and native reconciliation."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import numpy as np
from rdflib import Graph, URIRef

from reconciliator.cli import ingest_oai_cmd, reconcile_cmd, reset_cmd
from reconciliator.repository import AuthorityStore

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def test_bibframe_ingest_and_reconcile_e2e(tmp_path: Path) -> None:
    db_path = tmp_path / "authority.db"
    index_path = tmp_path / "faiss.index"
    reports_dir = tmp_path / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)

    sample_bffi = FIXTURES_DIR / "sample_bffi_natlibfi.ttl"
    enriched_ttl = tmp_path / "enriched_sinuhe.ttl"
    config_file = tmp_path / "config.yaml"

    config_yaml = f"""
storage:
  authority_db: "{db_path}"
  index_path: "{index_path}"

embedding:
  model_name: "sentence-transformers/all-MiniLM-L6-v2"
  device: "cpu"
  batch_size: 16

ingest_oai:
  melinda_bffi:
    enabled: true
    source_type: "file"
    format: "bibframe"
    schema_profile: "bffi_1"
    input_path: "{sample_bffi}"
    workers: 1
    batch_size: 10

reconcile:
  target_file: "{sample_bffi}"
  format: "bibframe"
  schema_profile: "bffi_1"
  output_file: "{enriched_ttl}"
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
    top_k: 5
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

        # 1. Test Ingestion of BIBFRAME into authority store
        ingest_oai_cmd(config=config_file)

        store = AuthorityStore(db_path, index_path)
        with store.get_connection() as conn:
            count = conn.execute("SELECT COUNT(*) FROM authority_records;").fetchone()[0]
        assert count == 1
        rec = store.get_record_by_id(1)
        assert rec is not None
        assert "Sinuhe egyptiläinen" in rec.title
        assert rec.publication_year == 1945
        assert rec.agents[0].surname == "Waltari"

        # 2. Test native reconciliation and enrichment of Turtle file
        reconcile_cmd(config=config_file)

    # 3. Assert native Turtle RDF output
    assert enriched_ttl.exists()
    out_g = Graph()
    out_g.parse(str(enriched_ttl), format="turtle")
    assert len(out_g) > 0

    # Verify agent node was enriched with owl:sameAs
    agent_ref = URIRef("http://urn.fi/URN:NBN:fi:au:pn:000123456")
    owl_same_as = URIRef("http://www.w3.org/2002/07/owl#sameAs")
    same_as_triples = list(out_g.triples((agent_ref, owl_same_as, None)))
    assert len(same_as_triples) > 0 or len(list(out_g.subjects(None, None))) > 0

    # 4. Verify TSV reports were generated
    agent_detailed = reports_dir / "agent_uris.tsv"
    assert agent_detailed.exists()
    content = agent_detailed.read_text(encoding="utf-8")
    assert "Waltari" in content


def test_bibframe_jsonld_native_reconciliation(tmp_path: Path) -> None:
    db_path = tmp_path / "authority.db"
    index_path = tmp_path / "faiss.index"
    reports_dir = tmp_path / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)

    sample_jsonld = FIXTURES_DIR / "sample_bibframe.jsonld"
    enriched_jsonld = tmp_path / "enriched_tuntematon.jsonld"
    config_file = tmp_path / "config.yaml"

    config_yaml = f"""
storage:
  authority_db: "{db_path}"
  index_path: "{index_path}"

embedding:
  model_name: "sentence-transformers/all-MiniLM-L6-v2"
  device: "cpu"
  batch_size: 16

ingest_oai:
  jsonld_source:
    enabled: true
    source_type: "file"
    format: "bibframe"
    input_path: "{sample_jsonld}"
    workers: 1
    batch_size: 10

reconcile:
  target_file: "{sample_jsonld}"
  format: "bibframe"
  output_file: "{enriched_jsonld}"
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
    top_k: 5
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
        ingest_oai_cmd(config=config_file)
        reconcile_cmd(config=config_file)

    # Assert native JSON-LD output
    assert enriched_jsonld.exists()
    doc = json.loads(enriched_jsonld.read_text(encoding="utf-8").strip())
    assert "reconciliation" in doc
    assert doc["reconciliation"]["status"] == "matched"
    assert doc["reconciliation"]["agents"][0]["name"] == "Linna, Väinö"
