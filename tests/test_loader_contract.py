"""The loader contract: one shape, one options object, declared capabilities.

Three formats reach the datastore through `RecordLoader`. They do not extract
alike and are not expected to — merging their extraction was measured and
declined. What they must share is the contract, and what they must *state* is
every output they cannot produce. A silently omitted output is indistinguishable
from an impossible one, which is how the BibFrame path came to apply none of the
MARC path's normalisation without anything saying so.
"""

from __future__ import annotations

import pathlib

import pytest
from lxml import etree
from rdflib import Graph

from reconciliator.loaders.base import (
    DecomposedRecord,
    FormatAdapter,
    LoadContext,
    RecordLoader,
    _looks_like_blank_node,
)
from reconciliator.loaders.registry import (
    ADAPTERS,
    LOADERS,
    BibframeLoader,
    MarcAuthorityLoader,
    MarcBibliographicLoader,
    load_record,
    select_loader,
)

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
MARC_NS = "{http://www.loc.gov/MARC21/slim}"


def _marc(extra: str = "", leader: str = "00000cam a2200000 i 4500") -> etree._Element:
    return etree.fromstring(
        f"""<record xmlns="http://www.loc.gov/MARC21/slim">
            <leader>{leader}</leader>
            <controlfield tag="001">R1</controlfield>
            <datafield tag="100" ind1="1" ind2=" "><subfield code="a">Waltari, Mika</subfield></datafield>
            <datafield tag="245" ind1="1" ind2="0"><subfield code="a">Sinuhe egyptilainen</subfield></datafield>
            {extra}
        </record>""".encode()
    )


def _bibframe_graph() -> Graph:
    return Graph().parse(
        data="""
        @prefix bf: <http://id.loc.gov/ontologies/bibframe/> .
        <http://example.org/w1> a bf:Work ;
            bf:title [ a bf:Title ; bf:mainTitle "Sinuhe egyptilainen" ] .
        <http://example.org/i1> a bf:Instance ; bf:instanceOf <http://example.org/w1> ;
            bf:title [ a bf:Title ; bf:mainTitle "Sinuhe egyptilainen" ] .
        """,
        format="turtle",
    )


@pytest.fixture
def ctx() -> LoadContext:
    return LoadContext(source="melinda", record_origin="melinda")


# --- the contract ---------------------------------------------------------


def test_every_loader_declares_capabilities():
    for loader in LOADERS:
        assert loader.capabilities.name
        assert loader.capabilities.formats


def test_every_loader_returns_the_same_shape(ctx: LoadContext):
    """W/E/M are always lists. The authority decomposer returned lists while the
    other two returned singletons, so callers had to know which they held."""
    for item in (_marc(), _bibframe_graph()):
        record = load_record(item, ctx)
        assert isinstance(record, DecomposedRecord)
        for name in ("works", "expressions", "manifestations", "agents", "entity_keys"):
            assert isinstance(getattr(record, name), list), name


def test_dispatch_picks_the_right_loader():
    assert isinstance(select_loader(_marc()), MarcBibliographicLoader)
    assert isinstance(select_loader(_bibframe_graph()), BibframeLoader)
    authority = _marc(leader="00000nz  a2200000n  4500")
    assert isinstance(select_loader(authority), MarcAuthorityLoader)


def test_an_unrecognised_item_is_refused_rather_than_guessed():
    with pytest.raises(ValueError, match="no loader accepts"):
        select_loader(object())


def test_authority_dispatch_precedes_bibliographic():
    """Both accept an XML element; the authority test is the narrower one."""
    ids = [type(loader).__name__ for loader in LOADERS]
    assert ids.index("MarcAuthorityLoader") < ids.index("MarcBibliographicLoader")


# --- the guarantee the base class provides -------------------------------


@pytest.mark.parametrize("make_item", [_marc, _bibframe_graph])
def test_every_loader_stamps_record_origin(make_item, ctx: LoadContext):
    """The decomposers each guessed an origin and the insert overwrote it, so a
    caller inserting without `record_origin=` stored the wrong one - and origin
    plus the sources registry decides which rows may be matched against."""
    record = load_record(make_item(), ctx)
    for collection in (record.works, record.expressions, record.manifestations, record.agents):
        for entity in collection:
            assert entity.record_origin == "melinda"


def test_the_bibframe_decomposer_alone_does_not_stamp_origin(ctx: LoadContext):
    """Pins the gap the base class closes, so a regression is visible."""
    from reconciliator.loaders.bibframe import decompose_bibframe_work

    work, *_ = decompose_bibframe_work(g=_bibframe_graph(), source="melinda")
    assert work.record_origin == "unknown"
    assert load_record(_bibframe_graph(), ctx).works[0].record_origin == "melinda"


