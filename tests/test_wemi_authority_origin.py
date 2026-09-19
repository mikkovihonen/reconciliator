"""Tests for distinguishing authority records and target corpus records in WEMIRepository and MARCXML loaders."""

from lxml import etree

from reconciliator.loaders.marcxml import (
    decompose_marc_authority,
    is_marc_authority_record,
)
from reconciliator.reconciliation import WorkMatcher
from reconciliator.models import (
    AgentEntity,
    ExpressionEntity,
    IdentifierEntity,
    ItemEntity,
    ManifestationEntity,
    MatchRecord,
    WorkEntity,
)
from reconciliator.database import WEMIRepository


def test_record_origin_names_the_ingest_source():
    """record_origin is the source a row came from, not a two-valued literal.

    Authoritativeness is a property of the source (the `sources` table), so a
    corpus being reconciled is simply a source registered as non-authoritative.
    """
    w = WorkEntity(
        work_uri="urn:work:1",
        source="melinda",
        source_id="1",
        title="Test Work",
        title_normalized="test work",
        record_origin="melinda",
    )
    assert w.record_origin == "melinda"

    corpus_work = WorkEntity(
        work_uri="urn:work:2",
        source="my_corpus",
        source_id="2",
        title="Corpus Work",
        title_normalized="corpus work",
        record_origin="my_corpus",
    )
    assert corpus_work.record_origin == "my_corpus"

    # Every WEMI tier carries the same notion of origin.
    exp = ExpressionEntity(
        work_id=1,
        expression_uri="urn:exp:1",
        source="melinda",
        source_id="1",
        language="fin",
        title="Test Exp",
        record_origin="melinda",
    )
    manif = ManifestationEntity(
        work_id=1,
        manifestation_uri="urn:manif:1",
        source="my_corpus",
        source_id="1",
        title="Test Manif",
        title_normalized="test manif",
        record_origin="my_corpus",
    )
    item = ItemEntity(
        manifestation_id=1,
        item_uri="urn:item:1",
        source="koha",
        source_id="1",
        holding_organization="Koha Library",
        record_origin="koha",
    )
    agent = AgentEntity(
        agent_uri="urn:agent:1",
        source="melinda",
        source_id="1",
        name="Mika Waltari",
        name_normalized="mika waltari",
        record_origin="melinda",
    )
    assert [exp.record_origin, manif.record_origin, item.record_origin, agent.record_origin] == [
        "melinda",
        "my_corpus",
        "koha",
        "melinda",
    ]

    # An unregistered row is not silently treated as authoritative.
    assert (
        WorkEntity(
            work_uri="urn:work:3",
            source="x",
            source_id="3",
            title="T",
            title_normalized="t",
        ).record_origin
        == "unknown"
    )


def test_source_registry_decides_authoritativeness(tmp_path):
    """Authoritativeness lives in the sources table and drives match filtering."""
    repo = WEMIRepository(tmp_path / "sources.db")

    repo.register_source("melinda", is_authoritative=True)
    repo.register_source("fennica", is_authoritative=True)
    repo.register_source("my_corpus", is_authoritative=False)

    assert repo.get_sources() == {
        "melinda": True,
        "fennica": True,
        "my_corpus": False,
    }
    assert repo.authoritative_sources() == ["fennica", "melinda"]

    # Re-registering updates rather than duplicating.
    repo.register_source("fennica", is_authoritative=False)
    assert repo.authoritative_sources() == ["melinda"]
    assert set(repo.get_sources()) == {"melinda", "fennica", "my_corpus"}


