"""Tests for multi-format output enrichment of aggregates:
- MARCXML 730/700 enrichment with $0 URIs from constituent linkages
- BFFI RDF enrichment with owl:sameAs on bffi:hasPart nodes and bffi:marcKey $0 update
- Verification that BFFI enrichment introduces zero bflc: triples
"""

import pytest
from lxml import etree
from rdflib import Graph, Literal, Namespace, RDF, URIRef

from reconciliator.xml_enricher import MARCXMLEnricher
from reconciliator.loaders.bibframe.enricher import enrich_bibframe_graph
from reconciliator.loaders.bibframe.profiles import get_profile
from reconciliator.models import CorpusConstituentLinkage, CorpusLinkage


BFFI = Namespace("http://urn.fi/URN:NBN:fi:schema:bffi:")
BF = Namespace("http://id.loc.gov/ontologies/bibframe/")
OWL = Namespace("http://www.w3.org/2002/07/owl#")


def test_marcxml_constituent_enrichment():
    """Verify that MARCXMLEnricher injects $0 into matching 730 and 700 constituent fields."""
    xml_data = """<record xmlns="http://www.loc.gov/MARC21/slim">
  <leader>00000nam a2200000 i 4500</leader>
  <controlfield tag="001">12345</controlfield>
  <datafield tag="245" ind1="1" ind2="0">
    <subfield code="a">Suuri laulukirja</subfield>
  </datafield>
  <datafield tag="730" ind1="0" ind2="2">
    <subfield code="a">Laula, tyttö!</subfield>
    <subfield code="g">Merikanto, Oskar</subfield>
  </datafield>
  <datafield tag="700" ind1="1" ind2="2">
    <subfield code="a">Merikanto, Oskar,</subfield>
    <subfield code="d">1868-1924.</subfield>
    <subfield code="t">Oi muistatko vielä sen virren</subfield>
  </datafield>
</record>"""

    tree = etree.fromstring(xml_data.encode("utf-8"))
    enricher = MARCXMLEnricher()

    c_links = [
        CorpusConstituentLinkage(
            id=1,
            record_id="rec_1",
            constituent_ordinal=1,
            constituent_work_id="cw_1",
            title="Laula, tyttö!",
            field_tag="730",
            final_work_uri="http://urn.fi/URN:NBN:fi:au:pn:song1",
        ),
        CorpusConstituentLinkage(
            id=2,
            record_id="rec_1",
            constituent_ordinal=2,
            constituent_work_id="cw_2",
            title="Oi muistatko vielä sen virren",
            field_tag="700",
            final_work_uri="http://urn.fi/URN:NBN:fi:au:pn:song2",
        ),
    ]

    enricher.enrich_record_tree(
        record_elem=tree,
        result=None,
        enrich=True,
        constituent_linkages=c_links,
    )

    # Verify 730 has $0
    f730 = tree.find(".//{http://www.loc.gov/MARC21/slim}datafield[@tag='730']")
    if f730 is None:
        f730 = tree.find(".//datafield[@tag='730']")
    assert f730 is not None
    sf0_730 = [sf.text for sf in f730.findall(".//{http://www.loc.gov/MARC21/slim}subfield[@code='0']") or f730.findall(".//subfield[@code='0']")]
    assert "http://urn.fi/URN:NBN:fi:au:pn:song1" in sf0_730

    # Verify 700 has $0
    f700 = tree.find(".//{http://www.loc.gov/MARC21/slim}datafield[@tag='700']")
    if f700 is None:
        f700 = tree.find(".//datafield[@tag='700']")
    assert f700 is not None
    sf0_700 = [sf.text for sf in f700.findall(".//{http://www.loc.gov/MARC21/slim}subfield[@code='0']") or f700.findall(".//subfield[@code='0']")]
    assert "http://urn.fi/URN:NBN:fi:au:pn:song2" in sf0_700


def test_bffi_rdf_constituent_enrichment_without_bflc():
    """Verify that BFFI graphs are enriched with owl:sameAs and bffi:marcKey updates with no bflc: triples."""
    profile = get_profile("bffi_1")

    g = Graph()
    g.bind("bffi", BFFI)
    g.bind("bf", BF)
    g.bind("owl", OWL)

    manifestation_node = URIRef("http://urn.fi/URN:NBN:fi:bib:manif:alb1")
    work_node = URIRef("http://urn.fi/URN:NBN:fi:bib:work:alb1")
    expr_node = URIRef("http://urn.fi/URN:NBN:fi:bib:expr:alb1")

    g.add((manifestation_node, RDF.type, BFFI.CollectionManifestation))
    g.add((manifestation_node, BFFI.workManifested, work_node))
    g.add((work_node, RDF.type, BFFI.AggregatingWork))

    # Add constituent parts
    part1 = URIRef("http://urn.fi/URN:NBN:fi:bib:work:part1")
    t1 = URIRef("http://urn.fi/URN:NBN:fi:bib:title:part1")
    g.add((work_node, BFFI.hasPart, part1))
    g.add((part1, RDF.type, BFFI.Work))
    g.add((part1, BFFI.title, t1))
    g.add((t1, RDF.type, BFFI.Title))
    g.add((t1, BFFI.mainTitle, Literal("Kalliolle kukkulalle")))
    g.add((part1, BFFI.marcKey, Literal("7300 $aKalliolle kukkulalle")))

    c_links = [
        CorpusConstituentLinkage(
            id=1,
            record_id="natlibfi/alb1",
            constituent_ordinal=1,
            constituent_work_id="cw_part1",
            title="Kalliolle kukkulalle",
            field_tag="bffi:hasPart",
            final_work_uri="http://urn.fi/URN:NBN:fi:au:pn:kallio",
        )
    ]

    enrich_bibframe_graph(
        g=g,
        nodes=(manifestation_node, expr_node, work_node),
        profile=profile,
        enrich=True,
        constituent_linkages=c_links,
    )

    # 1. Verify owl:sameAs triple added
    target_same_as = URIRef("http://urn.fi/URN:NBN:fi:au:pn:kallio")
    assert (part1, OWL.sameAs, target_same_as) in g

    # 2. Verify bffi:marcKey updated with $0
    mk_vals = [str(o) for o in g.objects(part1, BFFI.marcKey)]
    assert len(mk_vals) == 1
    assert "$0http://urn.fi/URN:NBN:fi:au:pn:kallio" in mk_vals[0]

    # 3. Verify STRICTLY NO bflc: namespace anywhere in the graph
    for s, p, o in g:
        assert "bflc" not in str(s)
        assert "bflc" not in str(p)
        assert "bflc" not in str(o)

