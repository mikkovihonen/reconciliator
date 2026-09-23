"""Unit tests for Asteri Authority separation and Teka (asteri_work) authority support."""

from typing import Any
from unittest.mock import MagicMock

from lxml import etree

from reconciliator.clustering.agent import AgentClusterer
from reconciliator.config import AgentClusterConfig
from reconciliator.loaders.marcxml import (
    build_target_record_from_marc,
    decompose_marc_wemi,
    parse_marc_record_elem,
)
from reconciliator.models import MatchRecord
from reconciliator.reconciliation.matcher import WorkMatcher
from reconciliator.utils.curie import clear_curie_cache, parse_curie


def setup_function() -> None:
    clear_curie_cache()


def test_parse_curie_asteri_agent_and_work() -> None:
    # Agent authorities (Kanto / Asteri person & corporate & legacy Aleph FIN11)
    assert parse_curie("(FI-ASTERI-N)000041539") == ("kanto", "000041539")
    assert parse_curie("(FI-ASTERI-C)000012345") == ("kanto", "000012345")
    assert parse_curie("(FIN11)000041539") == ("kanto", "000041539")
    assert parse_curie("http://urn.fi/URN:NBN:fi:au:pn:000041539") == ("kanto", "000041539")

    # Work authorities (Teka / Asteri work & legacy Aleph FIN13)
    assert parse_curie("(FI-ASTERI-W)000015266") == ("asteri_work", "000015266")
    assert parse_curie("(FIN13)000015266") == ("asteri_work", "000015266")
    assert parse_curie("https://melinda.kansalliskirjasto.fi/teka/byid/(FI-ASTERI-W)000015266") == (
        "asteri_work",
        "000015266",
    )
    assert parse_curie("https://urn.fi/URN:NBN:fi:au:teka:000015266") == (
        "asteri_work",
        "000015266",
    )

    # Bibliographic carrier (Melinda / FIN01) - strictly bibliographic, not authority
    assert parse_curie("(FI-MELINDA)012345678") == ("melinda", "012345678")
    assert parse_curie("(FIN01)012345678") == ("melinda", "012345678")


def test_marcxml_loader_extracts_asteri_work_240() -> None:
    xml = """
    <record xmlns="http://www.loc.gov/MARC21/slim">
      <controlfield tag="001">991234567</controlfield>
      <datafield tag="100" ind1="1" ind2=" ">
        <subfield code="a">Waltari, Mika,</subfield>
        <subfield code="d">1908-1979.</subfield>
        <subfield code="0">(FI-ASTERI-N)000041539</subfield>
      </datafield>
      <datafield tag="240" ind1="1" ind2="0">
        <subfield code="a">Sinuhe egyptiläinen.</subfield>
        <subfield code="l">englanti</subfield>
        <subfield code="0">(FI-ASTERI-W)000015266</subfield>
      </datafield>
      <datafield tag="245" ind1="1" ind2="4">
        <subfield code="a">The Egyptian /</subfield>
        <subfield code="c">Mika Waltari.</subfield>
      </datafield>
    </record>
    """
    elem = etree.fromstring(xml.encode("utf-8"))

    # 1. Test parse_marc_record_elem
    parsed = parse_marc_record_elem(elem)
    assert "000015266" in parsed["identifiers"]["asteri_work"]
    assert "000015266" in parsed["work_identifiers"]["asteri_work"]

    # 2. Test decompose_marc_wemi
    (
        work,
        expression,
        manifestation,
        items,
        agents,
        relationships,
        identifiers,
        associations,
        contributions,
    ) = decompose_marc_wemi(elem, source="fennica")

    work_id_entities = [
        ident
        for ident in identifiers
        if ident.entity_type == "work" and ident.source == "asteri_work"
    ]
    assert len(work_id_entities) == 1
    assert work_id_entities[0].source_id == "000015266"

    # 3. Test build_target_record_from_marc
    target_rec = build_target_record_from_marc(elem, source="target")
    assert "asteri_work" in target_rec.identifiers
    assert "000015266" in target_rec.identifiers["asteri_work"]


