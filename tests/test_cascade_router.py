"""Unit tests for the Cascading Dual-Route Reranker and LLM Judge router."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from reconciliator.config import (
    CascadeRouterConfig,
    LLMJudgeConfig,
    RerankConfig,
)
from reconciliator.models import Agent, BibliographicRecord
from reconciliator.rerank.cascade import CascadeScorer
from reconciliator.rerank.evaluator import CandidateEvaluator
from reconciliator.rerank.factory import create_rerank_scorer


def make_record(
    source_id: str,
    title: str,
    surname: str = "Waltari",
    year: int = 1945,
    volume: str | None = None,
    publisher: str = "WSOY",
) -> BibliographicRecord:
    return BibliographicRecord(
        source_id=source_id,
        source="melinda",
        title=title,
        publication_year=year,
        publisher=publisher,
        volume_designation=volume,
        agents=[Agent(name=f"{surname}, Mika", surname=surname)],
    )


def test_cascade_router_config_defaults() -> None:
    """Verify default parameters of CascadeRouterConfig."""
    cfg = CascadeRouterConfig()
    assert cfg.heuristic_engine == "bm25"
    assert cfg.high_confidence_threshold == 0.90
    assert cfg.low_confidence_threshold == 0.55
    assert cfg.ambiguity_margin == 0.08
    assert cfg.uncertainty_band_min == 0.60
    assert cfg.uncertainty_band_max == 0.88
    assert cfg.max_escalated_candidates == 5


def test_cascade_router_config_validation() -> None:
    """Verify validation boundaries on CascadeRouterConfig."""
    with pytest.raises(ValidationError):
        CascadeRouterConfig(high_confidence_threshold=1.5)  # le=1.0

    with pytest.raises(ValidationError):
        CascadeRouterConfig(low_confidence_threshold=-0.1)  # ge=0.0

    with pytest.raises(ValidationError):
        CascadeRouterConfig(max_escalated_candidates=0)  # ge=1

    with pytest.raises(ValidationError):
        CascadeRouterConfig.model_validate({"unknown_field": "invalid"})  # extra="forbid"


def test_factory_instantiates_cascade_scorer() -> None:
    """Verify create_rerank_scorer creates a CascadeScorer when engine='cascade'."""
    mock_backend = MagicMock()
    config = RerankConfig(
        engine="cascade",
        cascade=CascadeRouterConfig(heuristic_engine="bm25"),
        judge=LLMJudgeConfig(),
    )
    scorer = create_rerank_scorer(config, judge_backend=mock_backend)
    assert isinstance(scorer, CascadeScorer)
    assert scorer.heuristic_engine == "bm25"


def test_cascade_scorer_single_fast_rejection() -> None:
    """Low score below low_confidence_threshold rejected without invoking judge."""
    mock_heuristic = MagicMock()
    mock_heuristic.score.return_value = 0.30
    mock_judge = MagicMock()

    config = CascadeRouterConfig(low_confidence_threshold=0.55)
    scorer = CascadeScorer(config=config, heuristic_scorer=mock_heuristic, judge_scorer=mock_judge)

    score = scorer.score("target", "candidate")
    assert score == 0.30
    mock_heuristic.score.assert_called_once_with("target", "candidate")
    mock_judge.score.assert_not_called()


def test_cascade_scorer_single_fast_acceptance() -> None:
    """High score above high_confidence_threshold accepted without invoking judge."""
    mock_heuristic = MagicMock()
    mock_heuristic.score.return_value = 0.95
    mock_judge = MagicMock()

    config = CascadeRouterConfig(high_confidence_threshold=0.90)
    scorer = CascadeScorer(config=config, heuristic_scorer=mock_heuristic, judge_scorer=mock_judge)

    score = scorer.score("target", "candidate")
    assert score == 0.95
    mock_heuristic.score.assert_called_once_with("target", "candidate")
    mock_judge.score.assert_not_called()


def test_cascade_scorer_single_uncertainty_escalation() -> None:
    """Score in uncertainty band triggers LLM judge evaluation."""
    mock_heuristic = MagicMock()
    mock_heuristic.score.return_value = 0.75
    mock_judge = MagicMock()
    mock_judge.score.return_value = 0.88

    config = CascadeRouterConfig(uncertainty_band_min=0.60, uncertainty_band_max=0.88)
    scorer = CascadeScorer(config=config, heuristic_scorer=mock_heuristic, judge_scorer=mock_judge)

    score = scorer.score("target", "candidate", past_key_values="mock_kv")
    assert score == 0.88
    mock_heuristic.score.assert_called_once_with("target", "candidate")
    mock_judge.score.assert_called_once_with("target", "candidate", past_key_values="mock_kv")


def test_cascade_scorer_batch_fast_rejection() -> None:
    """All candidates have low scores -> fast rejection, 0 judge calls."""
    mock_heuristic = MagicMock()
    mock_heuristic.score_batch.return_value = [0.20, 0.35, 0.15]
    mock_judge = MagicMock()

    config = CascadeRouterConfig(low_confidence_threshold=0.55)
    scorer = CascadeScorer(config=config, heuristic_scorer=mock_heuristic, judge_scorer=mock_judge)

    scores = scorer.score_batch("target", ["c1", "c2", "c3"])
    assert scores == [0.20, 0.35, 0.15]
    mock_heuristic.score_batch.assert_called_once_with("target", ["c1", "c2", "c3"])
    mock_judge.score_batch.assert_not_called()


def test_cascade_scorer_batch_high_confidence_wide_margin() -> None:
    """Top candidate has high score and wide margin -> fast acceptance, 0 judge calls."""
    mock_heuristic = MagicMock()
    mock_heuristic.score_batch.return_value = [0.95, 0.70, 0.40]
    mock_judge = MagicMock()

    config = CascadeRouterConfig(
        high_confidence_threshold=0.90,
        ambiguity_margin=0.08,
    )
    scorer = CascadeScorer(config=config, heuristic_scorer=mock_heuristic, judge_scorer=mock_judge)

    scores = scorer.score_batch("target", ["c1", "c2", "c3"])
    assert scores == [0.95, 0.70, 0.40]
    mock_heuristic.score_batch.assert_called_once_with("target", ["c1", "c2", "c3"])
    mock_judge.score_batch.assert_not_called()


def test_cascade_scorer_batch_close_competition_escalation() -> None:
    """Top candidate high score but second candidate is within ambiguity margin -> escalate both."""
    mock_heuristic = MagicMock()
    # c1 = 0.94, c2 = 0.91 (margin = 0.03 < 0.08), c3 = 0.40
    mock_heuristic.score_batch.return_value = [0.94, 0.91, 0.40]
    mock_judge = MagicMock()
    # Judge resolves the dispute: c1 is confirmed (0.98), c2 is rejected (0.25)
    mock_judge.score_batch.return_value = [0.98, 0.25]

    config = CascadeRouterConfig(
        high_confidence_threshold=0.90,
        ambiguity_margin=0.08,
        max_escalated_candidates=5,
    )
    scorer = CascadeScorer(config=config, heuristic_scorer=mock_heuristic, judge_scorer=mock_judge)

    scores = scorer.score_batch("target", ["c1", "c2", "c3"], past_key_values="mock_kv")
    # c1 and c2 received judge scores, c3 retained heuristic score
    assert scores == [0.98, 0.25, 0.40]
    mock_judge.score_batch.assert_called_once_with(
        "target", ["c1", "c2"], past_key_values="mock_kv"
    )


def test_cascade_scorer_batch_uncertainty_band_capping() -> None:
    """Scores in uncertainty band are escalated, but capped at max_escalated_candidates."""
    mock_heuristic = MagicMock()
    # 5 candidates in uncertainty band [0.60, 0.88]
    mock_heuristic.score_batch.return_value = [0.85, 0.80, 0.75, 0.70, 0.65]
    mock_judge = MagicMock()
    # Suppose max_escalated_candidates is 2 -> only the top 2 (indices 0 and 1) should be escalated
    mock_judge.score_batch.return_value = [0.92, 0.88]

    config = CascadeRouterConfig(
        uncertainty_band_min=0.60,
        uncertainty_band_max=0.88,
        max_escalated_candidates=2,
    )
    scorer = CascadeScorer(config=config, heuristic_scorer=mock_heuristic, judge_scorer=mock_judge)

    scores = scorer.score_batch("target", ["c1", "c2", "c3", "c4", "c5"])
    assert scores[0] == 0.92
    assert scores[1] == 0.88
    # Remaining candidates keep their heuristic scores
    assert scores[2] == 0.75
    assert scores[3] == 0.70
    assert scores[4] == 0.65
    mock_judge.score_batch.assert_called_once_with("target", ["c1", "c2"])


def test_candidate_evaluator_cascade_batch_tier_attribution() -> None:
    """Verify CandidateEvaluator.evaluate_candidates_batch assigns correct match_tier provenance."""
    mock_backend = MagicMock()
    mock_backend.score_batch.return_value = [0.96]  # Judge accepts escalated candidate

    config = RerankConfig(
        engine="cascade",
        cascade=CascadeRouterConfig(
            heuristic_engine="bm25",
            high_confidence_threshold=0.90,
            ambiguity_margin=0.08,
            uncertainty_band_min=0.60,
            uncertainty_band_max=0.88,
        ),
    )
    evaluator = CandidateEvaluator(config, judge_backend=mock_backend)

    target = make_record("T1", "Sinuhe egyptiläinen", surname="Waltari", year=1945)
    # Cand 1: Pre-veto surname mismatch
    c_veto = make_record("C1", "Sinuhe egyptiläinen", surname="Virtanen", year=1945)
    # Cand 2: Close title match that falls in uncertainty band
    c_uncertain = make_record("C2", "Sinuhe Mika Waltari", surname="Waltari", year=1945)

    evals = evaluator.evaluate_candidates_batch(
        target,
        [(c_veto, 0.85), (c_uncertain, 0.82)],
        acceptance_threshold=0.65,
    )

    assert len(evals) == 2

    # Cand 1 was vetoed
    assert not evals[0].accepted
    assert evals[0].match_tier == "tier2_preveto"

    # Cand 2 was evaluated by cascade router
    assert evals[1].accepted
    # Escalated to judge -> tier2_cascade_judge
    assert evals[1].match_tier in ("tier2_cascade_judge", "tier3_cascade_heuristic")
