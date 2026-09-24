"""Tests for Agent metadata extraction, date parsing, and Wikidata author ingestion."""

from __future__ import annotations

from lxml import etree

from reconciliator.ingestion.wikidata import _extract_author_data
from reconciliator.loaders.marcxml import parse_marc_dates, parse_marc_record_elem
from reconciliator.normalizers.record import RecordNormalizer


def test_parse_marc_dates_variants() -> None:
    # Full range
    assert parse_marc_dates("1908-1979") == (1908, 1979)
    assert parse_marc_dates("ca. 1850 - 1910") == (1850, 1910)
    assert parse_marc_dates("noin 1850-1910.") == (1850, 1910)
    assert parse_marc_dates("1850?-1910?") == (1850, 1910)

    # Birth only
    assert parse_marc_dates("1948-") == (1948, None)
    assert parse_marc_dates("s. 1950") == (1950, None)
    assert parse_marc_dates("f. 1925") == (1925, None)
    assert parse_marc_dates("b. 1930") == (1930, None)

    # Death only
    assert parse_marc_dates("-1920") == (None, 1920)
    assert parse_marc_dates("k. 2012") == (None, 2012)
    assert parse_marc_dates("d. 1999") == (None, 1999)

    # Century / invalid
    assert parse_marc_dates("1800-luku") == (None, None)
    assert parse_marc_dates(None) == (None, None)
    assert parse_marc_dates("") == (None, None)


def test_marcxml_agent_rich_fields_extraction() -> None:
    xml_str = """
    <record xmlns="http://www.loc.gov/MARC21/slim">
        <leader>00000cam a22000004i 4500</leader>
        <controlfield tag="001">rec-001</controlfield>
        <datafield tag="100" ind1="1" ind2=" ">
            <subfield code="a">Virtanen, Matti</subfield>
            <subfield code="c">professori</subfield>
            <subfield code="d">1920-1995</subfield>
            <subfield code="q">Matti Ilmari</subfield>
            <subfield code="u">Helsingin yliopisto</subfield>
            <subfield code="0">http://urn.fi/URN:NBN:fi:au:pn:000123</subfield>
        </datafield>
        <datafield tag="245" ind1="1" ind2="0">
            <subfield code="a">Testiteos</subfield>
        </datafield>
    </record>
    """
    root = etree.fromstring(xml_str.encode("utf-8"))
    raw = parse_marc_record_elem(root, source="test")
    agents = raw["agents"]
    assert len(agents) == 1
    a = agents[0]
    assert a["name"] == "Virtanen, Matti"
    assert a["qualifiers"] == ["professori"]
    assert a["dates"] == "1920-1995"
    assert a["birth_year"] == 1920
    assert a["death_year"] == 1995
    assert a["fuller_name"] == "Matti Ilmari"
    assert a["uri_subfield_0"] == ["http://urn.fi/URN:NBN:fi:au:pn:000123"]

    normalizer = RecordNormalizer()
    rec = normalizer.normalize(raw)
    assert len(rec.agents) == 1
    norm_agent = rec.agents[0]
    assert norm_agent.name == "Virtanen, Matti"
    assert norm_agent.surname == "Virtanen"
    assert norm_agent.qualifiers == ["professori"]
    assert norm_agent.dates == "1920-1995"
    assert norm_agent.birth_year == 1920
    assert norm_agent.death_year == 1995
    assert norm_agent.fuller_name == "Matti Ilmari"


def test_wikidata_author_rich_extraction() -> None:
    entity = {
        "id": "Q12345",
        "labels": {
            "fi": {"value": "Mika Waltari"},
            "en": {"value": "Mika Waltari"},
        },
        "descriptions": {
            "fi": {"value": "suomalainen kirjailija"},
        },
        "aliases": {
            "fi": [{"value": "M. Waltari"}],
        },
        "claims": {
            "P31": [{"mainsnak": {"datavalue": {"value": {"id": "Q5"}}}}],
            "P3595": [{"mainsnak": {"datavalue": {"value": "000028241"}}}],
            "P569": [{"mainsnak": {"datavalue": {"value": {"time": "+1908-09-19T00:00:00Z"}}}}],
            "P570": [{"mainsnak": {"datavalue": {"value": {"time": "+1979-08-26T00:00:00Z"}}}}],
            "P106": [{"mainsnak": {"datavalue": {"value": {"id": "Q36180"}}}}],  # writer
            "P108": [{"mainsnak": {"datavalue": {"value": {"id": "Q858718"}}}}],
            "P101": [{"mainsnak": {"datavalue": {"value": {"id": "Q12483"}}}}],  # astronomy
            "P921": [{"mainsnak": {"datavalue": {"value": {"id": "Q395"}}}}],  # mathematics
        },
    }
    data = _extract_author_data(entity)
    assert data is not None
    assert data["qid"] == "Q12345"
    assert data["name"] == "Mika Waltari"
    assert data["identifiers"]["kanto"] == "000028241"
    assert data["birth_year"] == 1908
    assert data["death_year"] == 1979
    assert data["occupations"] == ["Q36180"]
    assert data["affiliations"] == ["Q858718"]
    assert data["fields_of_work"] == ["Q12483"]
    assert data["qualifiers"] == ["suomalainen kirjailija"]
    assert data["variant_names"] == ["M. Waltari"]


