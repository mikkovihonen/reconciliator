"""Tests for AgentClusterer cross-source agent identity resolution and cluster aggregation."""

from __future__ import annotations

from typing import Any

import pytest

from reconciliator.clustering import AgentClusterer
from reconciliator.config import AgentClusterConfig, AppConfig
from reconciliator.database import WEMIRepository
from reconciliator.models import (
    AgentEntity,
    AgentRelationshipEntity,
    ContributionEntity,
    EntitySubject,
    IdentifierEntity,
    WorkEntity,
)


@pytest.fixture
def repo(tmp_storage: Any) -> WEMIRepository:
    """Initialize test repository connected to isolated Postgres test schema."""
    return WEMIRepository(
        db_path=tmp_storage.database_url,
        dimension=4,
    )


def test_agent_clustering_configuration_integrity() -> None:
    """Verify that agent clustering settings derive strictly from defaults.yaml with zero hardcoding."""
    app_cfg = AppConfig()
    cfg = app_cfg.agent_clustering

    assert cfg.enabled is True
    assert cfg.similarity_threshold == 0.85
    assert cfg.date_tolerance_years == 1
    assert cfg.major_date_discrepancy_veto_years == 2
    assert cfg.require_corroboration_for_date_discrepancy is True
    assert cfg.token_matching_algorithm == "token_set_ratio"
    assert cfg.max_blocking_bucket_size == 200
    assert cfg.date_tolerance_score == 0.70
    assert cfg.date_missing_score == 0.50
    assert "asteri" in cfg.source_authority_precedence
    assert "isni" in cfg.override_identifiers
    assert "name_normalized" in cfg.blocking_keys
    assert cfg.scoring_weights.name_weight == 0.35

    # Verify custom overrides work dynamically without python modification
    custom_cfg = AgentClusterConfig(
        similarity_threshold=0.92,
        date_tolerance_years=3,
        source_authority_precedence=["wikidata", "asteri"],
    )
    assert custom_cfg.similarity_threshold == 0.92
    assert custom_cfg.date_tolerance_years == 3
    assert custom_cfg.source_authority_precedence[0] == "wikidata"


def test_idempotent_agent_ingest_and_clean_model(repo: WEMIRepository) -> None:
    """Verify that agent ingestion is 100% idempotent without counts in raw tables."""
    agent = AgentEntity(
        source="melinda",
        source_id="agent_001",
        name="Waltari, Mika",
        name_normalized="waltari, mika",
        surname="Waltari",
        birth_year=1908,
        death_year=1979,
        qualifiers=["kirjailija"],
        variant_names=["Waltari, M."],
    )

    ids1 = repo.insert_agents_batch([agent])
    ids2 = repo.insert_agents_batch([agent])

    assert ids1 == ids2
    aid = ids1[0]

    fetched = repo.get_agent(aid)
    assert fetched is not None
    assert fetched.qualifiers == ["kirjailija"]
    assert fetched.variant_names == ["Waltari, M."]


def test_deterministic_identifier_linking(repo: WEMIRepository) -> None:
    """Verify that matching standard identifiers (ISNI) deterministically cluster agents."""
    agent1 = AgentEntity(
        source="melinda",
        source_id="mel_100",
        name="Jansson, Tove",
        name_normalized="jansson, tove",
        surname="Jansson",
        birth_year=1914,
        death_year=2001,
        identifiers=[
            IdentifierEntity(
                entity_type="agent",
                entity_id=0,
                source="isni",
                source_id="0000 0001 2148 7654",
                source_id_normalized="0000000121487654",
            )
        ],
    )
    agent2 = AgentEntity(
        source="wikidata",
        source_id="Q10149",
        name="Tove Jansson",
        name_normalized="tove jansson",
        surname="Jansson",
        birth_year=1914,
        death_year=2001,
        identifiers=[
            IdentifierEntity(
                entity_type="agent",
                entity_id=0,
                source="isni",
                source_id="0000-0001-2148-7654",
                source_id_normalized="0000000121487654",
            )
        ],
    )

    aids = repo.insert_agents_batch([agent1, agent2])
    clusterer = AgentClusterer(repo=repo)
    res = clusterer.cluster_agents()

    assert res.total_agents >= 2
    assert res.multi_member_clusters >= 1

    # Verify both agents belong to the same cluster
    c1 = repo.get_canonical_agent_uri(aids[0])
    c2 = repo.get_canonical_agent_uri(aids[1])
    assert c1 is not None
    assert c1 == c2


