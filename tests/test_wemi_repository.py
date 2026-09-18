"""Unit tests for WEMIRepository batch operations, navigation, FTS, and vector search."""

from pathlib import Path
from typing import Any

import numpy as np
import pytest

from reconciliator.indexer import TorchVectorEngine
from reconciliator.models import (
    AgentEntity,
    ContributionEntity,
    EntityKey,
    ExpressionEntity,
    HarvestState,
    IdentifierEntity,
    ItemEntity,
    ManifestationEntity,
    ManifestationExpressionAssociation,
    MatchRecord,
    MediumOfPerformance,
    WorkEntity,
    WorkRelationship,
)
from reconciliator.repository import WEMIRepository
from tests.conftest import persist_match_records


@pytest.fixture
def repo(tmp_path: Path) -> WEMIRepository:
    db_path = tmp_path / "authority.db"
    w_idx = TorchVectorEngine(tmp_path / "work.pt", dimension=4)
    m_idx = TorchVectorEngine(tmp_path / "manif.pt", dimension=4)
    return WEMIRepository(
        db_path=db_path,
        dimension=4,
        work_indexer=w_idx,
        manifestation_indexer=m_idx,
    )


def test_insert_and_get_agents(repo: WEMIRepository):
    agent = AgentEntity(
        source="asteri",
        source_id="1",
        name="Mika Waltari",
        name_normalized="mika waltari",
        surname="Waltari",
        is_personal=True,
    )
    ids = repo.insert_agents_batch([agent])
    assert len(ids) == 1
    agent_id = ids[0]

    fetched = repo.get_agent(agent_id)
    assert fetched is not None
    assert fetched.name == "Mika Waltari"
    assert fetched.curie == "asteri:1"

    fetched_by_curie = repo.get_agent_by_curie("asteri:1")
    assert fetched_by_curie is not None
    assert fetched_by_curie.id == agent_id


def test_insert_and_get_works_with_vectors_and_fts(repo: WEMIRepository):
    works = [
        WorkEntity(
            source="melinda",
            source_id="w_sinuhe",
            title="Sinuhe egyptiläinen",
            title_normalized="sinuhe egyptilainen",
            original_language="fin",
            work_type="text",
            subjects_json=["muinainen egypti", "historialliset romaanit"],
            serialized_text="Mika Waltari Sinuhe egyptiläinen historiallinen romaani",
        ),
        WorkEntity(
            source="melinda",
            source_id="w_turms",
            title="Turms kuolematon",
            title_normalized="turms kuolematon",
            original_language="fin",
            work_type="text",
            subjects_json=["etruskit", "historialliset romaanit"],
            serialized_text="Mika Waltari Turms kuolematon etruskit",
        ),
    ]
    vectors = np.array(
        [
            [0.1, 0.2, 0.3, 0.4],
            [0.5, 0.6, 0.7, 0.8],
        ],
        dtype=np.float32,
    )

    work_ids = repo.insert_works_batch(works, vectors=vectors)
    assert len(work_ids) == 2

    # Test single getters
    w1 = repo.get_work(work_ids[0])
    assert w1 is not None
    assert w1.title == "Sinuhe egyptiläinen"
    assert "muinainen egypti" in [s["heading"] for s in w1.subjects_json]

    w_by_curie = repo.get_work_by_curie("melinda:w_turms")
    assert w_by_curie is not None
    assert w_by_curie.id == work_ids[1]

    w_by_source = repo.get_work_by_source("melinda", "w_sinuhe")
    assert w_by_source is not None
    assert w_by_source.id == work_ids[0]

    # Test FTS search
    fts_results = repo.search_works_fts("egyptiläinen")
    assert len(fts_results) >= 1
    assert fts_results[0][0].title == "Sinuhe egyptiläinen"

    # Test vector search
    query_vec = np.array([0.1, 0.2, 0.3, 0.4], dtype=np.float32)
    vec_results = repo.search_work_vectors(query_vec, top_k=2)
    assert len(vec_results) == 2
    assert vec_results[0][0].id == work_ids[0]