def test_repository_origin_filtering_and_counts(tmp_path):
    """Test inserting authority vs target entities and filtering lookups/searches by origin."""
    db_path = tmp_path / "wemi_origin.db"
    repo = WEMIRepository(db_path)

    # 1. Insert authority work and expression
    w_auth = WorkEntity(
        work_uri="urn:work:auth:101",
        source="asteri",
        source_id="101",
        record_origin="authority",
        title="Sinuhe egyptiläinen",
        title_normalized="sinuhe egyptilainen",
        serialized_text="Sinuhe egyptiläinen Mika Waltari",
    )
    exp_auth = ExpressionEntity(
        work_id=0,
        expression_uri="urn:exp:auth:101",
        source="asteri",
        source_id="101",
        record_origin="authority",
        language="fin",
        title="Sinuhe egyptiläinen",
    )
    manif_auth = ManifestationEntity(
        work_id=0,
        manifestation_uri="urn:manif:auth:101",
        source="asteri",
        source_id="101",
        record_origin="authority",
        title="Sinuhe egyptiläinen",
        title_normalized="sinuhe egyptilainen",
    )
    id_auth = IdentifierEntity(
        entity_type="manifestation",
        entity_id=0,
        scheme="isbn",
        value="978-951-0-12345-6",
        value_normalized="9789510123456",
    )

    repo.insert_wemi_batch(
        works=[w_auth],
        expressions=[exp_auth],
        manifestations=[manif_auth],
        identifiers=[id_auth],
        record_origin="authority",
    )

    # 2. Insert target work, expression, and manifestation with the same title and identifier
    w_target = WorkEntity(
        work_uri="urn:work:target:202",
        source="target",
        source_id="202",
        record_origin="target",
        title="Sinuhe egyptiläinen",
        title_normalized="sinuhe egyptilainen",
        serialized_text="Sinuhe egyptiläinen Mika Waltari",
    )
    exp_target = ExpressionEntity(
        work_id=0,
        expression_uri="urn:exp:target:202",
        source="target",
        source_id="202",
        record_origin="target",
        language="fin",
        title="Sinuhe egyptiläinen",
    )
    manif_target = ManifestationEntity(
        work_id=0,
        manifestation_uri="urn:manif:target:202",
        source="target",
        source_id="202",
        record_origin="target",
        title="Sinuhe egyptiläinen",
        title_normalized="sinuhe egyptilainen",
    )
    id_target = IdentifierEntity(
        entity_type="manifestation",
        entity_id=0,
        scheme="isbn",
        value="978-951-0-12345-6",
        value_normalized="9789510123456",
    )

    repo.insert_wemi_batch(
        works=[w_target],
        expressions=[exp_target],
        manifestations=[manif_target],
        identifiers=[id_target],
        record_origin="target",
    )

    # Verify counts by origin
    counts = repo.count_records_by_origin()
    assert counts["works"]["authority"] == 1
    assert counts["works"]["target"] == 1
    assert counts["expressions"]["authority"] == 1
    assert counts["expressions"]["target"] == 1
    assert counts["manifestations"]["authority"] == 1
    assert counts["manifestations"]["target"] == 1

    # Test lookup_by_identifier with origin filter
    repo.register_source("authority", is_authoritative=True)
    repo.register_source("target", is_authoritative=False)

    auth_matches = repo.lookup_by_identifier("isbn", "978-951-0-12345-6", authoritative_only=True)
    assert len(auth_matches) == 1
    m_type, m_id = auth_matches[0]
    manif_retrieved = repo.get_manifestation(m_id)
    assert manif_retrieved.record_origin == "authority"
    assert manif_retrieved.source == "asteri"

    target_matches = [
        (etype, eid)
        for etype, eid in repo.lookup_by_identifier("isbn", "978-951-0-12345-6")
        if (etype, eid) not in auth_matches
    ]
    assert len(target_matches) == 1
    tm_type, tm_id = target_matches[0]
    target_retrieved = repo.get_manifestation(tm_id)
    assert target_retrieved.record_origin == "target"
    assert target_retrieved.source == "target"

    all_matches = repo.lookup_by_identifier("isbn", "978-951-0-12345-6")
    assert len(all_matches) == 2

    # Test FTS origin filtering
    fts_auth = repo.search_works_fts("Sinuhe", authoritative_only=True)
    assert len(fts_auth) == 1
    assert fts_auth[0][0].record_origin == "authority"

    fts_target = [r for r in repo.search_works_fts("Sinuhe") if r[0].record_origin == "target"]
    assert len(fts_target) == 1
    assert fts_target[0][0].record_origin == "target"

    fts_all = repo.search_works_fts("Sinuhe")
    assert len(fts_all) == 2

    # Test find_works_by_author_and_title default to authority
    default_works = repo.find_works_by_author_and_title(None, "sinuhe")
    assert len(default_works) == 1
    assert default_works[0].record_origin == "authority"

    target_works = [
        w
        for w in repo.find_works_by_author_and_title(None, "sinuhe", authoritative_only=False)
        if w.record_origin == "target"
    ]
    assert len(target_works) == 1
    assert target_works[0].record_origin == "target"

    both_works = repo.find_works_by_author_and_title(None, "sinuhe", authoritative_only=False)
    assert len(both_works) == 2


