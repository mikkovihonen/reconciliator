"""Tests for derived domain blocking keys persisted in `entity_keys`.

`entity_keys` sits beside `identifiers`: that table holds externally-meaningful
standard numbers, this one holds the keys the clusterer blocks on and the funnels
dispatch on. Both are derived once at ingest rather than by re-parsing the record
payload at match time.
"""

from pathlib import Path

import pytest
from lxml import etree

from reconciliator.indexer import TorchVectorEngine
from reconciliator.loaders.marcxml import (
    decode_analytic,
    decompose_marc_wemi,
    derive_domain_keys,
    encode_analytic,
    extract_marc_analytics,
)
from reconciliator.models import EntityKey
from reconciliator.repository import WEMIRepository

MARC_NS = "http://www.loc.gov/MARC21/slim"


@pytest.fixture
def repo(tmp_path: Path) -> WEMIRepository:
    return WEMIRepository(
        db_path=tmp_path / "authority.db",
        dimension=4,
        work_indexer=TorchVectorEngine(tmp_path / "work.pt", dimension=4),
        manifestation_indexer=TorchVectorEngine(tmp_path / "manif.pt", dimension=4),
    )


def _record(
    control: str, title: str, author: str = "Waltari, Mika", extra: str = ""
) -> etree._Element:
    return etree.fromstring(
        f"""<record xmlns="{MARC_NS}">
            <controlfield tag="001">{control}</controlfield>
            <datafield tag="100" ind1="1" ind2=" "><subfield code="a">{author}</subfield></datafield>
            <datafield tag="245" ind1="1" ind2="0"><subfield code="a">{title}</subfield></datafield>
            {extra}
        </record>""".encode()
    )


# --- analytics encoding ---------------------------------------------------


def test_analytic_encoding_round_trips():
    assert decode_analytic(encode_analytic("Finlandia", "Sibelius, Jean")) == (
        "Finlandia",
        "Sibelius, Jean",
    )


def test_analytic_encoding_round_trips_without_creator():
    assert decode_analytic(encode_analytic("Valse triste", None)) == ("Valse triste", None)


def test_analytic_encoding_survives_stripping():
    """Regression guard: a U+001F separator was eaten by str.strip(), which counts
    U+001C-U+001F as whitespace. The encoding must survive the same treatment."""
    encoded = encode_analytic("A: b / c; d", "X, Y")
    assert encoded.strip() == encoded
    assert decode_analytic(encoded.strip()) == ("A: b / c; d", "X, Y")


def test_analytic_encoding_tolerates_punctuation_heavy_titles():
    for title in ("Quo vadis?", 'A "quoted" title', "Title: subtitle / author", "Ääni"):
        assert decode_analytic(encode_analytic(title, None)) == (title, None)


def test_extract_marc_analytics_reads_700_710_and_505():
    rec = _record(
        "R1",
        "Kokoelma",
        extra="""
        <datafield tag="700" ind1="1" ind2="2"><subfield code="a">Sibelius, Jean</subfield><subfield code="t">Finlandia</subfield></datafield>
        <datafield tag="710" ind1="2" ind2=" "><subfield code="a">Suomen Kansallisooppera</subfield><subfield code="t">Ohjelmisto</subfield></datafield>
        <datafield tag="505" ind1="0" ind2="0"><subfield code="t">Karelia-sarja</subfield><subfield code="t">Valse triste</subfield></datafield>
        """,
    )
    assert extract_marc_analytics(rec) == [
        ("Finlandia", "Sibelius, Jean"),
        ("Ohjelmisto", "Suomen Kansallisooppera"),
        ("Karelia-sarja", None),
        ("Valse triste", None),
    ]


# --- derivation -----------------------------------------------------------


def test_derive_domain_keys_emits_work_and_standard_keys():
    from reconciliator.loaders.marcxml import parse_marc_record_elem

    rec = _record(
        "R1",
        "Sinuhe egyptilainen",
        extra='<datafield tag="020" ind1=" " ind2=" "><subfield code="a">9789510123456</subfield></datafield>',
    )
    parsed = parse_marc_record_elem(rec, source="melinda")
    keys = derive_domain_keys(rec, parsed, "text", "waltari_mika", "sinuhe egyptilainen")
    assert keys["standard"] == ["9789510123456"]
    assert keys["work"] and keys["work"][0]


def test_derive_domain_keys_deduplicates_repeated_values():
    from reconciliator.loaders.marcxml import parse_marc_record_elem

    rec = _record(
        "R1",
        "Kokoelma",
        extra="""
        <datafield tag="505" ind1="0" ind2="0"><subfield code="t">Sama</subfield></datafield>
        <datafield tag="505" ind1="0" ind2="0"><subfield code="t">Sama</subfield></datafield>
        """,
    )
    parsed = parse_marc_record_elem(rec, source="melinda")
    keys = derive_domain_keys(rec, parsed, "text", "a", "b")
    assert keys["analytic"] == [encode_analytic("Sama", None)]


def test_key_sink_binds_keys_to_the_manifestation_uri():
    sink: list[EntityKey] = []
    _, _, manif, *_ = decompose_marc_wemi(_record("R1", "Sinuhe"), source="melinda", key_sink=sink)
    assert sink
    assert {k.entity_uri for k in sink} == {manif.manifestation_uri}
    assert {k.entity_type for k in sink} == {"manifestation"}


