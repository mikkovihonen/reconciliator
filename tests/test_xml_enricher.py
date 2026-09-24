from pathlib import Path

from lxml import etree

from reconciliator.loaders.marcxml import _get_subfields
from reconciliator.models import Agent, BibliographicRecord
from reconciliator.reconciliation import ReconciliationResult
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
        EnrichmentConfig,
        get_bundled_enrichment_defaults,
    )

    # 1. Defaults loaded from YAML
    yaml_defaults = get_bundled_enrichment_defaults()
    assert "predicate_profiles" in yaml_defaults
    assert "bffi" in yaml_defaults["predicate_profiles"]
    assert "bibframe" in yaml_defaults["predicate_profiles"]
    assert "rda" in yaml_defaults["predicate_profiles"]

    assert "bffi" in DEFAULT_MARC_PREDICATE_PROFILES
    assert DEFAULT_MARC_PREDICATE_PROFILES["bffi"]["work"] == (
        "http://urn.fi/URN:NBN:fi:schema:bffi:workManifested",
        "bffi",
    )

    # 2. EnrichmentConfig accepts custom predicate_profiles
    custom_profiles = {
        "custom": {
            "work": ("http://example.org/pred/work", "custom-scheme"),
            "expression": ("http://example.org/pred/expression", "custom-scheme"),
        }
    }
    toggles = EnrichmentConfig(
        active_profiles=["custom"],
        predicate_profiles=custom_profiles,
        marc_predicate_mode="distinct",
    )
    assert "custom" in toggles.predicate_profiles
    assert toggles.active_profiles == ["custom"]

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
    tr = MatchRecord(
        record_id="rec2",
        title="Archived Site",
        format="marcxml",
        title_stem="archived site",
        web_seed_key="http://example.org/new-seed",
    )

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
    from reconciliator.config import EnrichmentConfig
    from reconciliator.models import MatchRecord, WorkCluster

    enricher = MARCXMLEnricher()
    work = WorkCluster(
        cluster_id="w2", canonical_title="Swedish Book", canonical_work_uri="http://example.org/w2"
    )
    tr = MatchRecord(
        record_id="rec3",
        title="Swedish Book",
        format="marcxml",
        title_stem="swedish book",
        host_citation_key="HOST-001",
    )

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
    toggles_en = EnrichmentConfig(language="en")
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
    tr = MatchRecord(
        record_id="rec5",
        title="Series Item",
        format="marcxml",
        title_stem="series item",
        series_key="otavan-kirjasto-12",
    )

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


def test_ephemera_enrichment_configurable_terms() -> None:
    from reconciliator.config import EnrichmentConfig
    from reconciliator.models import MatchRecord

    custom_cfg = EnrichmentConfig(
        ephemera_terms=["custom_ephemera_tag"],
    )
    enricher = MARCXMLEnricher()

    tr = MatchRecord(
        record_id="ephem1",
        title="Ephemera Item",
        format="marcxml",
        title_stem="ephemera item",
        keys={"ephemera_corp": ["org1"]},
    )

    xml_str = """
    <record xmlns="http://www.loc.gov/MARC21/slim">
      <controlfield tag="001">006</controlfield>
      <datafield tag="655" ind1=" " ind2="7">
        <subfield code="a">Custom_Ephemera_Tag</subfield>
        <subfield code="2">slm</subfield>
      </datafield>
    </record>
    """
    elem = etree.fromstring(xml_str.strip())
    from reconciliator.reconciliation import WEMIMatchResult

    wemi_res = WEMIMatchResult(
        target_record_id="ephem1",
        work_uri="http://example.org/work/ephemera1",
    )
    enriched = enricher.enrich_record_tree(
        elem,
        target_record=tr,
        wemi_result=wemi_res,
        enrich=True,
        enrichment_config=custom_cfg,
    )
    f655 = enriched.find(".//{http://www.loc.gov/MARC21/slim}datafield[@tag='655']")
    assert f655 is not None
    assert _get_subfields(f655, "0") == ["http://example.org/work/ephemera1"]


def test_thesis_enrichment_configurable_relator() -> None:
    from reconciliator.config import EnrichmentConfig
    from reconciliator.models import MatchRecord

    custom_cfg = EnrichmentConfig(
        thesis_relator_code="custom_dgg",
        thesis_institution_uri="http://example.org/institution/univ1",
    )
    enricher = MARCXMLEnricher()

    tr = MatchRecord(
        record_id="thesis1",
        title="Thesis Item",
        format="marcxml",
        title_stem="thesis item",
        work_type="thesis",
    )

    xml_str = """
    <record xmlns="http://www.loc.gov/MARC21/slim">
      <controlfield tag="001">007</controlfield>
      <datafield tag="710" ind1="2" ind2=" ">
        <subfield code="a">Helsingin yliopisto</subfield>
        <subfield code="4">custom_dgg</subfield>
      </datafield>
    </record>
    """
    elem = etree.fromstring(xml_str.strip())
    enriched = enricher.enrich_record_tree(
        elem,
        target_record=tr,
        enrich=True,
        enrichment_config=custom_cfg,
    )
    f710 = enriched.find(".//{http://www.loc.gov/MARC21/slim}datafield[@tag='710']")
    assert f710 is not None
    assert _get_subfields(f710, "0") == ["http://example.org/institution/univ1"]