def test_identifier_override_of_date_discrepancy(repo: WEMIRepository) -> None:
    """Verify that verified standard identifiers override date discrepancies that would otherwise veto."""
    agent1 = AgentEntity(
        source="melinda",
        source_id="mel_101",
        name="Author, Test",
        name_normalized="author, test",
        surname="Author",
        birth_year=1910,
        identifiers=[
            IdentifierEntity(
                entity_type="agent",
                entity_id=0,
                source="isni",
                source_id="0000 0002 9999 8888",
                source_id_normalized="0000000299998888",
            )
        ],
    )
    agent2 = AgentEntity(
        source="wikidata",
        source_id="Q99999",
        name="Test Author",
        name_normalized="test author",
        surname="Author",
        birth_year=1915,  # 5 years diff -> normally vetoed
        identifiers=[
            IdentifierEntity(
                entity_type="agent",
                entity_id=0,
                source="isni",
                source_id="0000-0002-9999-8888",
                source_id_normalized="0000000299998888",
            )
        ],
    )

    aids = repo.insert_agents_batch([agent1, agent2])
    clusterer = AgentClusterer(repo=repo)
    clusterer.cluster_agents()

    c1 = repo.get_canonical_agent_uri(aids[0])
    c2 = repo.get_canonical_agent_uri(aids[1])
    assert c1 == c2


def test_corroborated_minor_date_discrepancy(repo: WEMIRepository) -> None:
    """Verify that 1-year date discrepancy is accepted with corroboration and rejected without."""
    # Pair A: 1-year discrepancy WITH shared work
    work = WorkEntity(
        source="test",
        source_id="w1",
        title="Shared Masterpiece",
        title_normalized="shared masterpiece",
    )
    wids = repo.insert_works_batch([work])
    wid = wids[0]

    agent_a1 = AgentEntity(
        source="melinda",
        source_id="a1",
        name="Virtanen, Kalle",
        name_normalized="virtanen, kalle",
        surname="Virtanen",
        birth_year=1920,
    )
    agent_a2 = AgentEntity(
        source="wikidata",
        source_id="a2",
        name="Kalle Virtanen",
        name_normalized="kalle virtanen",
        surname="Virtanen",
        birth_year=1921,  # 1 year diff
    )
    aids_a = repo.insert_agents_batch([agent_a1, agent_a2])
    repo.insert_contributions_batch(
        [
            ContributionEntity(
                entity_type="work",
                entity_id=wid,
                agent_id=aids_a[0],
                role="aut",
            ),
            ContributionEntity(
                entity_type="work",
                entity_id=wid,
                agent_id=aids_a[1],
                role="aut",
            ),
        ]
    )

    # Pair B: 1-year discrepancy WITHOUT corroboration
    agent_b1 = AgentEntity(
        source="melinda",
        source_id="b1",
        name="Korhonen, Pekka",
        name_normalized="korhonen, pekka",
        surname="Korhonen",
        birth_year=1930,
    )
    agent_b2 = AgentEntity(
        source="wikidata",
        source_id="b2",
        name="Pekka Korhonen",
        name_normalized="pekka korhonen",
        surname="Korhonen",
        birth_year=1931,  # 1 year diff, no works
    )
    aids_b = repo.insert_agents_batch([agent_b1, agent_b2])

    clusterer = AgentClusterer(repo=repo)
    clusterer.cluster_agents()

    # Pair A should be in the same cluster
    ca1 = repo.get_canonical_agent_uri(aids_a[0])
    ca2 = repo.get_canonical_agent_uri(aids_a[1])
    assert ca1 is not None and ca1 == ca2

    # Pair B should NOT be clustered together (different clusters)
    cb1 = repo.get_canonical_agent_uri(aids_b[0])
    cb2 = repo.get_canonical_agent_uri(aids_b[1])
    assert cb1 != cb2