def test_insert_and_navigate_wemi_hierarchy(repo: WEMIRepository):
    # 1. Work
    w_ids = repo.insert_works_batch(
        [
            WorkEntity(
                work_uri="urn:work:100",
                source="melinda",
                source_id="w100",
                title="Symphony No. 5",
                title_normalized="symphony no 5",
                work_type="music",
                serialized_text="Jean Sibelius Symphony No. 5 op. 82",
            )
        ]
    )
    work_id = w_ids[0]

    # 2. Expression
    e_ids = repo.insert_expressions_batch(
        [
            ExpressionEntity(
                work_id=work_id,
                expression_uri="urn:exp:100:audio",
                source="melinda",
                source_id="e100_1",
                language="zxx",
                content_type="audio_music",
                title="Symphony No. 5 (Berliner Philharmoniker)",
                serialized_text="Sibelius Symphony 5 Karajan Berliner Philharmoniker",
            )
        ]
    )
    exp_id = e_ids[0]

    # 3. Manifestation
    m_ids = repo.insert_manifestations_batch(
        [
            ManifestationEntity(
                work_id=work_id,
                manifestation_uri="urn:manif:100:cd",
                source="melinda",
                source_id="m100_1",
                title="Sibelius: Symphonies 4 & 5 (CD)",
                title_normalized="sibelius symphonies 4 and 5 cd",
                publication_year=1987,
                publisher="Deutsche Grammophon",
                carrier_type="audio_disc",
                serialized_text="Sibelius Symphonies 4 and 5 Karajan DG CD 1987",
            )
        ]
    )
    manif_id = m_ids[0]

    # 4. Junction
    repo.insert_manifestation_expressions_batch(
        [
            ManifestationExpressionAssociation(
                manifestation_id=manif_id,
                expression_id=exp_id,
                track_number=2,
                disc_number=1,
                duration="31:40",
            )
        ]
    )

    # 5. Item
    repo.insert_items_batch(
        [
            ItemEntity(
                manifestation_id=manif_id,
                source="koha",
                source_id="item_cd_001",
                holding_organization="Helsinki City Library",
                shelf_mark="78.511 SIB",
                barcode="12345678",
            )
        ]
    )

    # Navigation tests
    exps = repo.get_expressions_for_work(work_id)
    assert len(exps) == 1
    assert exps[0].id == exp_id

    manifs = repo.get_manifestations_for_work(work_id)
    assert len(manifs) == 1
    assert manifs[0].id == manif_id

    manif_exps = repo.get_expressions_for_manifestation(manif_id)
    assert len(manif_exps) == 1
    assert manif_exps[0].id == exp_id

    exp_manifs = repo.get_manifestations_for_expression(exp_id)
    assert len(exp_manifs) == 1
    assert exp_manifs[0].id == manif_id

    items = repo.get_items_for_manifestation(manif_id)
    assert len(items) == 1
    assert items[0].barcode == "12345678"


def test_polymorphic_identifiers_lookup(repo: WEMIRepository):
    identifiers = [
        IdentifierEntity(
            entity_type="manifestation",
            entity_id=42,
            scheme="isbn",
            value="978-951-0-99999-9",
            value_normalized="9789510999999",
        ),
        IdentifierEntity(
            entity_type="work",
            entity_id=10,
            scheme="wikidata_qid",
            value="Q7777",
            value_normalized="q7777",
        ),
    ]
    repo.insert_identifiers_batch(identifiers)

    # Exact lookup normalized
    res = repo.lookup_by_identifier("isbn", "9789510999999")
    assert len(res) == 1
    assert res[0] == ("manifestation", 42)

    # Exact lookup unnormalized value
    res2 = repo.lookup_by_identifier("isbn", "978-951-0-99999-9")
    assert len(res2) == 1
    assert res2[0] == ("manifestation", 42)

    # Wikidata QID lookup
    res_qid = repo.lookup_by_identifier("wikidata_qid", "Q7777")
    assert len(res_qid) == 1
    assert res_qid[0] == ("work", 10)

    # Fetch entity identifiers
    entity_ids = repo.get_identifiers_for_entity("manifestation", 42)
    assert len(entity_ids) == 1
    assert entity_ids[0].scheme == "isbn"


