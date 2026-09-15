"""Exhaustive tests for BIBFRAME and BFFI Work-Expression-Manifestation-Item (WEMI) decomposition.

Verifies:
- 6-condition Graph Decomposition Decision Matrix (C-1 through C-6)
- bffi:marcKey provenance and component extraction
- Construction of MatchRecord from BIBFRAME graphs
- End-to-end insertion into WEMIRepository with FK binding
"""

from __future__ import annotations

from pathlib import Path

import pytest
from rdflib import Graph

from reconciliator.loaders.bibframe import (
    build_target_record_from_bibframe,
    decompose_bibframe_work,
    extract_marc_key_components,
    get_profile,
)
from reconciliator.models import MatchRecord
from reconciliator.repository import WEMIRepository

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture
def test_repo(tmp_path: Path) -> WEMIRepository:
    db_path = tmp_path / "test_wemi_bibframe.db"
    return WEMIRepository(db_path=db_path)


def test_extract_marc_key_components():
    """Verify bffi:marcKey provenance extraction regex and component parsing."""
    mk1 = "1001 $aWaltari, Mika,$d1908-1979,$ewriter"
    comp1 = extract_marc_key_components(mk1)
    assert comp1 is not None
    tag, ind, sub = comp1
    assert tag == "100"
    assert ind == "1 "
    assert sub == "$aWaltari, Mika,$d1908-1979,$ewriter"

    mk2 = "24510$aTuntematon sotilas / $cVäinö Linna."
    comp2 = extract_marc_key_components(mk2)
    assert comp2 is not None
    tag2, ind2, sub2 = comp2
    assert tag2 == "245"
    assert ind2 == "10"
    assert sub2 == "$aTuntematon sotilas / $cVäinö Linna."

    mk3 = "70012$aMinaudier, Jean-Pierre,$d1961-,$ekääntäjä"
    comp3 = extract_marc_key_components(mk3)
    assert comp3 is not None
    tag3, ind3, sub3 = comp3
    assert tag3 == "700"
    assert ind3 == "12"

    assert extract_marc_key_components("invalid_key") is None
    assert extract_marc_key_components("") is None


def test_condition_c1_bffi_4tier_graph():
    """Condition C-1: Finnish BFFI 1.0 explicit 4-tier direct extraction."""
    ttl_path = FIXTURES_DIR / "sample_bffi_natlibfi.ttl"
    g = Graph()
    g.parse(str(ttl_path), format="turtle")

    profile = get_profile("bffi_1")
    (
        work,
        expression,
        manifestation,
        items,
        agents,
        relationships,
        identifiers,
        associations,
        mediums,
        contributions,
    ) = decompose_bibframe_work(g, profile=profile, source="melinda")

    # Work
    assert work.source_id == "http://urn.fi/URN:NBN:fi:bib:work:9912345678"
    assert work.title == "Sinuhe egyptiläinen"
    assert work.source == "melinda"
    assert "Sinuhe egyptiläinen" in work.serialized_text

    # Expression
    assert expression.source_id == "http://urn.fi/URN:NBN:fi:bib:expression:9912345678"
    assert expression.language == "fin"
    assert "Sinuhe egyptiläinen" in expression.serialized_text

    # Manifestation
    assert manifestation.source_id == "http://urn.fi/URN:NBN:fi:bib:instance:9912345678"
    assert manifestation.publication_year == 1945
    assert manifestation.publisher == "WSOY"
    assert manifestation.publication_place == "Helsinki"

    # Identifiers
    assert len(identifiers) == 1
    assert identifiers[0].scheme == "isbn"
    assert identifiers[0].value == "978-951-0-00001-0"

    # Agents & Contributions
    assert len(agents) == 1
    assert agents[0].name == "Waltari, Mika"
    assert len(contributions) == 1
    assert contributions[0].role == "aut"
    assert contributions[0].is_primary is True
    assert contributions[0].field_tag == "100"

    # Association
    assert len(associations) == 1
    assert associations[0].is_primary is True


def test_condition_c2_bffi_missing_expression_synthesizes_default():
    """Condition C-2: Finnish BFFI 1.0 missing expression synthesizes default expression."""
    turtle_data = """
    @prefix bffi: <http://urn.fi/URN:NBN:fi:schema:bffi:> .
    @prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .
    @prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .

    <http://urn.fi/URN:NBN:fi:bib:instance:111> a bffi:Manifestation ;
        rdfs:label "Kalevala" ;
        bffi:expressionManifested <http://urn.fi/URN:NBN:fi:bib:work:111> ;
        bffi:provisionActivity [
            a bffi:Publication ;
            bffi:date "1849"
        ] .

    <http://urn.fi/URN:NBN:fi:bib:work:111> a bffi:Work ;
        rdfs:label "Kalevala" ;
        bffi:languageOfExpression "fin" .
    """
    g = Graph()
    g.parse(data=turtle_data, format="turtle")

    profile = get_profile("bffi_1")
    work, expression, manifestation, *rest = decompose_bibframe_work(
        g, profile=profile, source="melinda"
    )

    assert work.source_id == "http://urn.fi/URN:NBN:fi:bib:work:111"
    # Synthesized default expression ID ending in #expr-orig
    assert expression.source_id == "http://urn.fi/URN:NBN:fi:bib:work:111#expr-orig"
    assert expression.title == "Kalevala"
    assert expression.language == "fin"
    assert manifestation.source_id == "http://urn.fi/URN:NBN:fi:bib:instance:111"


