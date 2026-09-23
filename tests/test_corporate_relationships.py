"""Tests for corporate body relationships in MARC and RDF authorities."""

from lxml import etree
import rdflib

from reconciliator.loaders.marcxml import decompose_marc_authority
from reconciliator.loaders.rdf_authority import decompose_rdf_authority
from reconciliator.models import AgentRelationshipEntity


def test_marc_corporate_body_relationships():
    # Corporate body A. Ahlström Karhulan tekniset with predecessor and successor
    xml = """
    <record xmlns="http://www.loc.gov/MARC21/slim">
        <leader>00000cz  a2200000n  4500</leader>
        <controlfield tag="001">000032485</controlfield>
        <datafield tag="110" ind1="2" ind2=" ">
            <subfield code="a">A. Ahlström Karhulan tekniset</subfield>
            <subfield code="0">http://urn.fi/URN:NBN:fi:au:pn:000032485</subfield>
        </datafield>
        <datafield tag="368" ind1=" " ind2=" ">
            <subfield code="a">yritys</subfield>
        </datafield>
        <datafield tag="510" ind1="2" ind2=" ">
            <subfield code="w">a</subfield>
            <subfield code="a">Karhula Osakeyhtiö</subfield>
            <subfield code="0">http://urn.fi/URN:NBN:fi:au:pn:000032484</subfield>
        </datafield>
        <datafield tag="510" ind1="2" ind2=" ">
            <subfield code="w">b</subfield>
            <subfield code="a">Ahlstrom Karhula Oy</subfield>
            <subfield code="0">http://urn.fi/URN:NBN:fi:au:pn:000032486</subfield>
        </datafield>
        <datafield tag="510" ind1="2" ind2=" ">
            <subfield code="i">Emoyhteisö:</subfield>
            <subfield code="a">A. Ahlström Osakeyhtiö</subfield>
            <subfield code="0">http://urn.fi/URN:NBN:fi:au:pn:000032480</subfield>
        </datafield>
    </record>
    """
    elem = etree.fromstring(xml.encode("utf-8"))
    agent_rels: list[AgentRelationshipEntity] = []

    res = decompose_marc_authority(
        elem,
        source="kanto",
        agent_relationship_sink=agent_rels,
    )
    agents = res[4]
    assert len(agents) == 1
    corp = agents[0]

    assert not corp.is_personal
    assert corp.name == "A. Ahlström Karhulan tekniset"
    assert "yritys" in corp.qualifiers

    # Check 510 parsed relationships
    rel_map = {r.target_name: r.relationship_type for r in agent_rels}
    assert rel_map.get("Karhula Osakeyhtiö") == "predecessor_of"
    assert rel_map.get("Ahlstrom Karhula Oy") == "successor_of"
    assert rel_map.get("A. Ahlström Osakeyhtiö") == "hierarchical_superior_of"


def test_rdf_corporate_body_relationships():
    rdf_turtle = """
    @prefix rdaa: <http://rdaregistry.info/Elements/a/> .
    @prefix rdac: <http://rdaregistry.info/Elements/c/> .
    @prefix skos: <http://www.w3.org/2004/02/skos/core#> .
    @prefix edtf: <http://id.loc.gov/datatypes/edtf/> .

    <http://urn.fi/URN:NBN:fi:au:pn:000032485> a rdac:C10005 ;
        skos:prefLabel "A. Ahlström Karhulan tekniset (yritys)" ;
        rdaa:P50025 "Karhulan tekniset" ;
        rdaa:P50004 "yritys" ;
        rdaa:P50236 "1915/1998"^^edtf:EDTF ;
        rdaa:P50012 <http://urn.fi/URN:NBN:fi:au:pn:000032484> ;
        rdaa:P50016 <http://urn.fi/URN:NBN:fi:au:pn:000032486> ;
        rdaa:P50007 <http://urn.fi/URN:NBN:fi:au:pn:000032480> .

    <http://urn.fi/URN:NBN:fi:au:pn:000032484> skos:prefLabel "Karhula Osakeyhtiö" .
    <http://urn.fi/URN:NBN:fi:au:pn:000032486> skos:prefLabel "Ahlstrom Karhula Oy" .
    <http://urn.fi/URN:NBN:fi:au:pn:000032480> skos:prefLabel "A. Ahlström Osakeyhtiö" .
    """
    g = rdflib.Graph()
    g.parse(data=rdf_turtle, format="turtle")

    record = decompose_rdf_authority(g, source="finaf")
    assert len(record.agents) == 1
    agent = record.agents[0]

    assert not agent.is_personal
    assert agent.name == "A. Ahlström Karhulan tekniset"
    assert "yritys" in agent.qualifiers
    assert agent.birth_year == 1915
    assert agent.death_year == 1998
    assert "Karhulan tekniset" in agent.variant_names

    rel_types = {r.target_agent_curie: r.relationship_type for r in record.agent_relationships}
    assert rel_types.get("http://urn.fi/URN:NBN:fi:au:pn:000032484") == "predecessor_of"
    assert rel_types.get("http://urn.fi/URN:NBN:fi:au:pn:000032486") == "successor_of"
    assert rel_types.get("http://urn.fi/URN:NBN:fi:au:pn:000032480") == "hierarchical_superior_of"

