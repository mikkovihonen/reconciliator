"""Unit tests for WEMIRepository batch operations, navigation, FTS, and vector search."""

from pathlib import Path

import numpy as np
import pytest

from reconciliator.indexer import TorchVectorEngine
from reconciliator.models import (
    AgentEntity,
    ContributionEntity,
    ExpressionEntity,
    HarvestState,
    IdentifierEntity,
    ItemEntity,
    ManifestationEntity,
    ManifestationExpressionAssociation,
    MediumOfPerformance,
    WorkEntity,
    WorkRelationship,
    TargetRecord,
)
from reconciliator.repository import CorpusRepository, WEMIRepository


@pytest.fixture
def repo(tmp_path: Path) -> WEMIRepository:
    db_path = tmp_path / "authority.db"
    w_idx = TorchVectorEngine(tmp_path / "work.pt", dimension=4)
    e_idx = TorchVectorEngine(tmp_path / "exp.pt", dimension=4)
    m_idx = TorchVectorEngine(tmp_path / "manif.pt", dimension=4)
    return WEMIRepository(
        db_path=db_path,
        dimension=4,
        work_indexer=w_idx,
        expression_indexer=e_idx,
        manifestation_indexer=m_idx,
    )


def test_insert_and_get_agents(repo: WEMIRepository):
    agent = AgentEntity(
        agent_uri="http://kanto.fi/agent/1",
        source="asteri",
        wikidata_qid="Q1234",
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
    assert fetched.wikidata_qid == "Q1234"

    fetched_by_uri = repo.get_agent_by_uri("http://kanto.fi/agent/1")
    assert fetched_by_uri is not None
    assert fetched_by_uri.id == agent_id


def test_insert_and_get_works_with_vectors_and_fts(repo: WEMIRepository):
    works = [
        WorkEntity(
            work_uri="urn:work:sinuhe",
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
            work_uri="urn:work:turms",
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
    assert "muinainen egypti" in w1.subjects_json

    w_by_uri = repo.get_work_by_uri("urn:work:turms")
    assert w_by_uri is not None
    assert w_by_uri.id == work_ids[1]

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

    # Medium of Performance
    repo.insert_medium_of_performance_batch(
        [
            MediumOfPerformance(
                work_id=w1_id,
                medium_uri="http://rdaregistry.info/termList/mop/violin",
                label="violin",
                player_count=1,
                is_soloist=True,
            )
        ]
    )


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
    """Verify CorpusRepository staging with normalized keys, detached payloads, and SQL blocking queries."""
    corpus_db = tmp_path / "reconcile_corpus.db"
    c_repo = CorpusRepository(corpus_db)

    # 1. Create TargetRecord with multi-valued and custom keys
    t1 = TargetRecord(
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

    t2 = TargetRecord(
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
    c_repo.insert_target_records_batch([t1, t2])
    assert c_repo.count_target_records() == 2

    # 3. Retrieve single record and verify reconstructed keys and payload
    fetched_1 = c_repo.get_target_record("target_001")
    assert fetched_1 is not None
    assert fetched_1.title == "Finlandia op. 26"
    assert fetched_1.music_work_key == "sibelius::finlandia::op26"
    assert fetched_1.get_keys("standard") == ["M-042-08955-4", "979-0-042-08955-4"]
    assert fetched_1.get_first_key("custom_archive") == "ark_12345/x987"
    assert b"Finlandia" in fetched_1.raw_payload
    assert fetched_1.serialized_work == "Sibelius Finlandia"

    # 4. Retrieve all records
    all_recs = c_repo.get_all_target_records()
    assert len(all_recs) == 2
    rec_map = {r.record_id: r for r in all_recs}
    assert rec_map["target_002"].music_work_key == "sibelius::finlandia::op26"

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
    found = repo.get_agent_by_uri("https://urn.fi/URN:NBN:fi:au:pn:000123456")
    assert found is not None
    assert found.name == "Virtanen, Matti"

    # Concept ID fallback match
    found_cid = repo.get_agent_by_uri("kanto:000123456")
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
    agent = repo.get_agent_by_uri("http://www.wikidata.org/entity/Q42")
    assert agent is not None
    assert agent.name == "Douglas Adams"
    assert agent.agent_uri == "http://www.wikidata.org/entity/Q42"

    agent_by_cid = repo.get_agent_by_uri("wikidata:Q42")
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
    agent = repo.get_agent_by_uri("http://www.wikidata.org/entity/Q42")
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


def test_authority_store_configurable_source_templates(tmp_path: Path) -> None:
    from reconciliator.repository import AuthorityStore
    from reconciliator.models import BibliographicRecord

    db_path = tmp_path / "custom_auth.db"
    idx_path = tmp_path / "custom_auth.index"
    store = AuthorityStore(
        db_path=db_path,
        index_path=idx_path,
        dimension=8,
        source_identifier_templates={"custom_lib": "urn:custom_lib:{id}"},
    )
    rec = BibliographicRecord(source="custom_lib", source_id="001234", title="Custom Book")
    vecs = np.zeros((1, 8), dtype=np.float32)
    store.insert_records_batch([rec], vectors=vecs)

    indexed = store.get_indexed_source_ids("custom_lib")
    assert "001234" in indexed
    assert "1234" in indexed
    assert "urn:custom_lib:001234" in indexed
    assert "urn:custom_lib:1234" in indexed


