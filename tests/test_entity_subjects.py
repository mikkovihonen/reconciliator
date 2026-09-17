"""Tests for polymorphic entity subjects persisted in `entity_subjects` table."""

from pathlib import Path

import pytest
from lxml import etree

from reconciliator.indexer import TorchVectorEngine
from reconciliator.loaders.marcxml import (
    decompose_marc_authority,
    decompose_marc_wemi,
)
from reconciliator.models import (
    AgentEntity,
    EntitySubject,
    ExpressionEntity,
    ManifestationEntity,
    VocabConcept,
    WorkEntity,
)
from reconciliator.repository import WEMIRepository
from reconciliator.vocab_store import VocabStore

MARC_NS = "http://www.loc.gov/MARC21/slim"


@pytest.fixture
def repo(tmp_path: Path) -> WEMIRepository:
    db_path = tmp_path / "authority.db"
    v_store = VocabStore(db_path)
    v_store.insert_concepts_batch(
        [
            VocabConcept(
                uri="http://www.yso.fi/onto/yso/p1234",
                vocabulary="yso",
                pref_labels={"fi": "historia", "sv": "historia", "en": "history"},
            ),
            VocabConcept(
                uri="http://www.yso.fi/onto/yso/p5678",
                vocabulary="yso",
                pref_labels={"fi": "kirjailijat", "sv": "författare", "en": "writers"},
            ),
            VocabConcept(
                uri="http://urn.fi/URN:NBN:fi:au:slm:g100",
                vocabulary="slm",
                pref_labels={"fi": "romaanit"},
            ),
            VocabConcept(
                uri="http://www.yso.fi/onto/yso/p9999",
                vocabulary="yso",
                pref_labels={"fi": "muu aihe"},
            ),
        ]
    )
    return WEMIRepository(
        db_path=db_path,
        dimension=4,
        work_indexer=TorchVectorEngine(tmp_path / "work.pt", dimension=4),
        manifestation_indexer=TorchVectorEngine(tmp_path / "manif.pt", dimension=4),
    )


@pytest.fixture
def vocab_store(repo: WEMIRepository) -> VocabStore:
    return VocabStore(repo.db_path)


def test_insert_and_retrieve_entity_subjects(repo: WEMIRepository):
    work = WorkEntity(
        work_uri="urn:work:test:1",
        title="Test Work",
        title_normalized="test work",
        source="test",
        source_id="1",
    )
    work_id = repo.insert_work(work)

    subjects = [
        EntitySubject(
            entity_type="work",
            entity_id=work_id,
            source="melinda",
            source_record_id="rec-1",
            relation_type="topic",
            vocabulary="yso",
            concept_id="p1234",
            raw_label="historia",
            is_resolved=True,
            is_primary=True,
        ),
        EntitySubject(
            entity_type="work",
            entity_id=work_id,
            source="melinda",
            source_record_id="rec-1",
            relation_type="genre_form",
            vocabulary="slm",
            concept_id="g100",
            raw_label="romaanit",
            is_resolved=True,
            is_primary=False,
        ),
        EntitySubject(
            entity_type="work",
            entity_id=work_id,
            source="melinda",
            source_record_id="rec-1",
            relation_type="topic",
            vocabulary=None,
            concept_id=None,
            raw_label="tuntematon aihe",
            is_resolved=False,
            is_primary=False,
        ),
    ]

    ids = repo.insert_entity_subjects_batch(subjects)
    assert len(ids) == 3
    assert all(i > 0 for i in ids)

    retrieved = repo.get_subjects_for_entity("work", work_id)
    assert len(retrieved) == 3
    # Primary subject should come first due to ORDER BY is_primary DESC
    assert retrieved[0].is_primary is True
    assert retrieved[0].concept_id == "p1234"
    assert retrieved[0].relation_type == "topic"

    unresolved = [s for s in retrieved if not s.is_resolved]
    assert len(unresolved) == 1
    assert unresolved[0].raw_label == "tuntematon aihe"


