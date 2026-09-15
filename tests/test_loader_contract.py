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


def test_bibframe_declares_the_normalisation_it_still_does_not_apply():
    caps = BibframeLoader.capabilities
    for flag, key in (
        ("folds_subject_vocabularies", "subject_vocabulary_folding"),
        ("normalises_relators", "relator_normalisation"),
    ):
        assert getattr(caps, flag) is False, flag
        assert key in caps.not_produced, f"{flag} is False but undeclared"


def test_bibframe_now_claims_deterministic_ids():
    """Closed in stage 5. The claim is verified by the tests below and by
    `check_output`, which rejects a parse-local identifier under this claim."""
    caps = BibframeLoader.capabilities
    assert caps.deterministic_ids is True
    assert "deterministic_ids" not in caps.not_produced


def test_every_loader_now_normalises_isbd(ctx: LoadContext):
    """Closed in stage 5 by moving the strip into `RecordLoader.finalise`, so it
    is a property of the interface rather than of each decomposer."""
    for loader in LOADERS:
        assert loader.capabilities.normalises_isbd is True, loader.capabilities.name
        assert "isbd_normalisation" not in loader.capabilities.not_produced


def test_isbd_punctuation_is_stripped_on_the_bibframe_path(ctx: LoadContext):
    """The toggle existed before stage 5 but reached only the MARC decomposers,
    so one catalogue's "Waltari, Mika" and another's "Waltari, Mika," were two
    agents in every report that groups by name."""
    record = load_record(_isbd_graph(), ctx)
    assert record.works[0].title == "Sinuhe egyptilainen"
    assert record.manifestations[0].title == "Sinuhe egyptilainen"
    assert record.manifestations[0].publisher == "WSOY"
    assert record.manifestations[0].publication_place == "Helsinki"
    assert [a.name for a in record.agents] == ["Waltari, Mika"]


def test_isbd_stripping_obeys_the_toggle():
    off = LoadContext(source="melinda", strip_isbd_punctuation=False)
    record = load_record(_isbd_graph(), off)
    assert record.works[0].title == "Sinuhe egyptilainen /"
    # And the declaration means "honours the toggle", not "always strips", so
    # surviving punctuation is not a contract violation here.
    assert BibframeLoader().check_output(record, off) == []


def test_the_marc_and_target_paths_agree_on_a_title(ctx: LoadContext):
    """The target file builds a MatchRecord and no entities, so it passes through
    no loader. Stripping the 245 where it is read - rather than only in
    `finalise` - keeps the two sides comparable: a work report that prints the
    target title beside the matched corpus title showed "Testikirja :" against
    "Testikirja" for what was one and the same string in both catalogues."""
    from reconciliator.loaders.marcxml import build_target_record_from_marc

    elem = _marc(
        '<datafield tag="245" ind1="1" ind2="0">'
        '<subfield code="a">Testikirja :</subfield>'
        '<subfield code="b">alaotsikko</subfield></datafield>'
    )
    # The fixture's own 245 comes first; replace it so only the ISBD one is read.
    for f in elem.findall(f"{MARC_NS}datafield[@tag='245']")[:1]:
        elem.remove(f)

    target = build_target_record_from_marc(elem, source="target")
    corpus = load_record(elem, ctx)
    assert target.title == "Testikirja"
    assert corpus.works[0].title == target.title
    assert corpus.manifestations[0].title == target.title


def test_isbd_stripping_never_empties_a_required_title(ctx: LoadContext):
    """`title` is a required str on three tiers: a heading that is punctuation
    all the way down is kept as it stands, because an empty title destroys the
    only evidence that the source record was odd."""
    record = load_record(_isbd_graph(title="..."), ctx)
    assert record.works[0].title == "..."


ISBD_TTL = """
@prefix bf: <http://id.loc.gov/ontologies/bibframe/> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
<http://example.org/w1> a bf:Work ;
    bf:title [ a bf:Title ; bf:mainTitle "%(t)s" ] ;
    bf:contribution [ a bf:Contribution ;
        bf:agent [ a bf:Person ; rdfs:label "Waltari, Mika," ] ] ;
    bf:hasInstance <http://example.org/i1> .
<http://example.org/i1> a bf:Instance ; bf:instanceOf <http://example.org/w1> ;
    bf:title [ a bf:Title ; bf:mainTitle "%(t)s" ] ;
    bf:provisionActivity [ a bf:Publication ;
        bf:agent [ rdfs:label "WSOY," ] ;
        bf:place [ rdfs:label "Helsinki :" ] ] .
"""


def _isbd_graph(title: str = "Sinuhe egyptilainen /") -> Graph:
    return Graph().parse(data=ISBD_TTL % {"t": title}, format="turtle")


# --- BibFrame identity ----------------------------------------------------

