"""Tests for relator code injection, relator term canonicalization, and bridge file overrides."""

from __future__ import annotations

from pathlib import Path
from lxml import etree

from reconciliator.config import EnrichmentConfig
from reconciliator.normalizers.relators import RelatorRegistry
from reconciliator.xml_enricher import MARCXMLEnricher


def test_relator_registry_canonical_and_formats() -> None:
    registry = RelatorRegistry()

    # 1. Preferred term lookup
    assert registry.get_canonical_term("act", lang="fi") == "näyttelijä"
    assert registry.get_canonical_term("act", lang="sv") == "skådespelare"
    assert registry.get_canonical_term("act", lang="en") == "actor agent"
    assert registry.get_canonical_term("act", role_format="code") == "actor"
    assert registry.get_canonical_term("act", role_format="loc_uri") == "actor"
    assert registry.get_canonical_term("aut", role_format="code") == "author"

    # 2. Term normalization with punctuation and abbreviations
    canon_lbl, code = registry.normalize_term_to_canonical("näyttelijä.", lang="fi")
    assert code == "act"
    assert canon_lbl == "näyttelijä"

    canon_lbl_code, code_c = registry.normalize_term_to_canonical(
        "näyttelijä.", lang="fi", role_format="code"
    )
    assert code_c == "act"
    assert canon_lbl_code == "actor"

    canon_lbl_abbr, code_abbr = registry.normalize_term_to_canonical("näytt.", lang="fi")
    assert code_abbr == "act"
    assert canon_lbl_abbr == "näyttelijä"

    canon_lbl_sv, code_sv = registry.normalize_term_to_canonical("skådesp.", lang="sv")
    assert code_sv == "act"
    assert canon_lbl_sv == "skådespelare"

    # 3. Format resolutions
    assert (
        registry.resolve_relator_identifier("act", format_mode="bridged_uri")
        == "http://urn.fi/URN:NBN:fi:au:mts:m1241"
    )
    assert registry.resolve_relator_identifier("act", format_mode="code") == "act"
    assert (
        registry.resolve_relator_identifier("act", format_mode="loc_uri")
        == "http://id.loc.gov/vocabulary/relators/act"
    )


def test_relator_registry_bridge_override(tmp_path: Path) -> None:
    custom_ttl = tmp_path / "custom-relators-bridge.ttl"
    custom_ttl.write_text(
        """@prefix loc: <http://id.loc.gov/vocabulary/relators/> .
@prefix cust: <http://example.org/roles/> .
@prefix skos: <http://www.w3.org/2004/02/skos/core#> .

loc:act skos:exactMatch cust:performer_role ;
        skos:prefLabel "esiintyjä"@fi, "performer agent"@en ;
        skos:altLabel "esiint."@fi ;
        skos:notation "act" .
""",
        encoding="utf-8",
    )

    registry = RelatorRegistry(bridge_file=custom_ttl)
    assert (
        registry.resolve_relator_identifier("act", format_mode="bridged_uri")
        == "http://example.org/roles/performer_role"
    )
    lbl, code = registry.normalize_term_to_canonical("esiint.", lang="fi")
    assert code == "act"
    assert lbl == "esiintyjä"
    assert registry.get_canonical_term("act", role_format="code") == "performer"


def test_xml_enricher_normalizes_and_injects_relator() -> None:
    raw_xml = """<record xmlns="http://www.loc.gov/MARC21/slim">
  <controlfield tag="001">rec1</controlfield>
  <datafield tag="041" ind1=" " ind2=" ">
    <subfield code="a">fin</subfield>
  </datafield>
  <datafield tag="700" ind1="1" ind2=" ">
    <subfield code="a">Eaton, Shirley,</subfield>
    <subfield code="d">1937-</subfield>
    <subfield code="e">näyttelijä.</subfield>
  </datafield>
</record>"""
    root = etree.fromstring(raw_xml.encode("utf-8"))

    enricher = MARCXMLEnricher()
    cfg = EnrichmentConfig(
        inject_role_codes=True,
        normalize_role_terms=True,
        role_format="bridged_uri",
    )
    enriched = enricher.enrich_record_tree(root, enrichment_config=cfg)

    f700 = enriched.find(".//{http://www.loc.gov/MARC21/slim}datafield[@tag='700']")
    sub_e = f700.find("{http://www.loc.gov/MARC21/slim}subfield[@code='e']")
    sub_4 = f700.find("{http://www.loc.gov/MARC21/slim}subfield[@code='4']")

    assert sub_e is not None
    assert sub_e.text == "näyttelijä"

    assert sub_4 is not None
    assert sub_4.text == "http://urn.fi/URN:NBN:fi:au:mts:m1241"


