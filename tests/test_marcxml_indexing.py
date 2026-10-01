"""Tests for optimized single-pass MARCXML indexing and field extraction."""

from lxml import etree

from reconciliator.ingestion.oai import _WEMIBatchAccumulator
from reconciliator.loaders.marcxml import (
    _get_controlfield,
    _get_datafields,
    _get_subfield,
    _get_subfields,
    decompose_marc_authority,
    decompose_marc_wemi,
    extract_marc_source_id,
    parse_marc_record_elem,
)
from reconciliator.models import AgentEntity, ManifestationEntity, WorkEntity

SAMPLE_BIB_XML = b"""<record xmlns="http://www.loc.gov/MARC21/slim">
  <leader>01234nam a2200349 c 4500</leader>
  <controlfield tag="001">123456</controlfield>
  <controlfield tag="003">FI-MELI</controlfield>
  <controlfield tag="008">950101s1995    fi |||||||||||||| ||fin|d</controlfield>
  <datafield tag="020" ind1=" " ind2=" "><subfield code="a">978-951-0-12345-6</subfield></datafield>
  <datafield tag="100" ind1="1" ind2=" ">
    <subfield code="a">Virtanen, Ville,</subfield>
    <subfield code="d">1950-</subfield>
    <subfield code="e">kirjoittaja.</subfield>
  </datafield>
  <datafield tag="245" ind1="1" ind2="0">
    <subfield code="a">Testikirja :</subfield>
    <subfield code="b">alaotsikko /</subfield>
    <subfield code="c">Ville Virtanen.</subfield>
  </datafield>
  <datafield tag="260" ind1=" " ind2=" ">
    <subfield code="a">Helsinki :</subfield>
    <subfield code="b">Otava,</subfield>
    <subfield code="c">1995</subfield>
  </datafield>
  <datafield tag="650" ind1=" " ind2="7">
    <subfield code="a">Suomen historia</subfield>
    <subfield code="2">yso/fin</subfield>
  </datafield>
  <datafield tag="650" ind1=" " ind2="7">
    <subfield code="a">Kirjallisuudentutkimus</subfield>
    <subfield code="2">yso/fin</subfield>
  </datafield>
</record>"""

SAMPLE_AUTH_XML = b"""<record xmlns="http://www.loc.gov/MARC21/slim">
  <leader>00520cz  a2200145n  450</leader>
  <controlfield tag="001">auth123</controlfield>
  <controlfield tag="003">FI-ASTERI</controlfield>
  <controlfield tag="008">010101|| anznnnbabn           |a ana||||||</controlfield>
  <datafield tag="024" ind1="7" ind2=" ">
    <subfield code="a">0000 0001 2345 6789</subfield>
    <subfield code="2">isni</subfield>
  </datafield>
  <datafield tag="100" ind1="1" ind2=" ">
    <subfield code="a">Virtanen, Ville,</subfield>
    <subfield code="d">1950-</subfield>
  </datafield>
  <datafield tag="400" ind1="1" ind2=" ">
    <subfield code="a">Virtanen, V.</subfield>
  </datafield>
</record>"""


def test_marc_field_and_subfield_getters() -> None:
    elem = etree.fromstring(SAMPLE_BIB_XML)

    # Controlfields
    assert _get_controlfield(elem, "001") == "123456"
    assert _get_controlfield(elem, "003") == "FI-MELI"
    assert _get_controlfield(elem, "999") is None

    # Datafields
    f245 = _get_datafields(elem, "245")
    assert len(f245) == 1
    assert _get_subfield(f245[0], "a") == "Testikirja :"
    assert _get_subfield(f245[0], "b") == "alaotsikko /"
    assert _get_subfield(f245[0], "z") is None

    # Subfields list
    f650 = _get_datafields(elem, "650")
    assert len(f650) == 2
    subs_a = [_get_subfield(f, "a") for f in f650]
    assert subs_a == ["Suomen historia", "Kirjallisuudentutkimus"]

    f100 = _get_datafields(elem, "100")[0]
    assert _get_subfields(f100, "e") == ["kirjoittaja."]
    assert _get_subfields(f100, "nonexistent") == []


def test_extract_marc_source_id() -> None:
    elem = etree.fromstring(SAMPLE_BIB_XML)
    source_id = extract_marc_source_id(elem, id_source="marc_001")
    assert source_id == "(FI-MELI)123456"


def test_parse_marc_record_elem() -> None:
    elem = etree.fromstring(SAMPLE_BIB_XML)
    parsed = parse_marc_record_elem(elem, source="test_source")
    assert parsed["source_id"] == "(FI-MELI)123456"
    assert parsed["title"] == "Testikirja"
    assert parsed["subtitle"] == "alaotsikko"
    assert parsed["agents"][0]["name"] == "Virtanen, Ville"
    assert parsed["publication_year"] == "1995"
    assert parsed["identifiers"]["isbn"] == ["978-951-0-12345-6"]


def test_decompose_marc_wemi() -> None:
    elem = etree.fromstring(SAMPLE_BIB_XML)
    res = decompose_marc_wemi(
        elem,
        source="melinda",
        id_source="marc_001",
    )
    assert res.work.title == "Testikirja"
    assert len(res.agents) == 1
    assert res.agents[0].name == "Virtanen, Ville"
    assert res.manifestation.publication_year == 1995


def test_decompose_marc_authority() -> None:
    elem = etree.fromstring(SAMPLE_AUTH_XML)
    works, exprs, manifs, items, agents, rels, idents, assoc, contribs = decompose_marc_authority(
        elem,
        source="asteri",
        id_source="marc_001",
    )
    assert len(agents) == 1
    assert agents[0].name == "Virtanen, Ville"
    assert agents[0].birth_year == 1950
    assert any(i.value == "0000 0001 2345 6789" for i in idents)


def test_batch_accumulator_merge_payload() -> None:
    acc = _WEMIBatchAccumulator(source_name="test")
    p1 = {
        "record_count": 2,
        "works": [WorkEntity(source="s", source_id="w1", title="W1", title_normalized="w1")],
        "agents": [AgentEntity(source="s", source_id="a1", name="A1", name_normalized="a1")],
        "manifestations": [],
    }
    p2 = {
        "record_count": 3,
        "works": [WorkEntity(source="s", source_id="w2", title="W2", title_normalized="w2")],
        "manifestations": [
            ManifestationEntity(
                source="s",
                source_id="m1",
                title="M1",
                title_normalized="m1",
                manifestation_uri="urn:manif:s:m1",
            )
        ],
    }

    acc.merge_payload(p1)
    assert acc.pending == 2
    assert len(acc.works) == 1
    assert len(acc.agents) == 1

    acc.merge_payload(p2)
    assert acc.pending == 5
    assert len(acc.works) == 2
    assert len(acc.manifestations) == 1

    taken = acc.take()
    assert taken["record_count"] == 5
    assert len(taken["works"]) == 2
    assert len(taken["manifestations"]) == 1
    assert len(taken["agents"]) == 1
    assert acc.pending == 0
