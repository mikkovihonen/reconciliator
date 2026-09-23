"""Tests for Finto RDF authority decomposition (Minna Canth / Wilja, Petja Valasvaara / Vince)."""

import rdflib

from reconciliator.loaders.rdf_authority import decompose_rdf_authority, is_rdf_agent_authority
from reconciliator.loaders.registry import LoadContext, RdfAuthorityLoader, select_loader


def test_finto_minna_canth_wilja():
    # Minna Canth (000149842) and pseudonym Wilja (000149841)
    # Using real Finto/Asteri predicates: rdaa:P50316 (related person) and rdaa:P50395 (note with "Todellinen nimi:")
    rdf_turtle = """
    @prefix rdaa: <http://rdaregistry.info/Elements/a/> .
    @prefix rdac: <http://rdaregistry.info/Elements/c/> .
    @prefix skos: <http://www.w3.org/2004/02/skos/core#> .

    <http://urn.fi/URN:NBN:fi:au:pn:000149841> a rdac:C10004 ;
        skos:prefLabel "Wilja" ;
        rdaa:P50121 "1844" ;
        rdaa:P50120 "1897" ;
        rdaa:P50316 <http://urn.fi/URN:NBN:fi:au:pn:000149842> ;
        rdaa:P50395 "Todellinen nimi: Minna Canth." .

    <http://urn.fi/URN:NBN:fi:au:pn:000149842> skos:prefLabel "Canth, Minna" .
    """
    g = rdflib.Graph()
    g.parse(data=rdf_turtle, format="turtle")

    assert is_rdf_agent_authority(g) is True

    record = decompose_rdf_authority(g, source="finaf")
    assert len(record.agents) == 1
    agent = record.agents[0]

    assert agent.is_personal is True
    assert agent.name == "Wilja"
    assert agent.birth_year == 1844
    assert agent.death_year == 1897

    # Verify that relationship type was refined by the note "Todellinen nimi:" to real_identity_of
    assert len(record.agent_relationships) >= 1
    rel = record.agent_relationships[0]
    assert rel.relationship_type == "real_identity_of"
    assert rel.target_agent_curie == "http://urn.fi/URN:NBN:fi:au:pn:000149842"
    assert rel.target_name == "Canth, Minna"


def test_finto_loader_registry_dispatch():
    rdf_turtle = """
    @prefix rdaa: <http://rdaregistry.info/Elements/a/> .
    @prefix rdac: <http://rdaregistry.info/Elements/c/> .
    @prefix skos: <http://www.w3.org/2004/02/skos/core#> .

    <http://urn.fi/URN:NBN:fi:au:pn:000191839> a rdac:C10004 ;
        skos:prefLabel "Vince (ohjelmoija)" ;
        rdaa:P50095 <http://urn.fi/URN:NBN:fi:au:pn:000178043> .

    <http://urn.fi/URN:NBN:fi:au:pn:000178043> skos:prefLabel "Advanced Art (yhtye)" .
    """
    g = rdflib.Graph()
    g.parse(data=rdf_turtle, format="turtle")

    loader = select_loader(g)
    assert isinstance(loader, RdfAuthorityLoader)

    ctx = LoadContext(source="finaf")
    rec = loader.load(g, ctx)

    assert len(rec.agents) == 1
    agent = rec.agents[0]
    assert agent.name == "Vince"
    assert agent.qualifiers == ["ohjelmoija"]

    # Verify corporate body membership
    member_rels = [r for r in rec.agent_relationships if r.relationship_type == "member_of"]
    assert len(member_rels) == 1
    assert member_rels[0].target_name == "Advanced Art (yhtye)"
