"""Unit tests for MARCXMLEnricher tree mutation and enrichment toggles."""

from lxml import etree

from reconciliator.loaders.marcxml import _get_subfields
from reconciliator.matcher import ReconciliationResult
from reconciliator.models import Agent, BibliographicRecord
from reconciliator.xml_enricher import MARCXMLEnricher


def test_enrich_agent_uris() -> None:
    xml_str = """
    <record xmlns="http://www.loc.gov/MARC21/slim">
      <controlfield tag="001">001</controlfield>
      <datafield tag="100" ind1="1" ind2=" ">
        <subfield code="a">Waltari, Mika</subfield>
      </datafield>
    </record>
    """
    elem = etree.fromstring(xml_str.strip())
    rec = BibliographicRecord(
        source_id="001",
        source="target",
        title="Sinuhe",
        agents=[Agent(name="Waltari, Mika", surname="Waltari")],
    )

    kanto_uri = "https://urn.fi/URN:NBN:fi:au:pn:000123456"
    wikidata_uri = "http://www.wikidata.org/entity/Q273115"

    res = ReconciliationResult(
        target_record=rec,
        selected_agent_uris={0: [kanto_uri, wikidata_uri]},
    )

    enricher = MARCXMLEnricher()
    enriched_elem = enricher.enrich_record_tree(elem, res, enrich=True, fold_legacy=False)

    f100 = enriched_elem.findall(".//{http://www.loc.gov/MARC21/slim}datafield[@tag='100']")[0]
    u0 = _get_subfields(f100, "0")
    u1 = _get_subfields(f100, "1")

    assert kanto_uri in u0
    assert wikidata_uri in u1


def test_enrich_disabled_does_not_mutate() -> None:
    xml_str = """
    <record xmlns="http://www.loc.gov/MARC21/slim">
      <controlfield tag="001">001</controlfield>
      <datafield tag="100" ind1="1" ind2=" ">
        <subfield code="a">Waltari, Mika</subfield>
      </datafield>
    </record>
    """
    elem = etree.fromstring(xml_str.strip())
    rec = BibliographicRecord(
        source_id="001",
        source="target",
        title="Sinuhe",
        agents=[Agent(name="Waltari, Mika", surname="Waltari")],
    )

    res = ReconciliationResult(
        target_record=rec,
        selected_agent_uris={0: ["https://urn.fi/URN:NBN:fi:au:pn:000123456"]},
    )

    enricher = MARCXMLEnricher()
    enriched_elem = enricher.enrich_record_tree(elem, res, enrich=False, fold_legacy=False)

    f100 = enriched_elem.findall(".//{http://www.loc.gov/MARC21/slim}datafield[@tag='100']")[0]
    u0 = _get_subfields(f100, "0")
    assert len(u0) == 0
