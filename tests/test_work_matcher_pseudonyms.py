"""Tests for author pseudonym matching in work-to-manifestation candidate evaluation."""

from reconciliator.config import RerankConfig
from reconciliator.models import Agent, BibliographicRecord
from reconciliator.rerank.evaluator import CandidateEvaluator


def test_surname_match_exact():
    cfg = RerankConfig(require_surname_match=True)
    evaluator = CandidateEvaluator(config=cfg)

    target = BibliographicRecord(
        source="test",
        source_id="1",
        title="Työmiehen vaimo",
        agents=[Agent(name="Minna Canth", surname="canth")],
    )
    candidate = BibliographicRecord(
        source="test",
        source_id="2",
        title="Työmiehen vaimo",
        agents=[Agent(name="Canth, Minna", surname="canth")],
    )

    assert evaluator.check_surname_match(target, candidate) is True


def test_surname_match_pseudonym_relationship():
    cfg = RerankConfig(require_surname_match=True)
    evaluator = CandidateEvaluator(config=cfg)

    # Manifestation authored under pseudonym Wilja
    target = BibliographicRecord(
        source="test",
        source_id="1",
        title="Novelleja",
        agents=[Agent(name="Wilja", surname="wilja")],
    )
    # Canonical work authored under real name Canth
    candidate = BibliographicRecord(
        source="test",
        source_id="2",
        title="Novelleja",
        agents=[Agent(name="Canth, Minna", surname="canth")],
    )

    # Without relationship knowledge, surname mismatch fails
    assert evaluator.check_surname_match(target, candidate) is False

    # With relationship knowledge connecting wilja <-> canth
    author_rels = {"wilja": {"canth"}}
    assert (
        evaluator.check_surname_match(target, candidate, author_relationships=author_rels)
        is True
    )
