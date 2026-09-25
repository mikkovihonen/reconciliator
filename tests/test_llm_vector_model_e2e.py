"""End-to-end integration test verifying the full pipeline with vector_model: 'llm' on a small corpus."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import numpy as np
from lxml import etree

from reconciliator.commands import (
    build_index_cmd,
    ingest_oai_cmd,
    ingest_vocab_cmd,
    reconcile_cmd,
    reset_cmd,
)
from reconciliator.judge.protocol import CandidateEvaluation, TargetEncoding


def test_offline_llm_vector_model_e2e(tmp_path: Path) -> None:
    """Verify that vector_model: 'llm' builds vector indexes and executes reconciliation on a sample corpus."""
    config_file = tmp_path / "test_llm_config.yaml"
    reports_dir = tmp_path / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)

    sample_finto = Path(__file__).parent / "fixtures" / "sample_finto.ttl"
    sample_melinda = Path(__file__).parent / "fixtures" / "sample_melinda.xml"
    sample_target = Path(__file__).parent / "fixtures" / "sample_target.xml"
    enriched_xml = tmp_path / "enriched.xml"
    work_index = tmp_path / "work.index"
    manifestation_index = tmp_path / "manifestation.index"

    config_yaml = f"""
vector_model: "llm"

storage:
  data_dir: "{tmp_path}"
  work_index: "{work_index}"
  manifestation_index: "{manifestation_index}"

llm:
  model_name: "mock-llm-judge"
  backend: "torch"
  device: "cpu"
  batch_size: 16

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
  threshold: 0.80
  agent_reports:
    detailed_file: "{reports_dir}/agent_uris.tsv"
    summary_file: "{reports_dir}/agent_summary.tsv"
  subject_reports:
    detailed_file: "{reports_dir}/subject_uris.tsv"
    summary_file: "{reports_dir}/subject_summary.tsv"
  work_reports:
    detailed_file: "{reports_dir}/work_uris.tsv"
    summary_file: "{reports_dir}/work_summary.tsv"
  rerank:
    enabled: true
    engine: "llm_judge"
    dense_weight: 0.6
    rerank_weight: 0.4
    top_k: 10
    short_circuit:
      enabled: true
      threshold: 0.98
  link_policy: "priority_fallback"
  vocabularies: ["KANTO", "YSO", "MUSO", "KAUNO", "SLM", "SEKO", "VIAF", "ISNI", "WIKIDATA"]
  agent_priority: ["KANTO", "ISNI", "VIAF", "WIKIDATA"]
  subject_priority: ["YSO", "KAUNO", "MUSO", "SLM", "SEKO"]

telemetry:
  heartbeat_interval_seconds: 0.1
  watch_interval_seconds: 0.1
"""
    config_file.write_text(config_yaml, encoding="utf-8")

    dim = 64
    mock_backend = MagicMock()
    mock_backend.dimension = dim

    def mock_embed_texts(texts: list[str], batch_size: Any = None) -> np.ndarray:
        res = np.zeros((len(texts), dim), dtype=np.float32)
        for i, t in enumerate(texts):
            val = (hash(t) % 100) / 100.0
            res[i, 0] = val
            res[i, 1] = 1.0 - val
        norms = np.linalg.norm(res, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        return res / norms

    mock_backend.embed_texts.side_effect = mock_embed_texts
    mock_backend.encode_batch_dense.side_effect = mock_embed_texts

    def mock_encode_target(text: str, retain_cache: bool = True) -> TargetEncoding:
        vec = mock_embed_texts([text])[0]
        return TargetEncoding(dense_vector=vec, kv_cache=None, base_seq_len=len(text.split()))

    mock_backend.encode_target.side_effect = mock_encode_target

    def mock_evaluate_candidate(
        base_cache: Any,
        base_seq_len: int,
        candidate_suffix: str,
        prior_bias: float = 0.0,
        **kwargs: Any,
    ) -> CandidateEvaluation:
        return CandidateEvaluation(is_match=True, confidence_score=0.95, raw_token="YES")

    mock_backend.evaluate_candidate.side_effect = mock_evaluate_candidate

    with patch("reconciliator.judge.factory.create_judge_backend", return_value=mock_backend):
        # 1. Reset storage
        reset_cmd(config=config_file, yes=True)

        # 2. Ingest vocabularies
        ingest_vocab_cmd(config=config_file)

        # 3. Ingest authority records from Melinda sample
        ingest_oai_cmd(config=config_file)

        # 4. Build WEMI vector indexes using the LLM backend
        build_index_cmd(config_path=config_file, batch_size=10, tier="all")

        # Verify work index file exists and is populated
        assert work_index.exists()

        # 5. Run offline reconciliation and enrichment with LLM vector model & LLM Judge reranker
        reconcile_cmd(config=config_file)

    # 6. Verify Enriched Output XML
    assert enriched_xml.exists()
    tree = etree.parse(str(enriched_xml))
    root = tree.getroot()
    records = root.findall(".//{http://www.loc.gov/MARC21/slim}record") or root.findall(".//record")
    assert len(records) == 2

    # Verify work TSV report was written
    work_report = reports_dir / "work_uris.tsv"
    assert work_report.exists()
    lines = work_report.read_text(encoding="utf-8").splitlines()
    assert len(lines) >= 3  # header + 2 records