def test_contributions_and_relationships(repo: WEMIRepository):
    # Insert prerequisite agents and works for FK constraints
    agent_ids = repo.insert_agents_batch(
        [
            AgentEntity(
                agent_uri="http://kanto.fi/agent/author_1",
                source="asteri",
                name="Kirjailija Yksi",
                name_normalized="kirjailija yksi",
            )
        ]
    )
    work_ids = repo.insert_works_batch(
        [
            WorkEntity(
                work_uri="urn:work:rel_1",
                source="melinda",
                source_id="w_rel_1",
                title="Teos Yksi",
                title_normalized="teos yksi",
            ),
            WorkEntity(
                work_uri="urn:work:rel_2",
                source="melinda",
                source_id="w_rel_2",
                title="Teos Kaksi",
                title_normalized="teos kaksi",
            ),
        ]
    )
    agent_id = agent_ids[0]
    w1_id, w2_id = work_ids[0], work_ids[1]

    # Contributions
    c_ids = repo.insert_contributions_batch(
        [
            ContributionEntity(
                entity_type="work",
                entity_id=w1_id,
                agent_id=agent_id,
                role="aut",
                is_primary=True,
            )
        ]
    )
    assert len(c_ids) == 1
    contribs = repo.get_contributions_for_entity("work", w1_id)
    assert len(contribs) == 1
    assert contribs[0].agent_id == agent_id
    assert contribs[0].role == "aut"

    # Work Relationships
    r_ids = repo.insert_work_relationships_batch(
        [
            WorkRelationship(
                source_work_id=w1_id,
                target_work_id=w2_id,
                relationship_type="supersedes",
            )
        ]
    )
    assert len(r_ids) == 1
    rels = repo.get_work_relationships(w1_id)
    assert len(rels) == 1
    assert rels[0].relationship_type == "supersedes"

    # Medium of Performance (verifying deduplication & ON CONFLICT DO UPDATE within same batch)
    repo.insert_medium_of_performance_batch(
        [
            MediumOfPerformance(
                work_id=w1_id,
                medium_uri="http://rdaregistry.info/termList/mop/violin",
                label="violin",
                player_count=1,
                is_soloist=False,
            ),
            MediumOfPerformance(
                work_id=w1_id,
                medium_uri="http://rdaregistry.info/termList/mop/violin",
                label="violin",
                player_count=2,
                is_soloist=True,
            ),
        ]
    )
    mops = repo.get_mediums_of_performance(w1_id)
    assert len(mops) == 1
    assert mops[0].medium_uri == "http://rdaregistry.info/termList/mop/violin"
    assert mops[0].player_count == 2
    assert mops[0].is_soloist is True