def test_get_entities_by_concept(repo: WEMIRepository):
    work1 = WorkEntity(
        work_uri="urn:work:test:10",
        title="W1",
        title_normalized="w1",
        source="test",
        source_id="10",
    )
    work2 = WorkEntity(
        work_uri="urn:work:test:20",
        title="W2",
        title_normalized="w2",
        source="test",
        source_id="20",
    )
    w1_id = repo.insert_work(work1)
    w2_id = repo.insert_work(work2)

    repo.insert_entity_subjects_batch(
        [
            EntitySubject(
                entity_type="work",
                entity_id=w1_id,
                relation_type="topic",
                vocabulary="yso",
                concept_id="p1234",
                is_resolved=True,
            ),
            EntitySubject(
                entity_type="work",
                entity_id=w2_id,
                relation_type="topic",
                vocabulary="yso",
                concept_id="p1234",
                is_resolved=True,
            ),
            EntitySubject(
                entity_type="work",
                entity_id=w2_id,
                relation_type="topic",
                vocabulary="yso",
                concept_id="p9999",
                is_resolved=True,
            ),
        ]
    )

    rows = repo.get_entities_by_concept("yso", "p1234")
    assert len(rows) == 2
    entity_ids = {r["entity_id"] for r in rows}
    assert entity_ids == {w1_id, w2_id}

    filtered_rows = repo.get_entities_by_concept("yso", "p1234", entity_type="agent")
    assert len(filtered_rows) == 0


def test_insert_wemi_batch_binds_entity_subjects(repo: WEMIRepository):
    work = WorkEntity(
        work_uri="urn:work:test:w1",
        title="Work 1",
        title_normalized="work 1",
        source="test",
        source_id="w1",
    )
    expr = ExpressionEntity(
        work_id=0,
        expression_uri="urn:exp:test:e1",
        work_uri="urn:work:test:w1",
        language="fin",
        source="test",
        source_id="e1",
    )
    manif = ManifestationEntity(
        manifestation_uri="urn:manif:test:m1",
        work_uri="urn:work:test:w1",
        title="Manif 1",
        title_normalized="manif 1",
        source="test",
        source_id="m1",
    )
    agent = AgentEntity(
        agent_uri="urn:agent:test:a1",
        name="Waltari, Mika",
        name_normalized="waltari, mika",
        source="test",
        source_id="a1",
    )

    subjects = [
        EntitySubject(
            entity_type="work",
            entity_uri="urn:work:test:w1",
            relation_type="topic",
            vocabulary="yso",
            concept_id="p1234",
            raw_label="historia",
            is_resolved=True,
        ),
        EntitySubject(
            entity_type="agent",
            entity_uri="urn:agent:test:a1",
            relation_type="occupation",
            vocabulary="yso",
            concept_id="p5678",
            raw_label="kirjailijat",
            is_resolved=True,
        ),
    ]

    repo.insert_wemi_batch(
        works=[work],
        expressions=[expr],
        manifestations=[manif],
        agents=[agent],
        entity_subjects=subjects,
    )

    work_subjects = repo.get_subjects_for_entity("work", work.id)
    assert len(work_subjects) == 1
    assert work_subjects[0].concept_id == "p1234"
    assert work_subjects[0].relation_type == "topic"

    agent_subjects = repo.get_subjects_for_agent(agent.id)
    assert len(agent_subjects) == 1
    assert agent_subjects[0].concept_id == "p5678"
    assert agent_subjects[0].relation_type == "occupation"