def test_is_marc_authority_record():
    """Test detection of MARC 21 authority records via leader and heuristic."""
    # Leader/06 == 'z'
    auth_xml = """<record xmlns="http://www.loc.gov/MARC21/slim">
      <leader>00000nz  a2200000n  4500</leader>
      <controlfield tag="001">12345</controlfield>
      <datafield tag="100" ind1="1" ind2=" ">
        <subfield code="a">Linna, Väinö,</subfield>
        <subfield code="d">1920-1992</subfield>
      </datafield>
    </record>"""
    elem_auth = etree.fromstring(auth_xml.encode("utf-8"))
    assert is_marc_authority_record(elem_auth) is True

    # Bibliographic record with Leader/06 == 'a' and 245
    bib_xml = """<record xmlns="http://www.loc.gov/MARC21/slim">
      <leader>00000cam a2200000   4500</leader>
      <controlfield tag="001">67890</controlfield>
      <datafield tag="245" ind1="1" ind2="0">
        <subfield code="a">Tuntematon sotilas /</subfield>
        <subfield code="c">Väinö Linna.</subfield>
      </datafield>
    </record>"""
    elem_bib = etree.fromstring(bib_xml.encode("utf-8"))
    assert is_marc_authority_record(elem_bib) is False

    # Record without leader but has 100 and no 245
    no_ldr_auth = """<record xmlns="http://www.loc.gov/MARC21/slim">
      <controlfield tag="001">999</controlfield>
      <datafield tag="100" ind1="1" ind2=" ">
        <subfield code="a">Leino, Eino</subfield>
      </datafield>
    </record>"""
    elem_no_ldr = etree.fromstring(no_ldr_auth.encode("utf-8"))
    assert is_marc_authority_record(elem_no_ldr) is True