def test_enrich_agent_actionable_urns() -> None:
    """Test that agents receive full actionable URNs from configured templates."""
    xml_str = """
    <record xmlns="http://www.loc.gov/MARC21/slim">
      <controlfield tag="001">008</controlfield>
      <datafield tag="700" ind1="1" ind2=" ">
        <subfield code="a">Waltari, Mika</subfield>
      </datafield>
    </record>
    """
    elem = etree.fromstring(xml_str.strip())
    rec = BibliographicRecord(
        source_id="008",
        source="target",
        title="Test Record",
        agents=[
            Agent(name="Waltari, Mika", surname="Waltari"),
            Agent(name="Atena Kustannus", surname=None),
        ],
    )

    res = ReconciliationResult(
        target_record=rec,
        selected_agent_uris={
            0: ["kanto_pn:000123456"],
            1: ["kanto_cn:000000252"],
        },
    )

    enricher = MARCXMLEnricher()
    # Add a 710 datafield for Atena Kustannus
    f710_elem = etree.SubElement(elem, "{http://www.loc.gov/MARC21/slim}datafield", tag="710", ind1="2", ind2=" ")
    s_a = etree.SubElement(f710_elem, "{http://www.loc.gov/MARC21/slim}subfield", code="a")
    s_a.text = "Atena Kustannus"

    enriched = enricher.enrich_record_tree(elem, res, enrich=True, fold_legacy=False)

    f700 = enriched.find(".//{http://www.loc.gov/MARC21/slim}datafield[@tag='700']")
    assert f700 is not None
    u0_700 = _get_subfields(f700, "0")
    assert u0_700 == ["https://urn.fi/URN:NBN:fi:au:pn:000123456"]

    f710 = enriched.find(".//{http://www.loc.gov/MARC21/slim}datafield[@tag='710']")
    assert f710 is not None
    u0_710 = _get_subfields(f710, "0")
    assert u0_710 == ["https://urn.fi/URN:NBN:fi:au:cn:000000252"]


def test_enrich_subject_headings_full_actionable_urns() -> None:
    """Test that subject fields with URN vocabularies or ontologies receive full actionable URNs/URIs."""
    from unittest.mock import MagicMock

    xml_str = """
    <record xmlns="http://www.loc.gov/MARC21/slim">
      <controlfield tag="001">009</controlfield>
      <datafield tag="655" ind1=" " ind2="7">
        <subfield code="a">sarjakuvat</subfield>
        <subfield code="2">slm/fin</subfield>
      </datafield>
      <datafield tag="650" ind1=" " ind2="7">
        <subfield code="a">kosto</subfield>
        <subfield code="2">yso/fin</subfield>
      </datafield>
      <datafield tag="651" ind1=" " ind2="7">
        <subfield code="a">Iso-Britannia</subfield>
        <subfield code="2">yso-paikat/fin</subfield>
      </datafield>
      <datafield tag="648" ind1=" " ind2="7">
        <subfield code="a">1980-luku</subfield>
        <subfield code="2">yso-aika</subfield>
      </datafield>
    </record>
    """
    elem = etree.fromstring(xml_str.strip())
    enricher = MARCXMLEnricher()

    mock_vf = MagicMock()

    def mock_fold(heading, source_vocab=None, source_field=None, source_uri=None, lang=None):
        if source_field == "655":
            return ("655", "slm/fin", "slm:s356", "sarjakuvat", "exact", None)
        elif source_field == "650":
            return ("650", "yso/fin", "yso:p22090", "kosto", "exact", None)
        elif source_field == "651":
            return ("651", "yso/fin", "yso-paikat:p104990", "Iso-Britannia", "exact", None)
        elif source_field == "648":
            return ("648", "yso/fin", "yso-aika:p6198061989", "1980-luku", "exact", None)
        return (source_field, source_vocab, source_uri or "", heading, "none", None)

    mock_vf.fold_subject_heading.side_effect = mock_fold
    enricher.vocab_folder = mock_vf

    enriched = enricher.enrich_record_tree(elem, enrich=True, fold_legacy=True)

    f655 = enriched.find(".//{http://www.loc.gov/MARC21/slim}datafield[@tag='655']")
    f650 = enriched.find(".//{http://www.loc.gov/MARC21/slim}datafield[@tag='650']")
    f651 = enriched.find(".//{http://www.loc.gov/MARC21/slim}datafield[@tag='651']")
    f648 = enriched.find(".//{http://www.loc.gov/MARC21/slim}datafield[@tag='648']")

    assert _get_subfields(f655, "0") == ["https://urn.fi/URN:NBN:fi:au:slm:s356"]
    assert _get_subfields(f650, "0") == ["http://www.yso.fi/onto/yso/p22090"]
    assert _get_subfields(f651, "0") == ["http://www.yso.fi/onto/yso/p104990"]
    assert _get_subfields(f648, "0") == ["http://www.yso.fi/onto/yso/p6198061989"]


