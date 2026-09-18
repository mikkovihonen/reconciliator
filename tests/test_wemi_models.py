"""Unit tests for WEMI domain entities, controlled vocabularies, and schemas."""

import json

import pytest
from pydantic import ValidationError

from reconciliator.models import (
    AgentEntity,
    ContributionEntity,
    EntitySubject,
    ExpressionEntity,
    HarvestState,
    IdentifierEntity,
    ItemEntity,
    ManifestationCluster,
    ManifestationEntity,
    ManifestationExpressionAssociation,
    MultiplexClusterResult,
    WorkCluster,
    WorkEntity,
    WorkRelationship,
)


def test_work_entity_validations():
    work = WorkEntity(
        source="melinda",
        source_id="000123456",
        title="Tuntematon sotilas",
        title_normalized="tuntematon sotilas",
        original_language="fin",
        work_type="text",
    )
    assert work.work_type == "text"
    assert work.curie == "melinda:000123456"

    # Test EntitySubject model for decoupled subject relations
    es = EntitySubject(
        entity_type="work",
        raw_label="sotakirjallisuus",
        relation_type="topic",
        vocabulary="yso",
        concept_id="Y1002",
    )
    assert es.raw_label == "sotakirjallisuus"
    assert es.vocabulary == "yso"
    assert es.concept_id == "Y1002"


def test_work_types_controlled_vocabulary():
    # Valid types
    for wt in [
        "text",
        "thesis",
        "manuscript",
        "serial",
        "music",
        "codex_fragment",
        "ephemera",
        "heritage_model",
    ]:
        w = WorkEntity(
            work_uri=f"uri_{wt}",
            source="test",
            source_id=f"id_{wt}",
            title=f"Title {wt}",
            title_normalized=f"title {wt}",
            work_type=wt,
        )
        assert w.work_type == wt

    # Invalid type
    with pytest.raises(ValidationError):
        WorkEntity(
            work_uri="uri_invalid",
            source="test",
            source_id="id_invalid",
            title="Invalid",
            title_normalized="invalid",
            work_type="non_existent_type",  # type: ignore
        )


def test_expression_entity_validations():
    exp = ExpressionEntity(
        work_id=1,
        source="melinda",
        source_id="exp_001",
        language="fin",
        content_type="text",
        title="Tuntematon sotilas (alkuperäinen laitos)",
        version_designation="1. painoksen teksti",
    )
    assert exp.work_id == 1
    assert exp.curie == "melinda:exp_001"
    assert exp.content_type == "text"
    assert exp.language == "fin"

    # Audio music expression
    music_exp = ExpressionEntity(
        work_id=2,
        expression_uri="https://kansalliskirjasto.finna.fi/Record/exp_audio_1",
        source="melinda",
        source_id="exp_audio_001",
        language="und",
        content_type="audio_music",
    )
    assert music_exp.content_type == "audio_music"

    # Invalid content_type
    with pytest.raises(ValidationError):
        ExpressionEntity(
            work_id=1,
            expression_uri="exp_bad",
            source="test",
            source_id="bad",
            language="fin",
            content_type="invalid_content_type",  # type: ignore
        )


def test_manifestation_entity_validations():
    manif = ManifestationEntity(
        work_id=1,
        source="melinda",
        source_id="bib_9999",
        manifestation_uri="https://kansalliskirjasto.finna.fi/Record/melinda.9999",
        title="Tuntematon sotilas",
        title_normalized="tuntematon sotilas",
        publication_year=1954,
        publisher="WSOY",
        publication_place="Helsinki",
        edition_statement="1. p.",
        edition_number=1,
        carrier_type="volume",
        is_component=False,
    )
    assert manif.carrier_type == "volume"
    assert manif.publication_year == 1954
    assert manif.is_component is False

    # Component manifestation (e.g. audio track or article)
    component = ManifestationEntity(
        container_id=10,
        is_component=True,
        component_type="track",
        source="melinda",
        source_id="track_10_1",
        title="Track 1: Intro",
        title_normalized="track 1 intro",
        carrier_type="audio_disc",
    )
    assert component.is_component is True
    assert component.component_type == "track"
    assert component.carrier_type == "audio_disc"