def test_decompose_marc_authority_work():
    """Test decomposing a Work authority record (130 Uniform Title)."""
    xml = """<record xmlns="http://www.loc.gov/MARC21/slim">
      <leader>00000nz  a2200000n  4500</leader>
      <controlfield tag="001">auth_work_001</controlfield>
      <datafield tag="024" ind1="7" ind2=" ">
        <subfield code="a">http://urn.fi/URN:NBN:fi:au:work:001</subfield>
        <subfield code="2">urn</subfield>
      </datafield>
      <datafield tag="130" ind1=" " ind2="0">
        <subfield code="a">Kalevala</subfield>
      </datafield>
      <datafield tag="377" ind1=" " ind2=" ">
        <subfield code="a">fin</subfield>
      </datafield>
      <datafield tag="380" ind1=" " ind2=" ">
        <subfield code="a">Eepos</subfield>
      </datafield>
      <datafield tag="430" ind1=" " ind2="0">
        <subfield code="a">Vanha Kalevala</subfield>
      </datafield>
      <datafield tag="530" ind1=" " ind2="0">
        <subfield code="i">derivative of</subfield>
        <subfield code="a">Kanteletar</subfield>
      </datafield>
    </record>"""
    elem = etree.fromstring(xml.encode("utf-8"))
    subject_sink = []
    (
        works,
        expressions,
        manifestations,
        items,
        agents,
        relationships,
        identifiers,
        assocs,
        contribs,
    ) = decompose_marc_authority(
        elem, source="asteri", record_origin="authority", subject_sink=subject_sink
    )

    assert len(works) == 1
    w = works[0]
    assert w.title == "Kalevala"
    assert w.work_uri == "asteri:auth_work_001"
    assert w.record_origin == "authority"
    # 4XX variant titles ride in subject_sink as EntitySubject with relation_type='variant_title'
    variant_subjects = [s for s in subject_sink if s.relation_type == "variant_title"]
    assert any(s.raw_label == "Vanha Kalevala" for s in variant_subjects)

    assert len(expressions) == 1
    assert expressions[0].language == "fin"
    assert expressions[0].record_origin == "authority"

    assert len(manifestations) == 0
    assert len(relationships) == 1
    assert relationships[0].relationship_type == "adaptation_of"


def test_decompose_marc_authority_author_title():
    """Test decomposing a Work authority record with 100 Author/Title ($t)."""
    xml = """<record xmlns="http://www.loc.gov/MARC21/slim">
      <leader>00000nz  a2200000n  4500</leader>
      <controlfield tag="001">auth_at_002</controlfield>
      <datafield tag="100" ind1="1" ind2=" ">
        <subfield code="a">Waltari, Mika,</subfield>
        <subfield code="d">1908-1979.</subfield>
        <subfield code="t">Sinuhe egyptiläinen</subfield>
      </datafield>
      <datafield tag="377" ind1=" " ind2=" ">
        <subfield code="a">fin</subfield>
      </datafield>
    </record>"""
    elem = etree.fromstring(xml.encode("utf-8"))
    (
        works,
        expressions,
        manifestations,
        items,
        agents,
        relationships,
        identifiers,
        assocs,
        contribs,
    ) = decompose_marc_authority(elem, source="asteri")

    assert len(works) == 1
    assert works[0].title == "Sinuhe egyptiläinen"
    assert works[0].record_origin == "authority"

    assert len(agents) == 1
    assert agents[0].name == "Waltari, Mika"
    assert agents[0].birth_year == 1908
    assert agents[0].death_year == 1979
    assert agents[0].record_origin == "authority"

    assert len(contribs) == 1
    assert contribs[0].role == "aut"


def test_decompose_marc_authority_agent():
    """Test decomposing an Agent authority record (100 without $t)."""
    xml = """<record xmlns="http://www.loc.gov/MARC21/slim">
      <leader>00000nz  a2200000n  4500</leader>
      <controlfield tag="001">agent_003</controlfield>
      <datafield tag="024" ind1="7" ind2=" ">
        <subfield code="a">0000-0002-1825-0097</subfield>
        <subfield code="2">orcid</subfield>
      </datafield>
      <datafield tag="100" ind1="1" ind2=" ">
        <subfield code="a">Jansson, Tove,</subfield>
        <subfield code="d">1914-2001</subfield>
        <subfield code="q">Tove Marika</subfield>
      </datafield>
      <datafield tag="400" ind1="1" ind2=" ">
        <subfield code="a">Jansson, Tove Marika</subfield>
      </datafield>
      <datafield tag="373" ind1=" " ind2=" ">
        <subfield code="a">Svenska författarföreningen</subfield>
      </datafield>
    </record>"""
    elem = etree.fromstring(xml.encode("utf-8"))
    (
        works,
        expressions,
        manifestations,
        items,
        agents,
        relationships,
        identifiers,
        assocs,
        contribs,
    ) = decompose_marc_authority(elem, source="asteri")

    assert len(works) == 0
    assert len(agents) == 1
    ag = agents[0]
    assert ag.name == "Jansson, Tove"
    assert ag.surname == "Jansson"
    assert ag.birth_year == 1914
    assert ag.death_year == 2001
    assert any(i.source == "orcid" and i.source_id == "0000-0002-1825-0097" for i in identifiers)
    assert "Jansson, Tove Marika" in ag.variant_names
    assert ag.record_origin == "authority"