def test_decompose_without_sink_is_unchanged():
    """The sink is optional; omitting it must not alter the returned tuple."""
    assert len(decompose_marc_wemi(_record("R1", "Sinuhe"), source="melinda")) == 10


# --- persistence ----------------------------------------------------------


def _ingest(repo: WEMIRepository, control: str, title: str, source: str = "melinda"):
    sink: list[EntityKey] = []
    w, e, m, itms, agts, rels, idents, assoc, mops, contribs = decompose_marc_wemi(
        _record(control, title), source=source, key_sink=sink
    )
    repo.insert_wemi_batch(
        works=[w],
        expressions=[e],
        manifestations=[m],
        agents=agts,
        identifiers=idents,
        contributions=contribs,
        entity_keys=sink,
        record_origin=source,
    )
    return m


def test_keys_persist_and_read_back_as_a_bag(repo: WEMIRepository):
    manif = _ingest(repo, "R1", "Sinuhe egyptilainen")
    bag = repo.get_entity_keys("manifestation", manif.id)
    assert "work" in bag
    assert all(isinstance(v, list) for v in bag.values())


def test_multi_record_batch_does_not_collapse_keys_onto_the_first_row(repo: WEMIRepository):
    """Regression guard: positional binding once attributed a whole batch to row 0."""
    sink: list[EntityKey] = []
    works, exprs, manifs, idents = [], [], [], []
    for ctrl, title in (("R1", "Sinuhe egyptilainen"), ("R2", "Turms kuolematon")):
        w, e, m, _itms, _agts, _rels, ids, _assoc, _mops, _c = decompose_marc_wemi(
            _record(ctrl, title), source="melinda", key_sink=sink
        )
        works.append(w)
        exprs.append(e)
        manifs.append(m)
        idents.extend(ids)
    repo.insert_wemi_batch(
        works=works,
        expressions=exprs,
        manifestations=manifs,
        identifiers=idents,
        entity_keys=sink,
        record_origin="melinda",
    )

    bag1 = repo.get_entity_keys("manifestation", manifs[0].id)
    bag2 = repo.get_entity_keys("manifestation", manifs[1].id)
    assert bag1 and bag2
    assert bag1["work"] != bag2["work"], "distinct records must not share a work blocking key"


def test_reingesting_the_same_record_does_not_duplicate_keys(repo: WEMIRepository):
    manif = _ingest(repo, "R1", "Sinuhe egyptilainen")
    before = repo.get_entity_keys("manifestation", manif.id)
    _ingest(repo, "R1", "Sinuhe egyptilainen")
    assert repo.get_entity_keys("manifestation", manif.id) == before


def test_get_all_entity_keys_returns_every_record(repo: WEMIRepository):
    m1 = _ingest(repo, "R1", "Sinuhe egyptilainen")
    m2 = _ingest(repo, "R2", "Turms kuolematon")
    allkeys = repo.get_all_entity_keys("manifestation")
    assert {m1.id, m2.id} <= set(allkeys)


def test_find_entities_by_key_locates_the_owning_record(repo: WEMIRepository):
    manif = _ingest(repo, "R1", "Sinuhe egyptilainen")
    work_key = repo.get_entity_keys("manifestation", manif.id)["work"][0]
    assert repo.find_entities_by_key("work", work_key) == [manif.id]


def test_unbindable_keys_are_dropped_rather_than_orphaned(repo: WEMIRepository):
    """A key naming a URI absent from the batch must not persist with entity_id 0."""
    repo.insert_wemi_batch(
        works=[],
        expressions=[],
        manifestations=[],
        entity_keys=[
            EntityKey(
                entity_type="manifestation",
                entity_id=0,
                key_type="work",
                key_value="orphan",
                entity_uri="urn:manif:nowhere:X",
            )
        ],
    )
    assert repo.find_entities_by_key("work", "orphan") == []


# --- BibFrame parity ------------------------------------------------------


def test_bibframe_keys_derive_from_entities_not_the_graph():
    """The BibFrame side needs no sink: its keys come from decomposed entities."""
    from rdflib import Graph

    from reconciliator.loaders.bibframe.decomposition import (
        bibframe_entity_keys,
        decompose_bibframe_work,
    )

    ttl = """
    @prefix bf: <http://id.loc.gov/ontologies/bibframe/> .
    @prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
    <http://example.org/w1> a bf:Work ;
        bf:title [ a bf:Title ; bf:mainTitle "Sinuhe egyptilainen" ] .
    <http://example.org/i1> a bf:Instance ;
        bf:instanceOf <http://example.org/w1> ;
        bf:title [ a bf:Title ; bf:mainTitle "Sinuhe egyptilainen" ] .
    """
    g = Graph().parse(data=ttl, format="turtle")
    w, _e, m, _itms, agts, _rels, idents, *_ = decompose_bibframe_work(g, source="bf_source")
    keys = bibframe_entity_keys(m, w, agts, idents)
    assert keys, "BibFrame records must carry blocking keys too"
    assert {k.entity_uri for k in keys} == {m.manifestation_uri}
    assert any(k.key_type == "work" for k in keys)