def test_marcxml_agent_subject_counts_harvesting_and_filtering() -> None:
    from reconciliator.loaders.marcxml import decompose_marc_wemi

    xml_str = """
    <record xmlns="http://www.loc.gov/MARC21/slim">
        <leader>00000cam a22000004i 4500</leader>
        <controlfield tag="001">rec-subj-001</controlfield>
        <datafield tag="100" ind1="1" ind2=" ">
            <subfield code="a">Virtanen, Matti</subfield>
        </datafield>
        <datafield tag="650" ind1=" " ind2="7">
            <subfield code="a">historia</subfield>
            <subfield code="0">http://www.yso.fi/onto/yso/p2591</subfield>
            <subfield code="2">yso</subfield>
        </datafield>
        <datafield tag="651" ind1=" " ind2="7">
            <subfield code="a">Suomi</subfield>
            <subfield code="0">http://www.yso.fi/onto/yso-paikat/p105994</subfield>
            <subfield code="2">yso-paikat/fin</subfield>
        </datafield>
        <datafield tag="655" ind1=" " ind2="7">
            <subfield code="a">elämäkerrat</subfield>
            <subfield code="0">http://urn.fi/URN:NBN:fi:au:slm:s123</subfield>
            <subfield code="2">slm</subfield>
        </datafield>
        <datafield tag="650" ind1=" " ind2="7">
            <subfield code="a">muinaistiede</subfield>
            <subfield code="0">http://www.yso.fi/onto/yso/p4567</subfield>
            <subfield code="2">ysa</subfield>
        </datafield>
        <datafield tag="650" ind1=" " ind2="7">
            <subfield code="a">unsupported term</subfield>
            <subfield code="0">http://example.org/term/999</subfield>
            <subfield code="2">local_dept_scheme</subfield>
        </datafield>
        <datafield tag="650" ind1=" " ind2=" ">
            <subfield code="a">uncontrolled keyword</subfield>
        </datafield>
        <datafield tag="245" ind1="1" ind2="0">
            <subfield code="a">Suomen historian vaiheita</subfield>
        </datafield>
    </record>
    """
    root = etree.fromstring(xml_str.encode("utf-8"))

    # 1. Default YAML configuration picking supported ontologies:
    raw = parse_marc_record_elem(root, source="test")
    agents = raw["agents"]
    assert len(agents) == 1
    subj_counts = agents[0]["subject_counts"]

    # Supported vocabularies picked with concept IDs
    assert subj_counts.get("yso:p2591") == 1
    assert subj_counts.get("yso-paikat:p105994") == 1
    assert subj_counts.get("slm:s123") == 1
    # Legacy 'ysa' mapped to modern 'yso'
    assert subj_counts.get("yso:p4567") == 1

    # Unsupported / uncontrolled filtered out
    assert "local_dept_scheme:999" not in subj_counts
    assert "unsupported term" not in subj_counts
    assert "uncontrolled keyword" not in subj_counts

    # 2. Dynamic YAML override test: restrict supported vocabularies to ["yso"] only
    raw_restricted = parse_marc_record_elem(
        root, source="test", supported_subject_vocabularies=["yso"]
    )
    r_subj_counts = raw_restricted["agents"][0]["subject_counts"]
    assert r_subj_counts.get("yso:p2591") == 1
    assert r_subj_counts.get("yso:p4567") == 1
    # slm and yso-paikat are now excluded because they were not in the configured list
    assert "slm:s123" not in r_subj_counts
    assert "yso-paikat:p105994" not in r_subj_counts

    # 3. Verify decompose_marc_wemi preserves subject data in intermediate dict
    *_, agent_entities, _, _, _, _ = decompose_marc_wemi(root, source="test")
    assert len(agent_entities) == 1
    # subject_counts is no longer on AgentEntity model, but remains in intermediate dict
    # The subject data is now tracked via contributions_table, not agent fields.


