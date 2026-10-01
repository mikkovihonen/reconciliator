"""Tests for decoupled reconciliation stages."""

from pathlib import Path
from unittest.mock import MagicMock

from reconciliator.config import ReconcileConfig, RerankConfig
from reconciliator.models import (
    AgentEntity,
    ExpressionEntity,
    IdentifierEntity,
    ManifestationEntity,
    MatchRecord,
    ReconciliationMatch,
    WorkEntity,
)
from reconciliator.reconciliation.matcher import WorkMatcher
from reconciliator.reconciliation.stages import (
    AgentResolutionStage,
    CandidateRankingStage,
    CandidateRetrievalStage,
    IdentifierMatchingStage,
    extract_agent_uris_from_identifiers,
)


def test_identifier_matching_stage():
    repo = MagicMock()
    stage = IdentifierMatchingStage(repo=repo, authoritative_only=True)

    # Align expression
    e1 = ExpressionEntity(
        work_id=1,
        source="melinda",
        source_id="e1",
        title="English",
        language="eng",
        content_type="text",
    )
    e2 = ExpressionEntity(
        work_id=1,
        source="melinda",
        source_id="e2",
        title="Finnish",
        language="fin",
        content_type="text",
    )
    assert stage.align_expression([e1, e2], target_language="fin") == e2

    # Fast path identifier match manifestation
    target = MatchRecord(
        record_id="rec1",
        format="marcxml",
        title="Title",
        title_stem="title",
    )
    work = WorkEntity(
        id=1,
        source="melinda",
        source_id="w1",
        work_uri="urn:work:1",
        title="Title",
        title_normalized="title",
    )
    manif = ManifestationEntity(
        id=10,
        source="melinda",
        source_id="m1",
        work_id=1,
        manifestation_uri="urn:manif:10",
        title="Title",
        title_normalized="title",
    )
    expr = ExpressionEntity(
        id=100,
        work_id=1,
        source="melinda",
        source_id="e100",
        language="fin",
        title="Title",
    )

    repo.lookup_by_identifier.return_value = [("manifestation", 10)]
    repo.get_manifestation.return_value = manif
    repo.get_work.return_value = work
    repo.get_expressions_for_manifestation.return_value = [expr]

    res = stage.try_identifiers(target, {"isbn": ["12345"]}, funnel_name="test")
    assert res is not None
    assert res.matched_work == work
    assert res.matched_manifestation == manif
    assert res.match_tier == "tier1_exact_identifier"


def test_candidate_retrieval_stage():
    repo = MagicMock()
    config = ReconcileConfig(target_file=Path("test.xml"), rerank=RerankConfig(top_k=42))
    stage = CandidateRetrievalStage(repo=repo, config=config)

    assert stage.retrieval_top_k() == 42
    assert stage.retrieval_top_k(default=10) == 42


def test_candidate_ranking_stage():
    repo = MagicMock()
    stage = CandidateRankingStage(repo=repo)

    rec = MatchRecord(
        record_id="r1",
        format="marcxml",
        title="Test Title",
        title_stem="test title",
        uniform_title="Uniform",
        year=2020,
        author_key="smith",
        identifiers={"isbn": ["111"]},
    )
    view = stage.target_view(rec)
    assert view.title == "Test Title"
    assert view.publication_year == 2020

    # Hard checks pass
    pass_match = ReconciliationMatch(
        candidate_record_id="c1",
        source="melinda",
        match_tier="tier2",
        match_score=0.9,
        volume_match=True,
        surname_match=True,
        year_diff=2,
    )
    assert stage.hard_checks_pass(pass_match) is True

    fail_volume = ReconciliationMatch(
        candidate_record_id="c2",
        source="melinda",
        match_tier="tier2",
        match_score=0.9,
        volume_match=False,
        surname_match=True,
    )
    assert stage.hard_checks_pass(fail_volume) is False


def test_extract_agent_uris_from_identifiers():
    repo = MagicMock()
    agent = AgentEntity(
        id=5,
        name="Tester",
        name_normalized="tester",
        source="viaf",
        source_id="12345",
        identifiers=[
            IdentifierEntity(entity_type="agent", scheme="wikidata", source_id="Q42"),
            IdentifierEntity(entity_type="agent", scheme="isni", source_id="00000001"),
        ],
    )
    u0, u1 = extract_agent_uris_from_identifiers(agent, repo)
    assert any("wikidata.org" in u for u in u1)
    assert any("viaf" in u for u in u0)
    assert any("isni" in u for u in u0)


def test_work_matcher_composition():
    repo = MagicMock()
    config = ReconcileConfig(target_file=Path("test.xml"), rerank=RerankConfig(top_k=25))
    matcher = WorkMatcher(wemi_repo=repo, config=config)

    assert isinstance(matcher.identifier_stage, IdentifierMatchingStage)
    assert isinstance(matcher.ranking_stage, CandidateRankingStage)
    assert isinstance(matcher.retrieval_stage, CandidateRetrievalStage)
    assert isinstance(matcher.agent_stage, AgentResolutionStage)
    assert matcher._retrieval_top_k() == 25