def test_condition_c3_lc_has_hub_hoisting():
    """Condition C-3: LC BIBFRAME 3.0 Hub hoisting into canonical Work."""
    turtle_data = """
    @prefix bf: <http://id.loc.gov/ontologies/bibframe/> .
    @prefix bflc: <http://id.loc.gov/ontologies/bflc/> .
    @prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .
    @prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .

    <http://id.loc.gov/resources/instances/inst-1> a bf:Instance ;
        rdfs:label "Hamlet (Arden Edition)" ;
        bf:instanceOf <http://id.loc.gov/resources/works/work-1> .

    <http://id.loc.gov/resources/works/work-1> a bf:Work ;
        rdfs:label "Hamlet (Arden Edition)" ;
        bf:language <http://id.loc.gov/vocabulary/languages/eng> ;
        bflc:hasHub <http://id.loc.gov/resources/hubs/hub-shakespeare-hamlet> .

    <http://id.loc.gov/resources/hubs/hub-shakespeare-hamlet> a bf:Hub ;
        rdfs:label "Hamlet" .
    """
    g = Graph()
    g.parse(data=turtle_data, format="turtle")

    work, expression, manifestation, items, agents, rels, *rest = decompose_bibframe_work(
        g, source="lc"
    )

    # Hub is canonical Work
    assert work.source_id == "http://id.loc.gov/resources/hubs/hub-shakespeare-hamlet"
    assert work.title == "Hamlet"
    assert work.work_type == "hub"

    # Specific work node acts as Expression realizing the Hub
    assert expression.source_id == "http://id.loc.gov/resources/works/work-1"
    assert expression.title == "Hamlet (Arden Edition)"
    assert expression.language == "eng"
    assert manifestation.source_id == "http://id.loc.gov/resources/instances/inst-1"


def test_condition_c4_translation_of_decomposition():
    """Condition C-4: LC BIBFRAME 2.0 / 3.0 translation decomposition into Work + translation Expression."""
    turtle_data = """
    @prefix bf: <http://id.loc.gov/ontologies/bibframe/> .
    @prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .
    @prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .

    <http://id.loc.gov/resources/instances/inst-tr> a bf:Instance ;
        rdfs:label "The Egyptian" ;
        bf:instanceOf <http://id.loc.gov/resources/works/work-tr> .

    <http://id.loc.gov/resources/works/work-tr> a bf:Work ;
        rdfs:label "The Egyptian" ;
        bf:language <http://id.loc.gov/vocabulary/languages/eng> ;
        bf:translationOf <http://urn.fi/URN:NBN:fi:bib:work:original-sinuhe> .

    <http://urn.fi/URN:NBN:fi:bib:work:original-sinuhe> a bf:Work ;
        rdfs:label "Sinuhe egyptiläinen" ;
        bf:language <http://id.loc.gov/vocabulary/languages/fin> .
    """
    g = Graph()
    g.parse(data=turtle_data, format="turtle")

    work, expression, manifestation, items, agents, rels, *rest = decompose_bibframe_work(
        g, source="lc"
    )

    # Original work node is the canonical Work
    assert work.source_id == "http://urn.fi/URN:NBN:fi:bib:work:original-sinuhe"
    assert work.title == "Sinuhe egyptiläinen"
    assert work.original_language == "fin"

    # Translation work node acts as Expression
    assert expression.source_id == "http://id.loc.gov/resources/works/work-tr"
    assert expression.title == "The Egyptian"
    assert expression.language == "eng"

    # Work relationship
    assert len(rels) == 1
    assert rels[0].relationship_type == "translation_of"


def test_condition_c5_pmo_performance_realization():
    """Condition C-5: Performed Music Ontology Performance realization and mediums of performance."""
    turtle_data = """
    @prefix bf: <http://id.loc.gov/ontologies/bibframe/> .
    @prefix pmo: <http://performedmusicontology.org/ontology/> .
    @prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .
    @prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .

    <http://example.org/inst/disc1> a bf:Instance ;
        rdfs:label "Sibelius: Violin Concerto / Chung, Previn" .

    <http://example.org/perf/1970-chung> a pmo:Performance ;
        rdfs:label "Performance of Sibelius Violin Concerto by Kyung Wha Chung" ;
        pmo:performedWork <http://example.org/work/violin-concerto> .

    <http://example.org/work/violin-concerto> a bf:Work ;
        rdfs:label "Violin Concerto in D minor, op. 47" ;
        pmo:mediumOfPerformance [
            a pmo:MediumOfPerformance ;
            rdfs:label "violin" ;
            pmo:playerCount "1"
        ], [
            a pmo:MediumOfPerformance ;
            rdfs:label "orchestra" ;
            pmo:playerCount "1"
        ] .
    """
    g = Graph()
    g.parse(data=turtle_data, format="turtle")

    (
        work,
        expression,
        manifestation,
        items,
        agents,
        rels,
        idents,
        assocs,
        mediums,
        contribs,
    ) = decompose_bibframe_work(g, source="music_source")

    # Musical Work
    assert work.source_id == "http://example.org/work/violin-concerto"
    assert work.title == "Violin Concerto in D minor, op. 47"
    assert work.work_type == "music"

    # Audio Performance Expression
    assert expression.source_id == "http://example.org/perf/1970-chung"
    assert expression.content_type == "audio_music"

    # Mediums of performance
    assert len(mediums) == 2
    labels = {m.label for m in mediums}
    assert "violin" in labels
    assert "orchestra" in labels