def test_marcxml_loader_extracts_asteri_work_130_and_name_title() -> None:
    xml_130 = """
    <record xmlns="http://www.loc.gov/MARC21/slim">
      <controlfield tag="001">991234568</controlfield>
      <datafield tag="130" ind1="0" ind2=" ">
        <subfield code="a">Kalevala.</subfield>
        <subfield code="0">https://melinda.kansalliskirjasto.fi/teka/byid/(FI-ASTERI-W)000001234</subfield>
      </datafield>
      <datafield tag="245" ind1="1" ind2="0">
        <subfield code="a">Kalevala</subfield>
      </datafield>
    </record>
    """
    elem_130 = etree.fromstring(xml_130.encode("utf-8"))
    parsed_130 = parse_marc_record_elem(elem_130)
    assert "000001234" in parsed_130["identifiers"]["asteri_work"]

    xml_700_t = """
    <record xmlns="http://www.loc.gov/MARC21/slim">
      <controlfield tag="001">991234569</controlfield>
      <datafield tag="245" ind1="0" ind2="0">
        <subfield code="a">Selected Works</subfield>
      </datafield>
      <datafield tag="700" ind1="1" ind2="2">
        <subfield code="a">Waltari, Mika,</subfield>
        <subfield code="t">Sinuhe egyptiläinen</subfield>
        <subfield code="0">(FIN13)000015266</subfield>
      </datafield>
    </record>
    """
    elem_700 = etree.fromstring(xml_700_t.encode("utf-8"))
    parsed_700 = parse_marc_record_elem(elem_700)
    assert "000015266" in parsed_700["identifiers"]["asteri_work"]


def test_authority_rank_penalizes_melinda() -> None:
    mock_repo = MagicMock()
    config = AgentClusterConfig(
        source_authority_precedence=["asteri", "kanto", "isni", "viaf", "wikidata"]
    )
    clusterer = AgentClusterer(mock_repo, config)

    agent_asteri: dict[str, Any] = {"source": "asteri", "source_id": "1"}
    agent_kanto: dict[str, Any] = {"source": "kanto", "source_id": "2"}
    agent_isni: dict[str, Any] = {"source": "isni", "source_id": "3"}
    agent_wikidata: dict[str, Any] = {"source": "wikidata", "source_id": "4"}
    agent_unknown: dict[str, Any] = {"source": "fennica", "source_id": "5"}
    agent_melinda: dict[str, Any] = {"source": "melinda", "source_id": "6"}

    rank_asteri = clusterer._authority_rank(agent_asteri)[0]
    rank_kanto = clusterer._authority_rank(agent_kanto)[0]
    rank_isni = clusterer._authority_rank(agent_isni)[0]
    rank_wikidata = clusterer._authority_rank(agent_wikidata)[0]
    rank_unknown = clusterer._authority_rank(agent_unknown)[0]
    rank_melinda = clusterer._authority_rank(agent_melinda)[0]

    assert rank_asteri < rank_kanto < rank_isni < rank_wikidata < rank_melinda
    assert rank_melinda == rank_unknown == len(config.source_authority_precedence) + 1


def test_matcher_fast_path_prioritizes_asteri_work() -> None:
    mock_repo = MagicMock()
    matcher = WorkMatcher(mock_repo)

    mock_work = MagicMock()
    mock_work.id = 42
    mock_work.work_uri = "urn:work:teka:000015266"

    # Mock repository returning work on asteri_work lookup
    def mock_lookup(
        scheme: str, value: str, authoritative_only: bool = False
    ) -> list[tuple[str, int]]:
        if scheme == "asteri_work" and value == "000015266":
            return [("work", 42)]
        return []

    mock_repo.lookup_by_identifier.side_effect = mock_lookup
    mock_repo.get_work.return_value = mock_work
    mock_repo.get_expressions_for_work.return_value = []

    target = MatchRecord(
        record_id="target_001",
        format="marcxml",
        title="The Egyptian",
        title_stem="egyptian",
        identifiers={
            "isbn": ["9789510123456"],
            "asteri_work": ["000015266"],
        },
    )

    result = matcher._try_fast_path_identifiers(
        target=target,
        ids=target.identifiers,
        funnel_name="book",
    )

    assert result is not None
    assert result.confidence_score == 1.0
    assert result.match_tier == "tier1_exact_identifier"
    assert result.work_uri == "urn:work:teka:000015266"