def test_manifestation_expression_association():
    assoc = ManifestationExpressionAssociation(
        manifestation_id=10,
        expression_id=5,
        track_number=1,
        disc_number=1,
        duration="3:45",
        is_primary=True,
    )
    assert assoc.manifestation_id == 10
    assert assoc.track_number == 1
    assert assoc.duration == "3:45"


def test_item_entity():
    item = ItemEntity(
        manifestation_id=10,
        item_uri="item_123",
        source="koha",
        source_id="koha_item_999",
        holding_organization="National Library of Finland",
        shelf_mark="Kansalliskokoelma Fennica 1954:12",
        barcode="31234567890",
        custodial_history="Donated in 1955",
        item_condition="good",
    )
    assert item.manifestation_id == 10
    assert item.barcode == "31234567890"


def test_polymorphic_identifiers():
    ident = IdentifierEntity(
        entity_type="manifestation",
        entity_id=10,
        scheme="isbn",
        value="978-951-0-12345-6",
        value_normalized="9789510123456",
    )
    assert ident.entity_type == "manifestation"
    assert ident.scheme == "isbn"

    # Agent ISNI
    ident_agent = IdentifierEntity(
        entity_type="agent",
        entity_id=1,
        scheme="isni",
        value="0000 0001 2144 1234",
        value_normalized="0000000121441234",
    )
    assert ident_agent.entity_type == "agent"


def test_agent_and_contributions():
    agent = AgentEntity(
        source="kanto",
        source_id="000012345",
        name="Linna, Väinö",
        name_normalized="linna vaino",
        surname="Linna",
        is_personal=True,
    )
    assert agent.surname == "Linna"
    assert agent.curie == "kanto:000012345"
    assert agent.is_personal is True

    contrib = ContributionEntity(
        entity_type="work",
        entity_id=1,
        agent_id=1,
        role="aut",
        is_primary=True,
    )
    assert contrib.role == "aut"
    assert contrib.is_primary is True


def test_work_relationships():
    rel = WorkRelationship(
        source_work_id=1,
        target_work_id=2,
        relationship_type="adaptation_of",
    )
    assert rel.relationship_type == "adaptation_of"

    # Invalid relationship type
    with pytest.raises(ValidationError):
        WorkRelationship(
            source_work_id=1,
            target_work_id=2,
            relationship_type="random_relation",  # type: ignore
        )


def test_medium_of_performance():
    mop = EntitySubject(
        entity_type="work",
        entity_id=100,
        concept_id="http://rdaregistry.info/termList/mop/1001",
        raw_label="piano",
        relation_type="medium_of_performance",
        vocabulary="seko",
        is_resolved=True,
    )
    assert mop.raw_label == "piano"
    assert mop.relation_type == "medium_of_performance"
    assert mop.vocabulary == "seko"
    assert mop.concept_id == "http://rdaregistry.info/termList/mop/1001"


def test_harvest_state():
    hs = HarvestState(
        source_id="melinda_oai",
        source_type="oai-pmh",
        metadata_prefix="marc21",
        set_spec="fennica",
        last_harvest_from="2026-01-01",
        last_harvest_until="2026-01-31",
        resumption_token="token_abc_123",
        records_harvested=5420,
        status="in_progress",
    )
    assert hs.records_harvested == 5420
    assert hs.status == "in_progress"


def test_multiplex_clustering_models():
    mc = ManifestationCluster(
        cluster_id="m_cluster_1",
        canonical_record_id="rec_101",
        publisher="WSOY",
        publication_year=1954,
        carrier_type="volume",
        member_record_count=2,
    )
    wc = WorkCluster(
        cluster_id="w_cluster_1",
        work_type="text",
        canonical_title="Tuntematon sotilas",
        canonical_author="Linna, Väinö",
    )
    res = MultiplexClusterResult(
        work_clusters_count=1,
        expression_clusters_count=1,
        manifestation_clusters_count=1,
        records_clustered=2,
        elapsed_seconds=0.15,
    )
    assert mc.canonical_record_id == "rec_101"
    assert wc.canonical_title == "Tuntematon sotilas"
    assert res.records_clustered == 2