def test_enrich_subject_headings_record_language_selection() -> None:
    """Test that MARCXML enrichment adapts subject heading labels and $2 subcodes to record language and per-vocab config."""
    from unittest.mock import MagicMock
    from reconciliator.config import VocabulariesConfig
    from reconciliator.models import VocabConcept
    from reconciliator.vocab import VocabFolder

    store = MagicMock()
    concept_yso = VocabConcept(
        uri="http://www.yso.fi/onto/yso/p2591",
        vocabulary="yso",
        concept_id="p2591",
        pref_labels={"fi": "lääkärit", "sv": "läkare", "en": "physicians"},
        target_field="650",
    )
    concept_kauno = VocabConcept(
        uri="http://www.yso.fi/onto/kauno/k123",
        vocabulary="kauno",
        concept_id="k123",
        pref_labels={"fi": "rikosromaanit", "sv": "kriminalromaner"},
        target_field="650",
    )

    def find_by_uri(uri):
        if uri == "http://www.yso.fi/onto/yso/p2591":
            return concept_yso
        if uri == "http://www.yso.fi/onto/kauno/k123":
            return concept_kauno
        return None

    store.find_concept_by_uri.side_effect = find_by_uri

    # Case A: Swedish record with default_label_language="record" -> both become Swedish
    cfg_record = VocabulariesConfig(default_label_language="record")
    vf_record = VocabFolder(vocab_store=store, config=cfg_record)
    enricher_swe = MARCXMLEnricher(vocab_folder=vf_record)

    xml_swe = """
    <record xmlns="http://www.loc.gov/MARC21/slim">
      <controlfield tag="001">swe001</controlfield>
      <controlfield tag="008">850101s1985    fi ||||||||||||||| |swe|d</controlfield>
      <datafield tag="650" ind1=" " ind2="7">
        <subfield code="a">lääkärit</subfield>
        <subfield code="0">http://www.yso.fi/onto/yso/p2591</subfield>
        <subfield code="2">yso/fin</subfield>
      </datafield>
      <datafield tag="650" ind1=" " ind2="7">
        <subfield code="a">rikosromaanit</subfield>
        <subfield code="0">http://www.yso.fi/onto/kauno/k123</subfield>
        <subfield code="2">kauno/fin</subfield>
      </datafield>
    </record>
    """
    elem_swe = etree.fromstring(xml_swe.strip())
    enriched_swe = enricher_swe.enrich_record_tree(elem_swe, enrich=False, fold_legacy=True)

    fields_650 = enriched_swe.findall(".//{http://www.loc.gov/MARC21/slim}datafield[@tag='650']")
    assert len(fields_650) == 2

    # YSO: translated to Swedish
    assert _get_subfields(fields_650[0], "a") == ["läkare"]
    assert _get_subfields(fields_650[0], "2") == ["yso/swe"]

    # Kauno: translated to Swedish
    assert _get_subfields(fields_650[1], "a") == ["kriminalromaner"]
    assert _get_subfields(fields_650[1], "2") == ["kauno/swe"]

    # Case B: Swedish record with per-vocabulary override vocabulary_label_languages={"kauno": "fi"}
    cfg_override = VocabulariesConfig(
        default_label_language="record",
        vocabulary_label_languages={"kauno": "fi"},
    )
    vf_override = VocabFolder(vocab_store=store, config=cfg_override)
    enricher_override = MARCXMLEnricher(vocab_folder=vf_override)

    elem_swe2 = etree.fromstring(xml_swe.strip())
    enriched_override = enricher_override.enrich_record_tree(elem_swe2, enrich=False, fold_legacy=True)

    fields_650_ov = enriched_override.findall(".//{http://www.loc.gov/MARC21/slim}datafield[@tag='650']")
    # YSO still Swedish
    assert _get_subfields(fields_650_ov[0], "a") == ["läkare"]
    assert _get_subfields(fields_650_ov[0], "2") == ["yso/swe"]
    # Kauno pinned to Finnish
    assert _get_subfields(fields_650_ov[1], "a") == ["rikosromaanit"]
    assert _get_subfields(fields_650_ov[1], "2") == ["kauno/fin"]

    # Case C: Finnish record
    xml_fin = """
    <record xmlns="http://www.loc.gov/MARC21/slim">
      <controlfield tag="001">fin001</controlfield>
      <controlfield tag="008">850101s1985    fi ||||||||||||||| |fin|d</controlfield>
      <datafield tag="650" ind1=" " ind2="7">
        <subfield code="a">läkare</subfield>
        <subfield code="0">http://www.yso.fi/onto/yso/p2591</subfield>
        <subfield code="2">yso/swe</subfield>
      </datafield>
    </record>
    """
    elem_fin = etree.fromstring(xml_fin.strip())
    enriched_fin = enricher_swe.enrich_record_tree(elem_fin, enrich=False, fold_legacy=True)
    field_fin = enriched_fin.find(".//{http://www.loc.gov/MARC21/slim}datafield[@tag='650']")
    assert _get_subfields(field_fin, "a") == ["lääkärit"]
    assert _get_subfields(field_fin, "2") == ["yso/fin"]