def test_configurable_multiple_work_authority_sources() -> None:
    xml = """
    <record xmlns="http://www.loc.gov/MARC21/slim">
      <controlfield tag="001">991234570</controlfield>
      <datafield tag="240" ind1="1" ind2="0">
        <subfield code="a">Custom Work Title</subfield>
        <subfield code="0">wikidata:Q42</subfield>
        <subfield code="0">(FI-ASTERI-W)000099999</subfield>
      </datafield>
      <datafield tag="245" ind1="1" ind2="0">
        <subfield code="a">Custom Edition</subfield>
      </datafield>
    </record>
    """
    elem = etree.fromstring(xml.encode("utf-8"))

    # When both wikidata and asteri_work are configured as work authorities
    custom_work_sources = ["wikidata", "asteri_work"]
    parsed = parse_marc_record_elem(elem, work_authority_sources=custom_work_sources)

    assert "Q42" in parsed["work_identifiers"]["wikidata"]
    assert "000099999" in parsed["work_identifiers"]["asteri_work"]
    assert "Q42" in parsed["identifiers"]["wikidata"]
    assert "000099999" in parsed["identifiers"]["asteri_work"]

    # When decomposed, both work authorities produce work IdentifierEntities dynamically
    *_, identifiers, _, _ = decompose_marc_wemi(elem, work_authority_sources=custom_work_sources)
    work_entities = [i for i in identifiers if i.entity_type == "work"]
    schemes = {i.scheme: i.source_id for i in work_entities}

    assert schemes.get("wikidata") == "Q42"
    assert schemes.get("asteri_work") == "000099999"


def test_default_carrier_source_from_config() -> None:
    from reconciliator.config import get_default_carrier_source

    # 1. Defaults loaded from defaults.yaml
    assert get_default_carrier_source() == "melinda"

    # 2. Config override
    custom_cfg = {"record_normalization": {"default_carrier_source": "fennica_carrier"}}
    assert get_default_carrier_source(custom_cfg) == "fennica_carrier"

    # 3. decompose_marc_wemi uses configured default when source is not explicitly passed
    xml = """
    <record xmlns="http://www.loc.gov/MARC21/slim">
      <controlfield tag="001">991234571</controlfield>
      <datafield tag="245" ind1="1" ind2="0">
        <subfield code="a">Default Carrier Test</subfield>
      </datafield>
    </record>
    """
    elem = etree.fromstring(xml.encode("utf-8"))

    # Default configuration
    work_default, _, manif_default, *_ = decompose_marc_wemi(elem)
    assert work_default.source == "melinda"
    assert manif_default.source == "melinda"

    # With custom configured default_carrier_source
    work_custom, _, manif_custom, *_ = decompose_marc_wemi(elem, config=custom_cfg)
    assert work_custom.source == "fennica_carrier"
    assert manif_custom.source == "fennica_carrier"


def test_agent_multiple_identifiers_extracted_uniformly() -> None:
    xml = """
    <record xmlns="http://www.loc.gov/MARC21/slim">
      <controlfield tag="001">991234572</controlfield>
      <datafield tag="100" ind1="1" ind2=" ">
        <subfield code="a">Virtanen, Matti</subfield>
        <subfield code="0">https://orcid.org/0000-0002-1825-0097</subfield>
        <subfield code="0">(FI-ASTERI-N)000012345</subfield>
        <subfield code="0">https://isni.org/isni/0000000121404100</subfield>
        <subfield code="1">http://www.wikidata.org/entity/Q42</subfield>
      </datafield>
      <datafield tag="245" ind1="1" ind2="0">
        <subfield code="a">Test Title</subfield>
      </datafield>
    </record>
    """
    elem = etree.fromstring(xml.encode("utf-8"))
    parsed = parse_marc_record_elem(elem)
    agent = parsed["agents"][0]
    agent_id_map = {i["source"]: i["source_id"] for i in agent["identifiers"]}

    assert agent_id_map.get("orcid") == "0000-0002-1825-0097"
    assert agent_id_map.get("kanto") == "000012345"
    assert agent_id_map.get("isni") == "0000000121404100"
    assert agent_id_map.get("wikidata") == "Q42"

    # Verify decompose_marc_wemi persists all agent identifiers without dropping non-ORCIDs
    *_, identifiers, _, _ = decompose_marc_wemi(elem)
    agent_entities = [i for i in identifiers if i.entity_type == "agent"]
    decomp_map = {i.source: i.source_id for i in agent_entities}

    assert decomp_map.get("orcid") == "0000-0002-1825-0097"
    assert decomp_map.get("kanto") == "000012345"
    assert decomp_map.get("isni") == "0000000121404100"
    assert decomp_map.get("wikidata") == "Q42"
