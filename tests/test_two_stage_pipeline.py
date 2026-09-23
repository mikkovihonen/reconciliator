"""Tests for TwoStageEntityResolver pipeline and pre-veto integration."""

from __future__ import annotations

from unittest.mock import MagicMock

import numpy as np
import pytest

from reconciliator.config import LLMJudgeConfig, RerankConfig
from reconciliator.judge.pipeline import TwoStageEntityResolver
from reconciliator.judge.protocol import CandidateEvaluation, TargetEncoding
from reconciliator.models import Agent, BibliographicRecord
from reconciliator.rerank.evaluator import CandidateEvaluator


@pytest.fixture
def mock_backend() -> MagicMock:
    backend = MagicMock()
    backend.dimension = 64
    backend.config = LLMJudgeConfig(
        positive_token="YES",
        negative_token="NO",
        candidate_suffix_template="Candidate: {text}\nMatch: ",
    )
    backend.encode_target.return_value = TargetEncoding(
        dense_vector=np.ones(64, dtype=np.float32) / np.sqrt(64),
        kv_cache="mock_kv_cache",
        base_seq_len=12,
    )
    backend.evaluate_candidate.return_value = CandidateEvaluation(
        is_match=True,
        confidence_score=0.95,
        raw_token="YES",
    )
    return backend


def test_two_stage_pipeline_pre_veto_volume_rejection(mock_backend: MagicMock) -> None:
    """Conflicting volumes should be rejected by pre-veto without calling LLM backend."""
    config = RerankConfig(
        engine="llm_judge",
        require_volume_match=True,
        apply_pre_veto=True,
    )
    evaluator = CandidateEvaluator(config)
    resolver = TwoStageEntityResolver(
        backend=mock_backend,
        config=config,
        evaluator=evaluator,
        apply_pre_veto=True,
    )

    target = BibliographicRecord(
        source_id="T1",
        source="melinda",
        title="Sinuhe egyptiläinen",
        volume_designation="osa 1",
        publication_year=1945,
    )
    candidate = BibliographicRecord(
        source_id="C1",
        source="fennica",
        title="Sinuhe egyptiläinen",
        volume_designation="osa 2",
        publication_year=1945,
    )

    verdict = resolver.evaluate_candidate(
        target_record=target,
        candidate_record=candidate,
        base_cache="cache",
        base_seq_len=10,
        dense_score=0.88,
    )

    assert verdict.accepted is False
    assert verdict.volume_match is False
    assert verdict.match_tier == "tier2_preveto"
    # Ensure backend evaluate_candidate was NEVER called!
    mock_backend.evaluate_candidate.assert_not_called()


def test_two_stage_pipeline_pre_veto_year_rejection(mock_backend: MagicMock) -> None:
    """Discrepant publication years beyond threshold should be rejected by pre-veto."""
    config = RerankConfig(
        engine="llm_judge",
        max_year_difference=5,
        apply_pre_veto=True,
    )
    evaluator = CandidateEvaluator(config)
    resolver = TwoStageEntityResolver(
        backend=mock_backend,
        config=config,
        evaluator=evaluator,
        apply_pre_veto=True,
    )

    target = BibliographicRecord(
        source_id="T1",
        source="melinda",
        title="Kalevala",
        publication_year=1835,
    )
    candidate = BibliographicRecord(
        source_id="C1",
        source="fennica",
        title="Kalevala",
        publication_year=1999,
    )

    verdict = resolver.evaluate_candidate(
        target_record=target,
        candidate_record=candidate,
        base_cache="cache",
        base_seq_len=10,
        dense_score=0.88,
    )

    assert verdict.accepted is False
    assert verdict.year_diff == 164
    assert verdict.match_tier == "tier2_preveto"
    mock_backend.evaluate_candidate.assert_not_called()


def test_two_stage_pipeline_successful_match(mock_backend: MagicMock) -> None:
    """Valid candidate passing pre-veto invokes LLM backend and returns accepted match."""
    config = RerankConfig(
        engine="llm_judge",
        dense_weight=0.5,
        rerank_weight=0.5,
        apply_pre_veto=True,
    )
    evaluator = CandidateEvaluator(config)
    resolver = TwoStageEntityResolver(
        backend=mock_backend,
        config=config,
        evaluator=evaluator,
        apply_pre_veto=True,
    )

    target = BibliographicRecord(
        source_id="T1",
        source="melinda",
        title="Sinuhe egyptiläinen",
        publication_year=1945,
        agents=[Agent(name="Waltari, Mika", surname="Waltari")],
    )
    candidate = BibliographicRecord(
        source_id="C1",
        source="fennica",
        title="Sinuhe egyptiläinen",
        publication_year=1945,
        agents=[Agent(name="Waltari, Mika", surname="Waltari")],
    )

    verdict = resolver.evaluate_candidate(
        target_record=target,
        candidate_record=candidate,
        base_cache="mock_cache",
        base_seq_len=10,
        dense_score=0.90,
    )

    assert verdict.accepted is True
    assert verdict.match_tier == "tier2_llm_judge"
    assert verdict.volume_match is True
    assert verdict.surname_match is True
    # Fused score: 0.5 * 0.90 + 0.5 * 0.95 = 0.925
    assert verdict.match_score == 0.925
    mock_backend.evaluate_candidate.assert_called_once()

