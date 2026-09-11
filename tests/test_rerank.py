"""Unit tests for pluggable reranking scorers, factory, and CandidateEvaluator."""

from typing import Optional
from unittest.mock import MagicMock

import pytest

from reconciliator.config import RerankConfig, ShortCircuitConfig
from reconciliator.models import Agent, BibliographicRecord
from reconciliator.rerank import (
    BM25Scorer,
    CandidateEvaluator,
    CrossEncoderScorer,
    FuzzyScorer,
    NoOpScorer,
    create_rerank_scorer,
)


def make_record(
    source_id: str,
    title: str,
    surname: str = "Waltari",
    year: int = 1945,
    volume: Optional[str] = "osa 1",
    publisher: str = "WSOY",
) -> BibliographicRecord:
    return BibliographicRecord(
        source_id=source_id,
        source="melinda",
        title=title,
        publication_year=year,
        publisher=publisher,
        volume_designation=volume,
        agents=[Agent(name=f"{surname}, Mika", surname=surname, role="aut")],
    )


def test_bm25_scorer() -> None:
    scorer = BM25Scorer()
    # Identical texts yield max normalized score (1.0)
    score_exact = scorer.score("Sinuhe egyptiläinen WSOY", "Sinuhe egyptiläinen WSOY")
    assert pytest.approx(score_exact, rel=1e-3) == 1.0

    # Overlapping texts
    score_part = scorer.score("Sinuhe egyptiläinen WSOY", "Sinuhe Mika Waltari")
    assert 0.0 < score_part < 1.0

    # Completely disjoint texts
    score_none = scorer.score("Sinuhe egyptiläinen", "Tuntematon sotilas")
    assert score_none == 0.0

    # Empty text handling
    assert scorer.score("", "Sinuhe") == 0.0
    assert scorer.score("Sinuhe", "") == 0.0

    # Batch scoring
    batch_scores = scorer.score_batch(
        "Sinuhe egyptiläinen",
        ["Sinuhe egyptiläinen", "Tuntematon sotilas", ""],
    )
    assert len(batch_scores) == 3
    assert pytest.approx(batch_scores[0], rel=1e-3) == 1.0
    assert batch_scores[1] == 0.0
    assert batch_scores[2] == 0.0


def test_fuzzy_scorer() -> None:
    scorer = FuzzyScorer()
    # Exact or subset tokens yield 1.0 with token_set_ratio
    assert scorer.score("Sinuhe egyptiläinen", "Sinuhe egyptiläinen") == 1.0
    assert scorer.score("Sinuhe egyptiläinen", "Sinuhe egyptiläinen WSOY") == 1.0

    # Typo tolerance
    score_typo = scorer.score("Sinuhe egyptilainen", "Sinuhe egyptiläinen")
    assert score_typo > 0.85

    # Completely different text
    assert scorer.score("Kalevala", "Muumipeikko") < 0.3

    # Empty string
    assert scorer.score("", "Sinuhe") == 0.0

    # Batch scoring
    batch = scorer.score_batch("Sinuhe", ["Sinuhe", "Kalevala"])
    assert batch[0] == 1.0
    assert batch[1] < 0.5


def test_noop_scorer() -> None:
    scorer = NoOpScorer()
    assert scorer.score("anything", "whatever") == 1.0
    assert scorer.score_batch("q", ["d1", "d2"]) == [1.0, 1.0]


def test_cross_encoder_scorer_mocked() -> None:
    mock_model = MagicMock()
    # Mock returning raw logit 2.5 (should be sigmoided)
    mock_model.predict.return_value = [2.5]
    scorer = CrossEncoderScorer(model=mock_model)

    score = scorer.score("query", "doc")
    assert 0.0 <= score <= 1.0
    mock_model.predict.assert_called_once_with([["query", "doc"]])

    # Batch scoring
    mock_model.predict.return_value = [0.8, 0.2]
    batch_scores = scorer.score_batch("query", ["doc1", "doc2"])
    assert batch_scores == [0.8, 0.2]