def test_insert_wemi_batch_mediums_of_performance_multi_work_binding(repo: WEMIRepository):
    """Verify that multi-work batches correctly bind MOPs by work_curie, dedup, and drop unbounds."""
    w1 = WorkEntity(
        source="fennica",
        source_id="w1_mop",
        title="Violin Concerto",
        title_normalized="violin concerto",
        work_type="music",
    )
    w2 = WorkEntity(
        source="fennica",
        source_id="w2_mop",
        title="Piano Sonata",
        title_normalized="piano sonata",
        work_type="music",
    )
    mops = [
        # Two entries for w1 with the same URI to verify deduplication
        MediumOfPerformance(
            work_curie=w1.curie,
            medium_uri="http://rdaregistry.info/termList/mop/violin",
            label="violin",
            player_count=1,
            is_soloist=True,
        ),
        MediumOfPerformance(
            work_curie=w1.curie,
            medium_uri="http://rdaregistry.info/termList/mop/violin",
            label="violin",
            player_count=1,
            is_soloist=True,
        ),
        # One entry for w2
        MediumOfPerformance(
            work_curie=w2.curie,
            medium_uri="http://rdaregistry.info/termList/mop/piano",
            label="piano",
            player_count=1,
            is_soloist=True,
        ),
        # One unbound entry (should be safely dropped, not mapped to w1)
        MediumOfPerformance(
            work_curie="urn:work:nonexistent:9999",
            medium_uri="http://rdaregistry.info/termList/mop/flute",
            label="flute",
        ),
    ]

    repo.insert_wemi_batch(
        works=[w1, w2],
        expressions=[],
        manifestations=[],
        mediums_of_performance=mops,
    )

    assert w1.id is not None and w1.id > 0
    assert w2.id is not None and w2.id > 0

    w1_mops = repo.get_mediums_of_performance(w1.id)
    assert len(w1_mops) == 1
    assert w1_mops[0].medium_uri == "http://rdaregistry.info/termList/mop/violin"

    w2_mops = repo.get_mediums_of_performance(w2.id)
    assert len(w2_mops) == 1
    assert w2_mops[0].medium_uri == "http://rdaregistry.info/termList/mop/piano"


def test_harvest_state_checkpoint(repo: WEMIRepository):
    state = HarvestState(
        source_id="melinda_books",
        source_type="oai-pmh",
        metadata_prefix="marc21",
        set_spec="books",
        last_harvest_from="2026-01-01T00:00:00Z",
        last_harvest_until="2026-02-01T00:00:00Z",
        resumption_token="tok_12345",
        records_harvested=1250,
        status="in_progress",
    )
    repo.save_harvest_state(state)

    fetched = repo.get_harvest_state("melinda_books")
    assert fetched is not None
    assert fetched.records_harvested == 1250
    assert fetched.resumption_token == "tok_12345"

    # Update state
    state.records_harvested = 2500
    state.status = "completed"
    repo.save_harvest_state(state)

    updated = repo.get_harvest_state("melinda_books")
    assert updated is not None
    assert updated.records_harvested == 2500
    assert updated.status == "completed"