def test_major_date_contradiction_homonym_guard(repo: WEMIRepository) -> None:
    """Verify that identical names with major date discrepancies are rejected from clustering."""
    doc = AgentEntity(
        source="melinda",
        source_id="doc_virtanen",
        name="Virtanen, Matti",
        name_normalized="virtanen, matti",
        surname="Virtanen",
        birth_year=1915,
        death_year=1980,
    )
    artist = AgentEntity(
        source="melinda",
        source_id="art_virtanen",
        name="Virtanen, Matti",
        name_normalized="virtanen, matti",
        surname="Virtanen",
        birth_year=1940,
        death_year=2010,
    )

    aids = repo.insert_agents_batch([doc, artist])
    clusterer = AgentClusterer(repo=repo)
    clusterer.cluster_agents()

    c1 = repo.get_canonical_agent_uri(aids[0])
    c2 = repo.get_canonical_agent_uri(aids[1])
    assert c1 != c2


def test_authority_hoisting_and_attribute_consensus(repo: WEMIRepository) -> None:
    """Verify that source authority precedence hoists canonical values and compiles attribute frequencies."""
    mel = AgentEntity(
        source="melinda",
        source_id="mel_tove",
        name="Jansson, Tove",
        name_normalized="jansson, tove",
        surname="Jansson",
        birth_year=1914,
        death_year=2001,
        variant_names=["Jansson, T."],
        qualifiers=["kirjailija"],
        identifiers=[
            IdentifierEntity(
                entity_type="agent",
                entity_id=0,
                source="isni",
                source_id="0000 0001 2148 7654",
                source_id_normalized="0000000121487654",
            )
        ],
    )
    ast = AgentEntity(
        source="asteri",
        source_id="ast_tove",
        name="Jansson, Tove Marika",
        name_normalized="jansson, tove marika",
        surname="Jansson",
        birth_year=1914,
        death_year=2001,
        variant_names=["Jansson, Tove"],
        identifiers=[
            IdentifierEntity(
                entity_type="agent",
                entity_id=0,
                source="isni",
                source_id="0000 0001 2148 7654",
                source_id_normalized="0000000121487654",
            )
        ],
    )

    aids = repo.insert_agents_batch([mel, ast])
    # Add affiliation for Asteri
    repo.insert_agent_relationships_batch(
        [
            AgentRelationshipEntity(
                source_agent_id=aids[1],
                target_name="Svenska författarföreningen",
                relationship_type="affiliation",
            )
        ]
    )
    # Add occupation concept
    repo.insert_entity_subjects_batch(
        [
            EntitySubject(
                entity_type="agent",
                entity_id=aids[1],
                relation_type="occupation",
                vocabulary="yso",
                concept_id="p4811",
                raw_label="kirjailijat",
            )
        ]
    )

    clusterer = AgentClusterer(repo=repo)
    clusterer.cluster_agents()

    cid = repo.get_agent_cluster_id(aids[1])
    assert cid is not None

    members = repo.get_agent_cluster_members(cid)
    assert len(members) == 2

    cluster = repo.get_agent_cluster(cid)
    assert cluster is not None
    # Asteri must be canonical per source_authority_precedence
    assert cluster.canonical_agent_source == "asteri"
    assert cluster.canonical_name == "Jansson, Tove Marika"
    assert cluster.member_count == 2

    # Check aggregated cluster attributes with frequency
    attrs = repo.get_agent_cluster_attributes(cid)
    variant_attrs = {a.value: a for a in attrs if a.attribute_type == "variant_name"}
    assert "Jansson, Tove Marika" in variant_attrs
    assert variant_attrs["Jansson, Tove Marika"].is_canonical is True

    # "Jansson, Tove" appeared both as primary name in Melinda and variant in Asteri -> freq = 2!
    assert "Jansson, Tove" in variant_attrs
    assert variant_attrs["Jansson, Tove"].frequency == 2

    # Affiliation
    affil_attrs = [a for a in attrs if a.attribute_type == "affiliation"]
    assert len(affil_attrs) == 1
    assert affil_attrs[0].value == "Svenska författarföreningen"
    assert affil_attrs[0].frequency == 1

    # Occupation
    occ_attrs = [a for a in attrs if a.attribute_type == "occupation"]
    assert len(occ_attrs) == 1
    assert occ_attrs[0].value == "p4811" or occ_attrs[0].value == "kirjailijat"