BLANK_NODE_TTL = """
@prefix bf: <http://id.loc.gov/ontologies/bibframe/> .
_:w a bf:Work ; bf:title [ a bf:Title ; bf:mainTitle "%s" ] .
_:i a bf:Instance ; bf:instanceOf _:w ;
    bf:title [ a bf:Title ; bf:mainTitle "%s" ] .
"""


def _blank_node_graph(title: str = "Sinuhe egyptilainen") -> Graph:
    return Graph().parse(data=BLANK_NODE_TTL % (title, title), format="turtle")


def test_blank_node_records_get_stable_identity_across_parses(ctx: LoadContext):
    """rdflib mints a fresh label per parse, so identity taken from the label
    changed on every ingest - new URIs, no upsert match, duplicate rows, and an
    `ingest_mode: replace` that could not help because the ids differed."""
    seen = set()
    for _ in range(3):
        record = load_record(_blank_node_graph(), ctx)
        seen.add(
            (
                record.works[0].work_uri,
                record.expressions[0].expression_uri,
                record.manifestations[0].manifestation_uri,
            )
        )
    assert len(seen) == 1, f"identity differed between parses: {seen}"


def test_blank_node_identity_follows_content(ctx: LoadContext):
    a = load_record(_blank_node_graph("Sinuhe egyptilainen"), ctx).works[0].work_uri
    b = load_record(_blank_node_graph("Turms kuolematon"), ctx).works[0].work_uri
    assert a != b


def test_blank_node_uris_follow_the_same_scheme_as_marc(ctx: LoadContext):
    """A derived id must still yield a real URI, not a bare parse label."""
    record = load_record(_blank_node_graph(), ctx)
    assert record.works[0].work_uri.startswith("urn:work:melinda:")
    assert record.expressions[0].expression_uri.startswith("urn:exp:melinda:")
    assert record.manifestations[0].manifestation_uri.startswith("urn:manif:melinda:")


AGENT_BNODE_TTL = """
@prefix bf: <http://id.loc.gov/ontologies/bibframe/> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
_:w a bf:Work ; bf:title [ a bf:Title ; bf:mainTitle "Sinuhe" ] ;
    bf:contribution [ a bf:Contribution ;
        bf:agent [ a bf:Person ; rdfs:label "Waltari, Mika" ] ] .
_:i a bf:Instance ; bf:instanceOf _:w ;
    bf:title [ a bf:Title ; bf:mainTitle "Sinuhe" ] .
"""


def test_a_blank_node_agent_gets_stable_identity(ctx: LoadContext):
    """BFFI puts the agent inside the contribution rather than in an authority
    file, so the agent is usually a blank node. The `_:`-prefix guard here was
    the twelfth of its kind and was missed by the first sweep: the agent_uri was
    a parse-local label, so re-ingesting one file created a new agent row for the
    same person every time."""
    seen = set()
    for _ in range(3):
        g = Graph().parse(data=AGENT_BNODE_TTL, format="turtle")
        record = load_record(g, ctx)
        seen.add(tuple(a.agent_uri for a in record.agents))
    assert len(seen) == 1, f"agent identity differed between parses: {seen}"
    (uris,) = seen
    assert uris and all(u.startswith("urn:agent:melinda:") for u in uris), uris
    assert not any(_looks_like_blank_node(u) for u in uris)


def test_a_blank_node_instance_gets_a_stable_target_id():
    """The same defect on the target side: `parse_bibframe_graph` took the record
    id from the blank node's label, so the same file produced different target
    ids run to run - and a reconciliation report keyed by them was not
    comparable between runs."""
    from reconciliator.loaders.bibframe import get_profile, parse_bibframe_graph

    seen = set()
    for _ in range(3):
        g = Graph().parse(data=AGENT_BNODE_TTL, format="turtle")
        seen.add(
            tuple(raw["source_id"] for raw, _nodes in parse_bibframe_graph(g, profile=get_profile()))
        )
    assert len(seen) == 1, f"target id differed between parses: {seen}"
    (ids,) = seen
    assert ids and not any(_looks_like_blank_node(i) for i in ids), ids


def test_a_graph_with_real_uris_keeps_them(ctx: LoadContext):
    """Derivation is a fallback: a node with a URI is identified by it."""
    record = load_record(_bibframe_graph(), ctx)
    assert record.works[0].work_uri.startswith("http://example.org/")


def test_str_of_a_blank_node_has_no_prefix():
    """Why every `startswith("_:")` guard in the decomposer was dead code.

    Only `.n3()` adds the prefix, so the guards never fired and parse-local
    labels were used as work, expression and manifestation URIs.
    """
    from rdflib import BNode

    from reconciliator.loaders.bibframe.decomposition import is_blank_node

    node = BNode()
    assert not str(node).startswith("_:")
    assert node.n3().startswith("_:")
    assert is_blank_node(node) is True


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