def test_corpus_repository_normalized_staging(tmp_path: Path):
    """Verify WEMIRepository corpus staging with normalized keys, detached payloads, and SQL blocking queries."""
    auth_db = tmp_path / "authority.db"
    c_repo = WEMIRepository(auth_db)

    # 1. Create MatchRecord with multi-valued and custom keys
    t1 = MatchRecord(
        record_id="target_001",
        format="marcxml",
        work_type="music",
        title="Finlandia op. 26",
        title_stem="finlandia op 26",
        year=1900,
        keys={
            "work_blocking": ["sibelius::finlandia"],
            "music_work": ["sibelius::finlandia::op26"],
            "standard": ["M-042-08955-4", "979-0-042-08955-4"],
            "custom_archive": ["ark_12345/x987"],
        },
        raw_payload=b"<record><datafield tag='245'><subfield code='a'>Finlandia</subfield></datafield></record>",
        serialized_work="Sibelius Finlandia",
    )

    t2 = MatchRecord(
        record_id="target_002",
        format="marcxml",
        work_type="music",
        title="Finlandia piano arrangement",
        title_stem="finlandia piano arrangement",
        year=1901,
        keys={
            "work_blocking": ["sibelius::finlandia"],
            "music_work": ["sibelius::finlandia::op26"],
            "standard": ["M-042-08955-4"],
        },
        raw_payload=b"<record><datafield tag='245'><subfield code='a'>Finlandia piano</subfield></datafield></record>",
        serialized_work="Sibelius Finlandia piano",
    )

    # 2. Insert batch
    persist_match_records(c_repo, [t1, t2])

    # 3. Project the records back out and verify the reconstructed key bag.
    # raw_payload is deliberately absent: the record is rebuilt from rows, and
    # nothing re-parses a payload at match time any more.
    all_recs = c_repo.get_all_match_records()
    assert len(all_recs) == 2
    rec_map = {r.record_id: r for r in all_recs}
    fetched_1 = rec_map["target_001"]
    assert fetched_1.title == "Finlandia op. 26"
    assert fetched_1.get_first_key("music_work") == "sibelius::finlandia::op26"
    assert fetched_1.get_keys("standard") == ["M-042-08955-4", "979-0-042-08955-4"]
    assert fetched_1.get_first_key("custom_archive") == "ark_12345/x987"
    assert fetched_1.serialized_work == "Sibelius Finlandia"
    assert rec_map["target_002"].get_first_key("music_work") == "sibelius::finlandia::op26"

    # 5. Query records by key
    by_music_key = c_repo.find_records_by_key("music_work", "sibelius::finlandia::op26")
    assert set(by_music_key) == {"target_001", "target_002"}

    by_archive_key = c_repo.find_records_by_key("custom_archive", "ark_12345/x987")
    assert by_archive_key == ["target_001"]

    # 6. SQL-level blocking groups
    groups = c_repo.get_blocking_groups(["music_work", "standard"])
    assert ("music_work", "sibelius::finlandia::op26") in groups
    assert set(groups[("music_work", "sibelius::finlandia::op26")]) == {"target_001", "target_002"}
    assert ("standard", "M-042-08955-4") in groups


def test_wemi_repository_agent_by_uri_concept_id_fallback(repo: WEMIRepository) -> None:
    from reconciliator.models import AgentEntity

    agent = AgentEntity(
        agent_uri="https://urn.fi/URN:NBN:fi:au:pn:000123456",
        name="Virtanen, Matti",
        name_normalized="virtanen, matti",
        source="kanto",
    )
    repo.insert_agents_batch([agent])

    # Direct URI match
    found = repo.get_agent_by_curie("https://urn.fi/URN:NBN:fi:au:pn:000123456")
    assert found is not None
    assert found.name == "Virtanen, Matti"

    # Concept ID fallback match
    found_cid = repo.get_agent_by_curie("kanto:000123456")
    assert found_cid is not None
    assert found_cid.id == found.id


def test_wemi_repository_wikidata_author_canonical_uri(repo: WEMIRepository) -> None:
    repo.insert_wikidata_authors(
        [
            {
                "qid": "Q42",
                "name": "Douglas Adams",
                "family_name": "Adams",
                "birth_year": 1952,
                "death_year": 2001,
            }
        ]
    )
    # Check lookup via URI and via concept id
    agent = repo.get_agent_by_curie("http://www.wikidata.org/entity/Q42")
    assert agent is not None
    assert agent.name == "Douglas Adams"
    assert agent.curie == "wikidata:Q42"

    agent_by_cid = repo.get_agent_by_curie("wikidata:Q42")
    assert agent_by_cid is not None
    assert agent_by_cid.id == agent.id