def test_create_rerank_scorer_factory() -> None:
    assert isinstance(create_rerank_scorer(RerankConfig(engine="bm25")), BM25Scorer)
    assert isinstance(create_rerank_scorer(RerankConfig(engine="fuzzy")), FuzzyScorer)
    assert isinstance(create_rerank_scorer(RerankConfig(engine="noop")), NoOpScorer)

    cfg_cross = RerankConfig(engine="cross_encoder", model_name="dummy/model", device="cpu")
    ce_scorer = create_rerank_scorer(cfg_cross)
    assert isinstance(ce_scorer, CrossEncoderScorer)
    assert ce_scorer.model_name == "dummy/model"
    assert ce_scorer.device == "cpu"

    # Invalid engine
    with pytest.raises(ValueError, match="Unknown rerank engine"):
        bad_cfg = RerankConfig.model_construct(engine="unknown_engine")  # type: ignore[arg-type]
        create_rerank_scorer(bad_cfg)


def test_candidate_evaluator_volume_discrimination() -> None:
    evaluator = CandidateEvaluator(RerankConfig(require_volume_match=True))

    target = make_record("T1", "Sinuhe", volume="osa 1")
    cand_conflict = make_record("C1", "Sinuhe", volume="osa 2")
    cand_match = make_record("C2", "Sinuhe", volume="osa 1")
    cand_none = make_record("C3", "Sinuhe", volume=None)

    assert not evaluator.check_volume_match(
        target.volume_designation, cand_conflict.volume_designation
    )
    assert evaluator.check_volume_match(target.volume_designation, cand_match.volume_designation)
    assert evaluator.check_volume_match(target.volume_designation, cand_none.volume_designation)


def test_candidate_evaluator_surname_match() -> None:
    evaluator = CandidateEvaluator(RerankConfig(require_surname_match=True))

    target = make_record("T1", "Sinuhe", surname="Waltari")
    cand_same = make_record("C1", "Sinuhe", surname="Waltari")
    cand_diff = make_record("C2", "Sinuhe", surname="Virtanen")

    assert evaluator.check_surname_match(target, cand_same)
    assert not evaluator.check_surname_match(target, cand_diff)


def test_candidate_evaluator_year_disqualification() -> None:
    evaluator = CandidateEvaluator(RerankConfig(max_year_difference=5, year_penalty_threshold=1))

    is_valid, penalty, diff = evaluator.evaluate_year(1945, 1946)
    assert is_valid and diff == 1 and penalty == 0.0

    is_valid, penalty, diff = evaluator.evaluate_year(1945, 1949)
    assert is_valid and diff == 4 and penalty > 0.0

    is_valid, _, diff = evaluator.evaluate_year(1945, 1955)
    assert not is_valid and diff == 10


def test_candidate_evaluator_short_circuit_with_volume_safeguard() -> None:
    config = RerankConfig(
        short_circuit=ShortCircuitConfig(enabled=True, threshold=0.98),
        require_volume_match=True,
    )
    evaluator = CandidateEvaluator(config)

    target = make_record("T1", "Sinuhe", volume="osa 1")
    cand_match = make_record("C1", "Sinuhe", volume="osa 1")
    cand_conflict = make_record("C2", "Sinuhe", volume="osa 2")

    # High score with matching volume -> accepted tier2_dense
    m_pass = evaluator.evaluate_candidate(target, cand_match, dense_score=0.99)
    assert m_pass.accepted
    assert m_pass.match_tier == "tier2_dense"

    # High score with conflicting volume -> disqualified!
    m_fail = evaluator.evaluate_candidate(target, cand_conflict, dense_score=0.99)
    assert not m_fail.accepted
    assert not m_fail.volume_match


def test_candidate_evaluator_disabled_rerank() -> None:
    config = RerankConfig(enabled=False)
    evaluator = CandidateEvaluator(config)

    target = make_record("T1", "Sinuhe")
    cand = make_record("C1", "Sinuhe")

    match = evaluator.evaluate_candidate(target, cand, dense_score=0.90, acceptance_threshold=0.85)
    assert match.accepted
    assert match.match_tier == "tier2_dense"
    assert match.rerank_score is None


def test_candidate_evaluator_fuzzy_engine() -> None:
    config = RerankConfig(
        engine="fuzzy",
        dense_weight=0.5,
        rerank_weight=0.5,
    )
    evaluator = CandidateEvaluator(config)

    target = make_record("T1", "Sinuhe egyptiläinen", publisher="WSOY")
    cand = make_record("C1", "Sinuhe egyptilainen", publisher="WSOY")  # minor typo in candidate

    match = evaluator.evaluate_candidate(target, cand, dense_score=0.80, acceptance_threshold=0.80)
    assert match.accepted
    assert match.match_tier == "tier3_hybrid_rerank"
    assert match.rerank_score is not None
    assert match.rerank_score > 0.8