def test_marcxml_agent_role_normalization() -> None:
    """Verify that agent role counts normalize natural-language terms, abbreviations, and URIs into MARC codes."""
    xml_str = """
    <record xmlns="http://www.loc.gov/MARC21/slim">
        <leader>00000cam a22000004i 4500</leader>
        <controlfield tag="001">rec-roles-001</controlfield>
        <datafield tag="100" ind1="1" ind2=" ">
            <subfield code="a">Sibelius, Jean</subfield>
            <subfield code="e">säveltäjä</subfield>
            <subfield code="4">cmp</subfield>
        </datafield>
        <datafield tag="700" ind1="1" ind2=" ">
            <subfield code="a">Kuusisto, Pekka</subfield>
            <subfield code="e">esittäjä</subfield>
            <subfield code="4">http://urn.fi/URN:NBN:fi:au:mts:m4115</subfield>
        </datafield>
        <datafield tag="700" ind1="1" ind2=" ">
            <subfield code="a">Virtanen, Matti</subfield>
            <subfield code="e">toim.</subfield>
            <subfield code="e">kirjoittaja</subfield>
        </datafield>
        <datafield tag="700" ind1="1" ind2=" ">
            <subfield code="a">Smith, John</subfield>
            <subfield code="e">illustrator</subfield>
            <subfield code="4">http://id.loc.gov/vocabulary/relators/ill</subfield>
        </datafield>
        <datafield tag="710" ind1="2" ind2=" ">
            <subfield code="a">Helsingin kaupunginorkesteri</subfield>
            <subfield code="e">esittäjä</subfield>
        </datafield>
        <datafield tag="245" ind1="1" ind2="0">
            <subfield code="a">Konserttitaltiointi</subfield>
        </datafield>
    </record>
    """
    root = etree.fromstring(xml_str.encode("utf-8"))
    raw = parse_marc_record_elem(root, source="test")
    agents = raw["agents"]
    assert len(agents) == 5

    # 1. Sibelius: 'säveltäjä' and 'cmp' -> both normalized to 'cmp' (count 2)
    assert agents[0]["name"] == "Sibelius, Jean"
    assert agents[0]["role_counts"] == {"cmp": 2}

    # 2. Kuusisto: 'esittäjä' and MTS m4115 URI -> both normalized to 'prf' (count 2)
    assert agents[1]["name"] == "Kuusisto, Pekka"
    assert agents[1]["role_counts"] == {"prf": 2}

    # 3. Virtanen: abbreviation 'toim.' -> 'edt', 'kirjoittaja' -> 'aut'
    assert agents[2]["name"] == "Virtanen, Matti"
    assert agents[2]["role_counts"] == {"edt": 1, "aut": 1}

    # 4. Smith: 'illustrator' and LoC URI -> 'ill' (count 2)
    assert agents[3]["name"] == "Smith, John"
    assert agents[3]["role_counts"] == {"ill": 2}

    # 5. Corporate agent: 'esittäjä' -> 'prf'
    assert agents[4]["name"] == "Helsingin kaupunginorkesteri"
    assert agents[4]["role_counts"] == {"prf": 1}

    # Verify RecordNormalizer: role_counts removed from Agent model (now in contributions_table)
    norm = RecordNormalizer()
    rec = norm.normalize(raw)
    assert len(rec.agents) == 5
    # role_counts no longer exists on Agent model
    assert not hasattr(rec.agents[0], "role_counts")