def test_wemi_repository_dynamic_author_identifiers_option3(repo: WEMIRepository) -> None:
    """Verify Option 3: wikidata_authors stores identifiers_json and WEMI identifiers table stores relational agent identifiers."""
    repo.insert_wikidata_authors(
        [
            {
                "qid": "Q42",
                "name": "Douglas Adams",
                "family_name": "Adams",
                "identifiers": {
                    "kanto": "000012345",
                    "isni": "0000 0001 2148 7654",
                    "viaf": "113230702",
                    "orcid": "0000-0002-1825-0097",
                },
            }
        ]
    )
    # 1. Verify wikidata_authors point lookup
    wa = repo.get_wikidata_author("Q42")
    assert wa is not None
    assert wa["name"] == "Douglas Adams"
    assert wa["identifiers"] == {
        "kanto": "000012345",
        "isni": "0000 0001 2148 7654",
        "viaf": "113230702",
        "orcid": "0000-0002-1825-0097",
    }

    # 2. Verify agent entity was created
    agent = repo.get_agent_by_curie("http://www.wikidata.org/entity/Q42")
    assert agent is not None

    # 3. Verify relational lookups in WEMI's identifiers table
    kanto_matches = repo.lookup_by_identifier("kanto", "000012345")
    assert len(kanto_matches) == 1
    assert kanto_matches[0] == ("agent", agent.id)

    orcid_matches = repo.lookup_by_identifier("orcid", "0000-0002-1825-0097")
    assert len(orcid_matches) == 1
    assert orcid_matches[0] == ("agent", agent.id)

    isni_matches = repo.lookup_by_identifier("isni", "0000000121487654")
    assert len(isni_matches) == 1
    assert isni_matches[0] == ("agent", agent.id)


# The AuthorityStore source_identifier_templates variant expansion is gone by
# design: WEMI stores canonical source_ids, so get_indexed_source_ids returns
# them as-is. Covered by test_wemi_repository_get_indexed_source_ids_variants.


def test_wemi_repository_intra_batch_duplicates_postgres(tmp_storage: Any) -> None:
    """Verify that batch inserts with duplicate keys in the same batch do not trigger
    psycopg.errors.CardinalityViolation on PostgreSQL ON CONFLICT DO UPDATE.
    """
    repo = WEMIRepository(
        db_path=tmp_storage.database_url,
        dimension=4,
    )
    # Duplicate agents in the same batch
    agent1 = AgentEntity(
        source="melinda",
        source_id="dup1",
        name="Author One",
        name_normalized="author one",
        surname="One",
    )
    agent2 = AgentEntity(
        source="melinda",
        source_id="dup1",
        name="Author One Updated",
        name_normalized="author one updated",
        surname="One",
    )
    ids = repo.insert_agents_batch([agent1, agent2])
    assert len(ids) == 2
    assert ids[0] == ids[1]
    assert ids[0] > 0
    fetched = repo.get_agent(ids[0])
    assert fetched is not None
    assert fetched.name == "Author One Updated"

    # Duplicate works in the same batch
    work1 = WorkEntity(
        source="melinda", source_id="w1", title="Title A", title_normalized="title a"
    )
    work2 = WorkEntity(
        source="melinda",
        source_id="w1",
        title="Title A Updated",
        title_normalized="title a updated",
    )
    w_ids = repo.insert_works_batch([work1, work2])
    assert len(w_ids) == 2
    assert w_ids[0] == w_ids[1]

    # Duplicate manifestations in the same batch
    manif1 = ManifestationEntity(
        source="melinda", source_id="m1", title="Title M", title_normalized="title m"
    )
    manif2 = ManifestationEntity(
        source="melinda",
        source_id="m1",
        title="Title M Updated",
        title_normalized="title m updated",
    )
    m_ids = repo.insert_manifestations_batch([manif1, manif2])
    assert len(m_ids) == 2
    assert m_ids[0] == m_ids[1]

    # Duplicate expressions
    expr1 = ExpressionEntity(source="melinda", source_id="e1", work_id=w_ids[0], language="fin")
    expr2 = ExpressionEntity(source="melinda", source_id="e1", work_id=w_ids[0], language="fin")
    e_ids = repo.insert_expressions_batch([expr1, expr2])
    assert len(e_ids) == 2
    assert e_ids[0] == e_ids[1]

    # Duplicate manifestation-expression links
    assoc1 = ManifestationExpressionAssociation(manifestation_id=m_ids[0], expression_id=e_ids[0])
    assoc2 = ManifestationExpressionAssociation(manifestation_id=m_ids[0], expression_id=e_ids[0])
    repo.insert_manifestation_expressions_batch([assoc1, assoc2])

    # Duplicate entity keys
    ek1 = EntityKey(entity_type="work", entity_id=w_ids[0], key_type="norm_title", key_value="KEY1")
    ek2 = EntityKey(entity_type="work", entity_id=w_ids[0], key_type="norm_title", key_value="KEY1")
    repo.insert_entity_keys_batch([ek1, ek2])

    # Duplicate wikidata authors
    repo.insert_wikidata_authors(
        [
            {"qid": "Q999", "name": "Wikidata Dup"},
            {"qid": "Q999", "name": "Wikidata Dup Updated"},
        ]
    )
    wa = repo.get_wikidata_author("Q999")
    assert wa is not None
    assert wa["name"] == "Wikidata Dup Updated"