def test_load_context_falls_back_to_the_source_name():
    assert LoadContext(source="melinda").origin == "melinda"
    assert LoadContext(source="melinda", record_origin="other").origin == "other"
    assert LoadContext(source="").origin == "unknown"


# --- declarations checked against reality --------------------------------


@pytest.mark.parametrize("make_item", [_marc, _bibframe_graph])
def test_output_matches_the_declaration(make_item, ctx: LoadContext):
    """A declaration nothing verifies drifts the moment either side changes.

    This caught the MARC declaration omitting the `kanto` and `viaf` schemes that
    agent $0/$1 authority URIs produce.
    """
    loader = select_loader(make_item())
    record = loader.load(make_item(), ctx)
    assert loader.check_output(record) == []


def test_check_output_catches_an_undeclared_key():
    from reconciliator.models import EntityKey

    loader = MarcBibliographicLoader()
    record = DecomposedRecord(
        entity_keys=[
            EntityKey(entity_type="manifestation", entity_id=1,
                      key_type="not_a_declared_key", key_value="x")
        ]
    )
    findings = loader.check_output(record)
    assert any("undeclared key types" in f for f in findings)


def test_check_output_catches_a_contradicted_declaration():
    """Emitting something the loader says it does not produce."""
    from reconciliator.models import EntityKey

    loader = BibframeLoader()
    assert "citation" in loader.capabilities.not_produced
    record = DecomposedRecord(
        entity_keys=[
            EntityKey(entity_type="manifestation", entity_id=1,
                      key_type="citation", key_value="x")
        ]
    )
    assert any("does not produce" in f for f in loader.check_output(record))


def test_check_output_catches_an_unstable_id_under_a_determinism_claim():
    from reconciliator.models import WorkEntity

    loader = MarcBibliographicLoader()
    assert loader.capabilities.deterministic_ids is True
    record = DecomposedRecord(
        works=[
            WorkEntity(
                work_uri="n93338243e01e43288030d7d010615c7cb1", source="s",
                source_id="s1", record_origin="s", title="T", title_normalized="t",
            )
        ]
    )
    assert any("parse-local identifiers" in f for f in loader.check_output(record))


# --- every gap is declared with a reason ---------------------------------


def test_bibframe_declares_the_normalisation_it_does_not_apply():
    caps = BibframeLoader.capabilities
    for flag, key in (
        ("deterministic_ids", "deterministic_ids"),
        ("normalises_isbd", "isbd_normalisation"),
        ("folds_subject_vocabularies", "subject_vocabulary_folding"),
        ("normalises_relators", "relator_normalisation"),
    ):
        assert getattr(caps, flag) is False, flag
        assert key in caps.not_produced, f"{flag} is False but undeclared"


def test_the_authority_loader_declares_why_it_emits_no_keys():
    caps = MarcAuthorityLoader.capabilities
    assert caps.key_types == set()
    assert "entity_keys" in caps.not_produced


def test_every_declared_gap_carries_a_reason():
    """`not_produced` is the whole point: an absence without a reason is a bug."""
    for loader in LOADERS:
        for key, reason in loader.capabilities.not_produced.items():
            assert len(reason) > 20, f"{loader.capabilities.name}/{key} has no real reason"


def test_a_false_normalisation_flag_is_always_explained():
    """Any loader claiming not to normalise must say why, for every concern."""
    concerns = {
        "normalises_isbd": "isbd_normalisation",
        "folds_subject_vocabularies": "subject_vocabulary_folding",
        "normalises_relators": "relator_normalisation",
        "deterministic_ids": "deterministic_ids",
    }
    for loader in LOADERS:
        caps = loader.capabilities
        for flag, key in concerns.items():
            if getattr(caps, flag) is False:
                assert key in caps.not_produced, f"{caps.name}: {flag} unexplained"


# --- bibra is an adapter, not a loader -----------------------------------


def test_bibra_is_an_adapter_not_a_loader():
    """It converts to MARCXML and performs no decomposition, so it has no
    outputs to declare and does not belong in the loader registry."""
    assert all(not isinstance(a, RecordLoader) for a in ADAPTERS)
    assert all(isinstance(a, FormatAdapter) for a in ADAPTERS)
    assert not any(isinstance(loader, FormatAdapter) for loader in LOADERS)


# --- the blank-node heuristic --------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("_:w1", True),
        ("n93338243e01e43288030d7d010615c7cb1", True),
        ("http://example.org/w1", False),
        ("urn:work:melinda:R1", False),
        ("urn:work:melinda:(FI-MELINDA)000100201", False),
    ],
)
def test_blank_node_detection(value: str, expected: bool):
    assert _looks_like_blank_node(value) is expected
