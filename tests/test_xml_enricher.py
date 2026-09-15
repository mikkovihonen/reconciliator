from pathlib import Path

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


def test_iter_marc_record_elements(tmp_path: Path) -> None:
    from reconciliator.loaders.marcxml import iter_marc_record_elements

    xml_content = """<?xml version="1.0" encoding="UTF-8"?>
    <collection xmlns="http://www.loc.gov/MARC21/slim">
      <record>
        <controlfield tag="001">REC-1</controlfield>
      </record>
      <record>
        <controlfield tag="001">REC-2</controlfield>
      </record>
    </collection>
    """
    file_path = tmp_path / "test.marcxml"
    file_path.write_text(xml_content, encoding="utf-8")

    elements = list(iter_marc_record_elements(file_path))
    assert len(elements) == 2
    # Elements are yielded with byte offsets > 0
    assert elements[0][1] > 0
    assert elements[1][1] >= elements[0][1]


def test_marc_predicate_profiles_configuration() -> None:
    from reconciliator.config import (
        DEFAULT_MARC_PREDICATE_PROFILES,
        EnrichmentTogglesConfig,
        get_bundled_record_defaults,
    )

    # 1. Defaults loaded from YAML
    yaml_defaults = get_bundled_record_defaults()
    assert "marc_predicate_profiles" in yaml_defaults
    assert "bffi" in yaml_defaults["marc_predicate_profiles"]
    assert "bibframe" in yaml_defaults["marc_predicate_profiles"]
    assert "rda" in yaml_defaults["marc_predicate_profiles"]

    assert "bffi" in DEFAULT_MARC_PREDICATE_PROFILES
    assert DEFAULT_MARC_PREDICATE_PROFILES["bffi"]["work"] == (
        "http://urn.fi/URN:NBN:fi:schema:bffi:workManifested",
        "bffi",
    )

    # 2. EnrichmentTogglesConfig accepts custom predicate_profiles
    custom_profiles = {
        "custom": {
            "work": ("http://example.org/pred/work", "custom-scheme"),
            "expression": ("http://example.org/pred/expression", "custom-scheme"),
        }
    }
    toggles = EnrichmentTogglesConfig(
        marc_predicate_profiles=["custom"],
        predicate_profiles=custom_profiles,
        marc_predicate_mode="distinct",
    )
    assert "custom" in toggles.predicate_profiles
    assert toggles.marc_predicate_profiles == ["custom"]

    # 3. Test tree enrichment with custom profile
    xml_str = """
    <record xmlns="http://www.loc.gov/MARC21/slim">
      <controlfield tag="001">001</controlfield>
      <datafield tag="245" ind1="1" ind2="0">
        <subfield code="a">Test Work</subfield>
      </datafield>
    </record>
    """
    elem = etree.fromstring(xml_str.strip())
    enricher = MARCXMLEnricher()

    from reconciliator.models import ExpressionCluster, WorkCluster
    work = WorkCluster(
        cluster_id="w1",
        canonical_title="Test Work",
        canonical_work_uri="http://example.org/work/w1",
    )
    expr = ExpressionCluster(
        cluster_id="e1",
        work_cluster_id="w1",
        language="fin",
        canonical_expression_uri="http://example.org/expr/e1",
    )

    enriched = enricher.enrich_record_tree(
        elem,
        enrich=True,
        fold_legacy=False,
        work_cluster=work,
        expression_cluster=expr,
        enrichment_config=toggles,
    )

    fields758 = enriched.findall(".//{http://www.loc.gov/MARC21/slim}datafield[@tag='758']")
    assert len(fields758) == 2

    # Check custom work 758
    w_field = fields758[0]
    assert _get_subfields(w_field, "4") == ["http://example.org/pred/work"]
    assert _get_subfields(w_field, "2") == ["custom-scheme"]
    assert _get_subfields(w_field, "0") == ["http://example.org/work/w1"]

    # Check custom expression 758
    e_field = fields758[1]
    assert _get_subfields(e_field, "4") == ["http://example.org/pred/expression"]
    assert _get_subfields(e_field, "2") == ["custom-scheme"]
    assert _get_subfields(e_field, "0") == ["http://example.org/expr/e1"]