def test_work_matcher_authority_isolation(tmp_path):
    """Ensure WorkMatcher only resolves candidates with record_origin == 'authority'."""
    db_path = tmp_path / "matcher_isolation.db"
    repo = WEMIRepository(db_path)

    # Insert authoritative work & manifestation
    w_auth = WorkEntity(
        work_uri="urn:work:asteri:111",
        source="asteri",
        source_id="111",
        record_origin="authority",
        title="Puhdistus",
        title_normalized="puhdistus",
        work_type="text",
    )
    exp_auth = ExpressionEntity(
        work_id=0,
        expression_uri="urn:exp:asteri:111:fin",
        source="asteri",
        source_id="111",
        record_origin="authority",
        language="fin",
        title="Puhdistus",
    )
    manif_auth = ManifestationEntity(
        work_id=0,
        manifestation_uri="urn:manif:asteri:111",
        source="asteri",
        source_id="111",
        record_origin="authority",
        title="Puhdistus",
        title_normalized="puhdistus",
    )
    id_auth = IdentifierEntity(
        entity_type="manifestation",
        entity_id=0,
        scheme="isbn",
        value="978-951-0-34567-8",
        value_normalized="9789510345678",
    )
    repo.insert_wemi_batch(
        works=[w_auth],
        expressions=[exp_auth],
        manifestations=[manif_auth],
        identifiers=[id_auth],
        record_origin="authority",
    )

    # Insert a target record in the same WEMIRepository (e.g. from local library target)
    w_target = WorkEntity(
        work_uri="urn:work:local_koha:999",
        source="local_koha",
        source_id="999",
        record_origin="target",
        title="Puhdistus",
        title_normalized="puhdistus",
        work_type="text",
    )
    exp_target = ExpressionEntity(
        work_id=0,
        expression_uri="urn:exp:local_koha:999:fin",
        source="local_koha",
        source_id="999",
        record_origin="target",
        language="fin",
        title="Puhdistus",
    )
    manif_target = ManifestationEntity(
        work_id=0,
        manifestation_uri="urn:manif:local_koha:999",
        source="local_koha",
        source_id="999",
        record_origin="target",
        title="Puhdistus",
        title_normalized="puhdistus",
    )
    id_target = IdentifierEntity(
        entity_type="manifestation",
        entity_id=0,
        scheme="isbn",
        value="978-951-0-34567-8",
        value_normalized="9789510345678",
    )
    repo.insert_wemi_batch(
        works=[w_target],
        expressions=[exp_target],
        manifestations=[manif_target],
        identifiers=[id_target],
        record_origin="target",
    )

    matcher = WorkMatcher(wemi_repo=repo)

    # 1. Tier 1 fast path match
    target_rec = MatchRecord(
        record_id="target_001",
        title="Puhdistus",
        title_stem="puhdistus",
        author_key="oksanen",
        format="marcxml",
    )
    res = matcher.match_target(target_rec, identifiers={"isbn": ["978-951-0-34567-8"]})
    assert res is not None
    assert res.matched_work is not None
    # Must be the authority record, NEVER the target record!
    assert res.matched_work.record_origin == "authority"
    assert res.matched_work.source == "asteri"
    assert res.matched_manifestation.record_origin == "authority"

    # 2. Tier 2 match without ISBN (Author + Title stem)
    res_title = matcher.match_target(target_rec)
    assert res_title is not None
    assert res_title.matched_work.record_origin == "authority"
    assert res_title.matched_work.source == "asteri"