def test_condition_c6_single_work_partitioning():
    """Condition C-6: Standard LC BIBFRAME 2.0 single bf:Work partitioned into Work + Expression."""
    ttl_path = FIXTURES_DIR / "sample_bibframe_lc.ttl"
    g = Graph()
    g.parse(str(ttl_path), format="turtle")

    (
        work,
        expression,
        manifestation,
        items,
        agents,
        relationships,
        identifiers,
        associations,
        mediums,
        contributions,
    ) = decompose_bibframe_work(g, source="lc")

    work_node_uri = "http://id.loc.gov/resources/works/c0284759"
    assert work.source_id == work_node_uri
    assert work.title == "Symphony No. 5 in E-flat major, op. 82"

    # Partitioned expression ID ending in #expr-1
    assert expression.source_id == f"{work_node_uri}#expr-1"
    assert expression.title == work.title

    # Manifestation
    assert manifestation.source_id == "http://id.loc.gov/resources/instances/c0284759"
    assert manifestation.publication_year == 1919
    assert manifestation.publisher == "Hansen"

    # Mediums of performance from bf:mediumOfPerformance
    assert len(mediums) == 1
    assert mediums[0].label == "orkesteri"

    # Agent & Contribution
    assert len(agents) == 1
    assert agents[0].name == "Sibelius, Jean"
    assert len(contributions) == 1
    assert contributions[0].role == "cmp"


def test_build_target_record_from_bibframe():
    """Verify build_target_record_from_bibframe for staging MatchRecord in corpus."""
    ttl_path = FIXTURES_DIR / "sample_bffi_natlibfi.ttl"
    g = Graph()
    g.parse(str(ttl_path), format="turtle")

    target_rec = build_target_record_from_bibframe(g, source="melinda")
    assert isinstance(target_rec, MatchRecord)
    assert target_rec.record_id == "http://urn.fi/URN:NBN:fi:bib:instance:9912345678"
    assert target_rec.format == "bibframe"
    assert target_rec.title == "Sinuhe egyptiläinen"
    assert target_rec.language == "fin"
    assert target_rec.year == 1945
    assert target_rec.get_first_key("standard") == "978-951-0-00001-0"
    assert target_rec.author_key is not None
    assert target_rec.title_stem is not None
    assert target_rec.work_blocking_key is not None
    assert target_rec.serialized_work is not None
    assert target_rec.serialized_expression is not None
    assert target_rec.serialized_manifestation is not None
    assert len(target_rec.raw_payload) > 0


def test_repository_insert_bibframe_decomposition(test_repo: WEMIRepository):
    """Verify complete end-to-end insertion of decomposed BIBFRAME entities into WEMIRepository."""
    ttl_path = FIXTURES_DIR / "sample_bffi_natlibfi.ttl"
    g = Graph()
    g.parse(str(ttl_path), format="turtle")

    (
        work,
        expression,
        manifestation,
        items,
        agents,
        relationships,
        identifiers,
        associations,
        mediums,
        contributions,
    ) = decompose_bibframe_work(g, profile=get_profile("bffi_1"), source="melinda")

    # Insert batch with automatic FK binding
    test_repo.insert_wemi_batch(
        works=[work],
        expressions=[expression],
        manifestations=[manifestation],
        items=items,
        agents=agents,
        relationships=relationships,
        identifiers=identifiers,
        manifestation_expressions=associations,
        mediums_of_performance=mediums,
        contributions=contributions,
    )

    # Verify lookups
    db_work = test_repo.get_work(work.id)
    assert db_work is not None
    assert db_work.title == "Sinuhe egyptiläinen"

    db_expr = test_repo.get_expression(expression.id)
    assert db_expr is not None
    assert db_expr.language == "fin"
    assert db_expr.work_id == db_work.id

    db_manif = test_repo.get_manifestation(manifestation.id)
    assert db_manif is not None
    assert db_manif.publication_year == 1945
    assert db_manif.work_id == db_work.id

    # Verify associations
    db_exprs = test_repo.get_expressions_for_manifestation(db_manif.id)
    assert len(db_exprs) == 1
    assert db_exprs[0].id == db_expr.id

    # Verify contributions
    db_contribs = test_repo.get_contributions_for_entity("work", db_work.id)
    assert len(db_contribs) == 1
    assert db_contribs[0].role == "aut"
    assert db_contribs[0].is_primary is True
    assert db_contribs[0].field_tag == "100"