def test_marcxml_decompose_extracts_entity_subjects(vocab_store: VocabStore):
    xml = f"""<record xmlns="{MARC_NS}">
        <controlfield tag="001">rec-100</controlfield>
        <datafield tag="100" ind1="1" ind2=" "><subfield code="a">Waltari, Mika</subfield></datafield>
        <datafield tag="245" ind1="1" ind2="0"><subfield code="a">Sinuhe egyptiläinen</subfield></datafield>
        <datafield tag="650" ind1=" " ind2="7">
            <subfield code="a">historia</subfield>
            <subfield code="2">yso</subfield>
            <subfield code="0">http://www.yso.fi/onto/yso/p1234</subfield>
        </datafield>
        <datafield tag="651" ind1=" " ind2="7">
            <subfield code="a">Egypti</subfield>
            <subfield code="2">yso-paikat</subfield>
        </datafield>
        <datafield tag="655" ind1=" " ind2="7">
            <subfield code="a">historialliset romaanit</subfield>
            <subfield code="2">slm</subfield>
        </datafield>
        <datafield tag="600" ind1="1" ind2="0">
            <subfield code="a">Akhnaton,</subfield>
            <subfield code="d">faarao</subfield>
            <subfield code="0">http://www.yso.fi/onto/koko/p8888</subfield>
        </datafield>
    </record>""".encode()

    record_elem = etree.fromstring(xml)
    subjs: list[EntitySubject] = []

    work, expr, manif, items, agents, rels, idents, assoc, mops, contribs = decompose_marc_wemi(
        record_elem,
        source="melinda",
        subject_sink=subjs,
        vocab_store=vocab_store,
    )

    # Verify subjects extracted
    assert len(subjs) >= 3
    rel_types = {s.relation_type for s in subjs}
    assert "topic" in rel_types
    assert "geographic" in rel_types
    assert "genre_form" in rel_types

    # Pre-resolved subject p1234
    yso_subj = next(s for s in subjs if s.concept_id == "p1234")
    assert yso_subj.is_resolved is True
    assert yso_subj.vocabulary == "yso"
    assert yso_subj.entity_type == "work"

    # Verify 600 personal name subject became an agent contribution with role='subject'
    subject_contribs = [c for c in contribs if c.role == "subject"]
    assert len(subject_contribs) == 1
    assert subject_contribs[0].field_tag == "600"
    assert any("Akhnaton" in a.name for a in agents)


def test_marc_authority_extracts_agent_subjects():
    xml = f"""<record xmlns="{MARC_NS}">
        <leader>00000cz  a2200000n  4500</leader>
        <controlfield tag="001">auth-agent-1</controlfield>
        <datafield tag="100" ind1="1" ind2=" ">
            <subfield code="a">Waltari, Mika,</subfield>
            <subfield code="d">1908-1979</subfield>
            <subfield code="0">http://urn.fi/URN:NBN:fi:au:pn:000045678</subfield>
        </datafield>
        <datafield tag="372" ind1=" " ind2=" ">
            <subfield code="a">kirjallisuus</subfield>
            <subfield code="2">yso</subfield>
            <subfield code="0">http://www.yso.fi/onto/yso/p1234</subfield>
        </datafield>
        <datafield tag="374" ind1=" " ind2=" ">
            <subfield code="a">kirjailijat</subfield>
            <subfield code="2">yso</subfield>
            <subfield code="0">http://www.yso.fi/onto/yso/p5678</subfield>
        </datafield>
        <datafield tag="370" ind1=" " ind2=" ">
            <subfield code="c">Helsinki</subfield>
        </datafield>
    </record>""".encode()

    record_elem = etree.fromstring(xml)
    subjs: list[EntitySubject] = []

    works, exprs, manifs, items, agents, rels, idents, assoc, mops, contribs = (
        decompose_marc_authority(
            record_elem,
            source="authority",
            subject_sink=subjs,
        )
    )

    assert len(agents) == 1
    assert len(subjs) == 3

    foa = next(s for s in subjs if s.relation_type == "field_of_activity")
    assert foa.concept_id == "p1234"
    assert foa.entity_type == "agent"

    occ = next(s for s in subjs if s.relation_type == "occupation")
    assert occ.concept_id == "p5678"
    assert occ.entity_type == "agent"

    place = next(s for s in subjs if s.relation_type == "associated_place")
    assert place.raw_label == "Helsinki"
    assert place.entity_type == "agent"
