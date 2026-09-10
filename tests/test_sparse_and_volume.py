"""Unit tests for SparseReranker, BM25 scoring, volume discrimination, and short-circuiting."""

from typing import Optional

from reconciliator.config import ShortCircuitConfig, SparseRerankConfig
from reconciliator.models import Agent, BibliographicRecord
from reconciliator.sparse import SparseReranker


def make_record(
    source_id: str,
    title: str,
    surname: str = "Waltari",
    year: int = 1945,
    volume: Optional[str] = "osa 1",
) -> BibliographicRecord:
    return BibliographicRecord(
        source_id=source_id,
        source="melinda",
        title=title,
        publication_year=year,
        publisher="WSOY",
        volume_designation=volume,
        agents=[Agent(name=f"{surname}, Mika", surname=surname, role="aut")],
    )


def test_volume_discrimination() -> None:
    reranker = SparseReranker(SparseRerankConfig(require_volume_match=True))

    target = make_record("T1", "Sinuhe", volume="osa 1")
    cand_conflict = make_record("C1", "Sinuhe", volume="osa 2")
    cand_match = make_record("C2", "Sinuhe", volume="osa 1")
    cand_none = make_record("C3", "Sinuhe", volume=None)

    assert not reranker.check_volume_match(
        target.volume_designation, cand_conflict.volume_designation
    )
    assert reranker.check_volume_match(target.volume_designation, cand_match.volume_designation)
    assert reranker.check_volume_match(target.volume_designation, cand_none.volume_designation)


def test_surname_match() -> None:
    reranker = SparseReranker(SparseRerankConfig(require_surname_match=True))

    target = make_record("T1", "Sinuhe", surname="Waltari")
    cand_same = make_record("C1", "Sinuhe", surname="Waltari")
    cand_diff = make_record("C2", "Sinuhe", surname="Virtanen")

    assert reranker.check_surname_match(target, cand_same)
    assert not reranker.check_surname_match(target, cand_diff)


def test_year_disqualification() -> None:
    reranker = SparseReranker(SparseRerankConfig(max_year_difference=5, year_penalty_threshold=1))

    is_valid, penalty, diff = reranker.evaluate_year(1945, 1946)
    assert is_valid and diff == 1 and penalty == 0.0

    is_valid, penalty, diff = reranker.evaluate_year(1945, 1949)
    assert is_valid and diff == 4 and penalty > 0.0

    is_valid, _, diff = reranker.evaluate_year(1945, 1955)
    assert not is_valid and diff == 10


def test_short_circuit_with_volume_safeguard() -> None:
    config = SparseRerankConfig(
        short_circuit=ShortCircuitConfig(enabled=True, threshold=0.98),
        require_volume_match=True,
    )
    reranker = SparseReranker(config)

    target = make_record("T1", "Sinuhe", volume="osa 1")
    cand_match = make_record("C1", "Sinuhe", volume="osa 1")
    cand_conflict = make_record("C2", "Sinuhe", volume="osa 2")

    # High score with matching volume -> accepted tier2_dense_faiss
    m_pass = reranker.evaluate_candidate(target, cand_match, dense_score=0.99)
    assert m_pass.accepted
    assert m_pass.match_tier == "tier2_dense_faiss"

    # High score with conflicting volume -> disqualified!
    m_fail = reranker.evaluate_candidate(target, cand_conflict, dense_score=0.99)
    assert not m_fail.accepted
    assert not m_fail.volume_match
