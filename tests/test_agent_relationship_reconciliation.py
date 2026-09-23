"""Tests for agent relationship utilization in reconciliation and selective guards."""

from reconciliator.models import Agent
from reconciliator.reconciliation.agents import (
    AgentFallbackResolver as AgentReconciler,
)
from reconciliator.reconciliation.agents import (
    check_agent_relationship,
)


def test_check_agent_relationship():
    target = Agent(
        name="Wilja",
        agent_uri="http://urn.fi/URN:NBN:fi:au:pn:000149841",
    )
    target.relationships = [
        {
            "relationship_type": "real_identity_of",
            "target_agent_curie": "http://urn.fi/URN:NBN:fi:au:pn:000149842",
            "target_name": "Canth, Minna",
        }
    ]

    cand_matching = {
        "id": 10,
        "name": "Canth, Minna",
        "agent_uri": "http://urn.fi/URN:NBN:fi:au:pn:000149842",
    }
    cand_unrelated = {
        "id": 11,
        "name": "Virtanen, Matti",
        "agent_uri": "http://urn.fi/URN:NBN:fi:au:pn:000999999",
    }

    assert check_agent_relationship(target, cand_matching) == "real_identity_of"
    assert check_agent_relationship(target, cand_unrelated) is None


def test_evaluate_candidates_pseudonym_surname_bypass():
    reconciler = AgentReconciler(
        similarity_threshold=0.70,
        surname_match_threshold=85.0,
    )

    target_wilja = Agent(
        name="Wilja",
        surname="Wilja",
        birth_year=1844,
        death_year=1897,
        agent_uri="http://urn.fi/URN:NBN:fi:au:pn:000149841",
    )
    target_wilja.relationships = [
        {
            "relationship_type": "real_identity_of",
            "target_agent_curie": "http://urn.fi/URN:NBN:fi:au:pn:000149842",
            "target_name": "Canth, Minna",
        }
    ]

    candidate_canth = {
        "id": 42,
        "name": "Canth, Minna",
        "surname": "Canth",
        "birth_year": 1844,
        "death_year": 1897,
        "score": 0.85,
        "agent_uri": "http://urn.fi/URN:NBN:fi:au:pn:000149842",
    }

    best_cand, reason = reconciler.evaluate_candidates(
        target_agent=target_wilja,
        target_pub_year=1885,
        candidates=[candidate_canth],
    )

    assert best_cand is not None
    assert best_cand["name"] == "Canth, Minna"
    assert reason == "accepted_via_relationship_real_identity_of"


def test_evaluate_candidates_corporate_lineage_date_bypass():
    reconciler = AgentReconciler(
        similarity_threshold=0.70,
        life_date_tolerance_years=1,
    )

    target_karhula = Agent(
        name="Karhula Osakeyhtiö",
        is_personal=False,
        birth_year=1873,
        death_year=1915,
        agent_uri="http://urn.fi/URN:NBN:fi:au:pn:000032484",
    )
    target_karhula.relationships = [
        {
            "relationship_type": "successor_of",
            "target_agent_curie": "http://urn.fi/URN:NBN:fi:au:pn:000032485",
            "target_name": "A. Ahlström Karhulan tekniset",
        }
    ]

    candidate_successor = {
        "id": 101,
        "name": "A. Ahlström Karhulan tekniset",
        "is_personal": False,
        "birth_year": 1915,
        "death_year": 1998,
        "score": 0.80,
        "agent_uri": "http://urn.fi/URN:NBN:fi:au:pn:000032485",
    }

    best_cand, reason = reconciler.evaluate_candidates(
        target_agent=target_karhula,
        target_pub_year=1920,
        candidates=[candidate_successor],
    )

    assert best_cand is not None
    assert reason == "accepted_via_relationship_successor_of"
