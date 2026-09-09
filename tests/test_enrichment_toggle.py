"""Unit tests verifying pipeline enrichment mode (enrich=True vs enrich=False)."""

from lxml import etree
from reconciliator.loaders.marcxml import _get_subfields
from reconciliator.matcher import ReconciliationResult
from reconciliator.models import Agent, BibliographicRecord
from reconciliator.xml_enricher import MARCXMLEnricher


def test_enrichment_mode_injects_novel_only() -> None:
    # Record already has Asteri URN, candidate also has VIAF
    xml_str = """
    <record xmlns="http://www.loc.gov/MARC21/slim">
      <controlfield tag="001">001</controlfield>
      <datafield tag="100" ind1="1" ind2=" ">
        <subfield code="a">Waltari, Mika</subfield>
        <subfield code="0">https://urn.fi/URN:NBN:fi:au:pn:000123456</subfield>
      </datafield>
    </record>
    """
    elem = etree.fromstring(xml_str.strip())
    rec = BibliographicRecord(
        source_id="001",
        source="target",
        title="Sinuhe",
        agents=[
            Agent(
                name="Waltari, Mika",
                surname="Waltari",
                uri_subfield_0=["https://urn.fi/URN:NBN:fi:au:pn:000123456"],
            )
        ],
    )

    kanto_uri = "https://urn.fi/URN:NBN:fi:au:pn:000123456"
    viaf_uri = "http://viaf.org/viaf/111079447"

    res = ReconciliationResult(
        target_record=rec,
        selected_agent_uris={0: [kanto_uri, viaf_uri]},
    )

    enricher = MARCXMLEnricher()
    enriched_elem = enricher.enrich_record_tree(elem, res, enrich=True, fold_legacy=False)

    f100 = enriched_elem.findall(".//{http://www.loc.gov/MARC21/slim}datafield[@tag='100']")[0]
    u0 = _get_subfields(f100, "0")

    # Asteri URN must NOT be duplicated, VIAF should be added
    assert u0.count(kanto_uri) == 1
    assert viaf_uri in u0
    assert len(u0) == 2


def test_audit_only_mode_leaves_xml_untouched() -> None:
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
        selected_agent_uris={0: ["http://viaf.org/viaf/111079447"]},
    )

    enricher = MARCXMLEnricher()
    # enrich=False: pure audit mode
    enriched_elem = enricher.enrich_record_tree(elem, res, enrich=False, fold_legacy=False)

    f100 = enriched_elem.findall(".//{http://www.loc.gov/MARC21/slim}datafield[@tag='100']")[0]
    u0 = _get_subfields(f100, "0")
    assert len(u0) == 0