def test_xml_enricher_abbreviation_and_conference_field() -> None:
    raw_xml = """<record>
  <datafield tag="041" ind1=" " ind2=" ">
    <subfield code="a">swe</subfield>
  </datafield>
  <datafield tag="700" ind1="1" ind2=" ">
    <subfield code="a">Person, Test</subfield>
    <subfield code="e">skådesp.</subfield>
  </datafield>
  <datafield tag="111" ind1="2" ind2=" ">
    <subfield code="a">Meeting, Annual</subfield>
    <subfield code="j">esitt.</subfield>
  </datafield>
</record>"""
    root = etree.fromstring(raw_xml.encode("utf-8"))

    enricher = MARCXMLEnricher()
    cfg = EnrichmentConfig(
        inject_role_codes=True,
        normalize_role_terms=True,
        role_format="bridged_uri",
    )
    enriched = enricher.enrich_record_tree(root, enrichment_config=cfg)

    # 700 with Swedish record language
    f700 = enriched.find(".//datafield[@tag='700']")
    sub_e = f700.find("subfield[@code='e']")
    sub_4 = f700.find("subfield[@code='4']")
    assert sub_e.text == "skådespelare"
    assert sub_4.text == "http://urn.fi/URN:NBN:fi:au:mts:m1241"

    # 111 with $j in Swedish record
    f111 = enriched.find(".//datafield[@tag='111']")
    sub_j = f111.find("subfield[@code='j']")
    sub_4_111 = f111.find("subfield[@code='4']")
    assert sub_j.text == "framförare"
    assert sub_4_111.text == "http://urn.fi/URN:NBN:fi:au:mts:m4115"


def test_xml_enricher_code_and_loc_uri_formats() -> None:
    raw_xml = """<record>
  <datafield tag="100" ind1="1" ind2=" ">
    <subfield code="a">Author, A.</subfield>
    <subfield code="e">kirj.</subfield>
  </datafield>
</record>"""

    enricher = MARCXMLEnricher()

    # Test code format: $e becomes standard LoC relator term ("author") and $4 is 3-letter code
    root_code = etree.fromstring(raw_xml.encode("utf-8"))
    cfg_code = EnrichmentConfig(
        inject_role_codes=True,
        normalize_role_terms=True,
        role_format="code",
    )
    res_code = enricher.enrich_record_tree(root_code, enrichment_config=cfg_code)
    f100_code = res_code.find(".//datafield[@tag='100']")
    assert f100_code.find("subfield[@code='e']").text == "author"
    assert f100_code.find("subfield[@code='4']").text == "aut"

    # Test loc_uri format: $e becomes standard LoC relator term ("author") and $4 is LoC URI
    root_loc = etree.fromstring(raw_xml.encode("utf-8"))
    cfg_loc = EnrichmentConfig(
        inject_role_codes=True,
        normalize_role_terms=True,
        role_format="loc_uri",
    )
    res_loc = enricher.enrich_record_tree(root_loc, enrichment_config=cfg_loc)
    f100_loc = res_loc.find(".//datafield[@tag='100']")
    assert f100_loc.find("subfield[@code='e']").text == "author"
    assert f100_loc.find("subfield[@code='4']").text == "http://id.loc.gov/vocabulary/relators/aut"


def test_xml_enricher_modernizes_existing_4_and_injects_missing_term() -> None:
    raw_xml = """<record>
  <datafield tag="700" ind1="1" ind2=" ">
    <subfield code="a">Musician, M.</subfield>
    <subfield code="4">cmp</subfield>
  </datafield>
</record>"""
    root = etree.fromstring(raw_xml.encode("utf-8"))

    enricher = MARCXMLEnricher()
    cfg = EnrichmentConfig(
        inject_role_codes=True,
        normalize_role_terms=True,
        role_format="bridged_uri",
        inject_missing_role_terms=True,
    )
    enriched = enricher.enrich_record_tree(root, enrichment_config=cfg)

    f700 = enriched.find(".//datafield[@tag='700']")
    sub_e = f700.find("subfield[@code='e']")
    sub_4 = f700.find("subfield[@code='4']")

    assert sub_e is not None
    assert sub_e.text == "säveltäjä"

    assert sub_4 is not None
    assert sub_4.text == "http://urn.fi/URN:NBN:fi:au:mts:m695"