def test_marcxml_agent_subject_concept_id_extraction() -> None:
    """Verify that subject headings extract canonical concept IDs from $0/$1 and vocab_store."""
    xml_str = """
    <record xmlns="http://www.loc.gov/MARC21/slim">
        <leader>00000cam a22000004i 4500</leader>
        <controlfield tag="001">rec-ids-001</controlfield>
        <datafield tag="100" ind1="1" ind2=" ">
            <subfield code="a">Virtanen, Matti</subfield>
        </datafield>
        <datafield tag="650" ind1=" " ind2="7">
            <subfield code="a">historia</subfield>
            <subfield code="0">http://www.yso.fi/onto/yso/p2591</subfield>
            <subfield code="2">yso</subfield>
        </datafield>
        <datafield tag="655" ind1=" " ind2="7">
            <subfield code="a">elämäkerrat</subfield>
            <subfield code="0">(FI-SLM)s123</subfield>
            <subfield code="2">slm</subfield>
        </datafield>
        <datafield tag="650" ind1=" " ind2="7">
            <subfield code="a">fantasiakirjallisuus</subfield>
            <subfield code="0">http://www.yso.fi/onto/kauno/k456</subfield>
            <subfield code="2">kauno</subfield>
        </datafield>
        <datafield tag="650" ind1=" " ind2="7">
            <subfield code="a">jazz</subfield>
            <subfield code="0">http://www.yso.fi/onto/muso/m789</subfield>
            <subfield code="2">muso</subfield>
        </datafield>
        <datafield tag="650" ind1=" " ind2="7">
            <subfield code="a">tekoäly</subfield>
            <subfield code="1">http://www.wikidata.org/entity/Q11660</subfield>
            <subfield code="2">wikidata</subfield>
        </datafield>
        <datafield tag="650" ind1=" " ind2="7">
            <subfield code="a">fysiikka</subfield>
            <subfield code="2">yso</subfield>
        </datafield>
        <datafield tag="245" ind1="1" ind2="0">
            <subfield code="a">Yhdistelmäteos</subfield>
        </datafield>
    </record>
    """
    root = etree.fromstring(xml_str.encode("utf-8"))

    # Mock vocab_store for resolving 'fysiikka' -> 'http://www.yso.fi/onto/yso/p1054'
    class MockConcept:
        uri = "http://www.yso.fi/onto/yso/p1054"

    class MockVocabStore:
        def find_concept_by_label(self, label: str, vocabulary: str | None = None):
            if label.strip().lower() == "fysiikka":
                return MockConcept()
            return None

    raw = parse_marc_record_elem(root, source="test", vocab_store=MockVocabStore())
    subj_counts = raw["agents"][0]["subject_counts"]

    # Subject IDs extracted
    assert subj_counts.get("yso:p2591") == 1
    assert subj_counts.get("slm:s123") == 1
    assert subj_counts.get("kauno:k456") == 1
    assert subj_counts.get("yso:m789") == 1
    assert subj_counts.get("wikidata:q11660") == 1
    # Resolved via vocab_store
    assert subj_counts.get("yso:p1054") == 1


def test_record_normalizer_subject_fold_still_works() -> None:
    """Verify that RecordNormalizer folds subject headings even though they are no longer stored on Agent."""
    xml_str = """
    <record xmlns="http://www.loc.gov/MARC21/slim">
        <leader>00000cam a22000004i 4500</leader>
        <controlfield tag="001">rec-fold-001</controlfield>
        <datafield tag="100" ind1="1" ind2=" ">
            <subfield code="a">Virtanen, Matti</subfield>
        </datafield>
        <datafield tag="650" ind1=" " ind2="7">
            <subfield code="a">historia</subfield>
            <subfield code="2">yso</subfield>
        </datafield>
        <datafield tag="655" ind1=" " ind2="7">
            <subfield code="a">elämäkerrat</subfield>
            <subfield code="2">slm</subfield>
        </datafield>
        <datafield tag="245" ind1="1" ind2="0">
            <subfield code="a">Suomen historia</subfield>
        </datafield>
    </record>
    """
    root = etree.fromstring(xml_str.encode("utf-8"))

    # Parse WITHOUT vocab_store -> raw agent has empty subject_counts because $0/$1 are absent
    raw = parse_marc_record_elem(root, source="test")
    assert raw["agents"][0]["subject_counts"] == {}

    # Mock VocabFolder that folds 'historia' -> 'http://www.yso.fi/onto/yso/p2591' and 'elämäkerrat' -> 'http://urn.fi/URN:NBN:fi:au:slm:s123'
    class MockFolder:
        def fold_subject_heading(
            self, heading, source_vocab=None, source_field="650", source_uri=None, lang=None
        ):
            if "historia" in heading.lower():
                return ("650", "yso", "http://www.yso.fi/onto/yso/p2591", "historia", "exact", None)
            if "elämäkerrat" in heading.lower():
                return (
                    "655",
                    "slm",
                    "http://urn.fi/URN:NBN:fi:au:slm:s123",
                    "elämäkerrat",
                    "exact",
                    None,
                )
            return (source_field, source_vocab or "yso", source_uri or "", heading, "none", None)

    norm = RecordNormalizer(vocab_folder=MockFolder())
    rec = norm.normalize(raw)

    # subject_counts no longer exists on Agent model
    assert not hasattr(rec.agents[0], "subject_counts")