def test_wemi_repository_agent_relational_attributes_and_identifiers(tmp_storage: Any) -> None:
    """Verify that agent attributes (qualifiers, affiliations, variant names) are stored relationally
    with additive count merging across batches, and identifiers are stored in identifiers_table.
    """
    from reconciliator.models import IdentifierEntity
    from reconciliator.repository import WEMIRepository

    repo = WEMIRepository(
        db_path=tmp_storage.database_url,
        dimension=4,
    )

    # Batch 1: Insert agent with initial qualifier, affiliation, variant name, and ISNI identifier
    agent1 = AgentEntity(
        source="asteri",
        source_id="12345",
        name="Waltari, Mika",
        name_normalized="waltari, mika",
        surname="Waltari",
        dates="1908-1979",
        qualifiers={"kirjailija": 1},
        affiliations={"Helsinki": 1},
        variant_names=["Waltari, M."],
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
    ids1 = repo.insert_agents_batch([agent1])
    assert len(ids1) == 1
    aid1 = ids1[0]
    assert aid1 > 0

    fetched1 = repo.get_agent(aid1)
    assert fetched1 is not None
    assert fetched1.qualifiers == {"kirjailija": 1}
    assert fetched1.affiliations == {"Helsinki": 1}
    assert "Waltari, M." in fetched1.variant_names
    assert len(fetched1.identifiers) == 1
    assert fetched1.identifiers[0].source == "isni"

    # Batch 2: Re-insert same agent with additional qualifier counts, a new qualifier, and a new variant name
    agent2 = AgentEntity(
        source="asteri",
        source_id="12345",
        name="Waltari, Mika",
        name_normalized="waltari, mika",
        surname="Waltari",
        dates="1908-1979",
        qualifiers={"kirjailija": 3, "toimittaja": 2},
        affiliations={"Helsinki": 2},
        variant_names=["Mika Toimi Waltari"],
        identifiers=[
            IdentifierEntity(
                entity_type="agent",
                entity_id=0,
                source="viaf",
                source_id="113230702",
                source_id_normalized="113230702",
            )
        ],
    )
    ids2 = repo.insert_agents_batch([agent2])
    assert ids2[0] == aid1

    fetched2 = repo.get_agent(aid1)
    assert fetched2 is not None
    # Additive merging across batches:
    # "kirjailija": 1 + 3 = 4
    # "toimittaja": 2
    # "Helsinki": 1 + 2 = 3
    assert fetched2.qualifiers["kirjailija"] == 4
    assert fetched2.qualifiers["toimittaja"] == 2
    assert fetched2.affiliations["Helsinki"] == 3
    assert "Waltari, M." in fetched2.variant_names
    assert "Mika Toimi Waltari" in fetched2.variant_names
    # Both identifiers should be linked
    ident_sources = {i.source: i.source_id for i in fetched2.identifiers}
    assert "isni" in ident_sources
    assert "viaf" in ident_sources

    # Test lookup via identifiers table
    matches = repo.lookup_by_identifier("isni", "0000000121487654")
    assert len(matches) == 1
    assert matches[0] == ("agent", aid1)