def test_scheme_definition_and_deduplication() -> None:
    from reconciliator.config import SchemeDefinition
    from reconciliator.models import MatchRecord

    # 1. Test SchemeDefinition coercion and default-in-accepted guarantee
    s = SchemeDefinition(default="memento", accepted=["wayback", "archive-it"])
    assert "memento" in s.accepted
    assert s.accepted[0] == "memento"

    s_str = SchemeDefinition.model_validate("feigb")
    assert s_str.default == "feigb"
    assert s_str.accepted == ["feigb"]

    # 2. Existing alternative scheme ($2 wayback) suppresses web_harvest injection
    xml_with_wayback = """
    <record xmlns="http://www.loc.gov/MARC21/slim">
      <controlfield tag="001">002</controlfield>
      <datafield tag="024" ind1="7" ind2=" ">
        <subfield code="a">http://example.org/archive</subfield>
        <subfield code="2">wayback</subfield>
      </datafield>
    </record>
    """
    elem = etree.fromstring(xml_with_wayback.strip())
    enricher = MARCXMLEnricher()
    tr = MatchRecord(record_id="rec2", title="Archived Site", format="marcxml", title_stem="archived site", web_seed_key="http://example.org/new-seed")

    enriched = enricher.enrich_record_tree(
        elem,
        target_record=tr,
        enrich=True,
        fold_legacy=False,
    )
    # Since 'wayback' is in accepted schemes for web_harvest, no duplicate 024 was injected
    fields024 = enriched.findall(".//{http://www.loc.gov/MARC21/slim}datafield[@tag='024']")
    assert len(fields024) == 1
    assert _get_subfields(fields024[0], "2") == ["wayback"]


def test_multilingual_linking_labels() -> None:
    from reconciliator.config import EnrichmentTogglesConfig
    from reconciliator.models import MatchRecord, WorkCluster

    enricher = MARCXMLEnricher()
    work = WorkCluster(cluster_id="w2", canonical_title="Swedish Book", canonical_work_uri="http://example.org/w2")
    tr = MatchRecord(record_id="rec3", title="Swedish Book", format="marcxml", title_stem="swedish book", host_citation_key="HOST-001")

    # 1. Swedish record (via 041 $a)
    xml_swe = """
    <record xmlns="http://www.loc.gov/MARC21/slim">
      <controlfield tag="001">003</controlfield>
      <datafield tag="041" ind1="0" ind2=" ">
        <subfield code="a">swe</subfield>
      </datafield>
    </record>
    """
    elem_swe = etree.fromstring(xml_swe.strip())
    enriched_swe = enricher.enrich_record_tree(
        elem_swe,
        target_record=tr,
        work_cluster=work,
        enrich=True,
        fold_legacy=False,
    )
    # Check Swedish 773 $i
    f773 = enriched_swe.find(".//{http://www.loc.gov/MARC21/slim}datafield[@tag='773']")
    assert f773 is not None
    assert _get_subfields(f773, "i") == ["Ingår i"]

    # Check Swedish 758 $i
    f758 = enriched_swe.find(".//{http://www.loc.gov/MARC21/slim}datafield[@tag='758']")
    assert f758 is not None
    assert _get_subfields(f758, "i") == ["Manifesterat verk"]

    # 2. English record (via toggles.language override)
    xml_eng = """
    <record xmlns="http://www.loc.gov/MARC21/slim">
      <controlfield tag="001">004</controlfield>
    </record>
    """
    elem_eng = etree.fromstring(xml_eng.strip())
    toggles_en = EnrichmentTogglesConfig(language="en")
    enriched_eng = enricher.enrich_record_tree(
        elem_eng,
        target_record=tr,
        work_cluster=work,
        enrich=True,
        fold_legacy=False,
        enrichment_config=toggles_en,
    )
    f773_en = enriched_eng.find(".//{http://www.loc.gov/MARC21/slim}datafield[@tag='773']")
    assert f773_en is not None
    assert _get_subfields(f773_en, "i") == ["Contained in"]

    f758_en = enriched_eng.find(".//{http://www.loc.gov/MARC21/slim}datafield[@tag='758']")
    assert f758_en is not None
    assert _get_subfields(f758_en, "i") == ["Work manifested"]


def test_series_uri_generation() -> None:
    from reconciliator.models import MatchRecord

    enricher = MARCXMLEnricher()
    tr = MatchRecord(record_id="rec5", title="Series Item", format="marcxml", title_stem="series item", series_key="otavan-kirjasto-12")

    xml_str = """
    <record xmlns="http://www.loc.gov/MARC21/slim">
      <controlfield tag="001">005</controlfield>
      <datafield tag="830" ind1=" " ind2="0">
        <subfield code="a">Otavan kirjasto</subfield>
      </datafield>
    </record>
    """
    elem = etree.fromstring(xml_str.strip())
    enriched = enricher.enrich_record_tree(
        elem,
        target_record=tr,
        enrich=True,
        fold_legacy=False,
    )
    f830 = enriched.find(".//{http://www.loc.gov/MARC21/slim}datafield[@tag='830']")
    assert f830 is not None
    assert _get_subfields(f830, "0") == ["urn:reconciliator:series:otavan-kirjasto-12"]


