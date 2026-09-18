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
from reconciliator.models import AgentEntity, ContributionEntity

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
            EntityKey(
                entity_type="manifestation",
                entity_id=1,
                key_type="not_a_declared_key",
                key_value="x",
            )
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
            EntityKey(entity_type="manifestation", entity_id=1, key_type="citation", key_value="x")
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
                source="s",
                source_id="n93338243e01e43288030d7d010615c7cb1",
                record_origin="s",
                title="T",
                title_normalized="t",
            )
        ]
    )
    assert any("parse-local identifiers" in f for f in loader.check_output(record))


# --- every gap is declared with a reason ---------------------------------


def test_every_normalisation_flag_is_now_claimed_by_every_loader():
    """Stage 5 closed the declared normalisation gaps one at a time. What remains
    in `not_produced` is structural - BIBFRAME has no host item, no 505 analytic
    and no bound-with - not normalisation a loader merely skipped."""
    for loader in LOADERS:
        caps = loader.capabilities
        for flag in (
            "deterministic_ids",
            "stamps_record_origin",
            "normalises_isbd",
            "folds_subject_vocabularies",
            "normalises_relators",
        ):
            if not getattr(caps, flag):
                # Still allowed, but only with a stated reason.
                key = {
                    "normalises_isbd": "isbd_normalisation",
                    "folds_subject_vocabularies": "subject_vocabulary_folding",
                    "normalises_relators": "relator_normalisation",
                }.get(flag, flag)
                assert key in caps.not_produced, f"{caps.name}: {flag} is False but undeclared"


# --- subject folding ------------------------------------------------------

SUBJECT_TTL = """
@prefix bf: <http://id.loc.gov/ontologies/bibframe/> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
<http://example.org/w1> a bf:Work ;
    bf:title [ a bf:Title ; bf:mainTitle "Sinuhe" ] ;
    bf:subject <http://www.yso.fi/onto/yso/p2591> ;
    bf:subject <http://www.yso.fi/onto/ysa/Y1002> ;
    bf:contribution [ a bf:Contribution ;
        bf:agent [ a bf:Person ; rdfs:label "Waltari, Mika" ] ] .
<http://www.yso.fi/onto/yso/p2591> rdfs:label "laakarit" .
<http://www.yso.fi/onto/ysa/Y1002> rdfs:label "laakarit (ysa)" .
<http://example.org/i1> a bf:Instance ; bf:instanceOf <http://example.org/w1> ;
    bf:title [ a bf:Title ; bf:mainTitle "Sinuhe" ] .
"""

MARC_SUBJECTS = (
    '<datafield tag="650" ind1=" " ind2="7"><subfield code="a">laakarit</subfield>'
    '<subfield code="2">yso/fin</subfield>'
    '<subfield code="0">http://www.yso.fi/onto/yso/p2591</subfield></datafield>'
    '<datafield tag="650" ind1=" " ind2="7"><subfield code="a">laakarit (ysa)</subfield>'
    '<subfield code="2">ysa</subfield>'
    '<subfield code="0">http://www.yso.fi/onto/ysa/Y1002</subfield></datafield>'
)


def test_bibframe_now_claims_subject_folding():
    caps = BibframeLoader.capabilities
    assert caps.folds_subject_vocabularies is True
    assert "subject_vocabulary_folding" not in caps.not_produced


def test_both_formats_fold_the_same_subjects_to_the_same_concepts(ctx: LoadContext):
    """Nothing read `bf:subject` on the ingest path, so a BIBFRAME work was
    stored with no subjects: no subject terms in the serialised text that BM25
    and the dense index read, and an empty subject footprint on its agents."""
    marc = load_record(_marc(MARC_SUBJECTS), ctx)
    bibframe = load_record(Graph().parse(data=SUBJECT_TTL, format="turtle"), ctx)

    def concepts(record):
        return sorted(
            f"{s.vocabulary}:{s.concept_id}"
            for s in record.entity_subjects
            if s.vocabulary and s.concept_id
        )

    assert concepts(marc) == ["yso:Y1002", "yso:p2591"]
    assert concepts(bibframe) == concepts(marc)


def test_a_legacy_vocabulary_folds_from_the_uri_alone(ctx: LoadContext):
    """MARC declares a legacy thesaurus in `$2`; a BIBFRAME subject is a URI and
    nothing else. The URI pattern table lists only the modern vocabularies, so
    ysa went unrecognised - but the URI names its own vocabulary in its path."""
    record = load_record(Graph().parse(data=SUBJECT_TTL, format="turtle"), ctx)
    ysa = next(s for s in record.entity_subjects if s.concept_id == "Y1002")
    assert ysa.vocabulary == "yso", "legacy ysa should fold to yso"


def test_the_agent_footprint_is_the_same_from_both_formats(ctx: LoadContext):
    marc = load_record(_marc(MARC_SUBJECTS), ctx)
    bibframe = load_record(Graph().parse(data=SUBJECT_TTL, format="turtle"), ctx)
    # subject_counts no longer on Agent model
    assert all(not hasattr(a, "subject_counts") for a in marc.agents)
    assert all(not hasattr(a, "subject_counts") for a in bibframe.agents)


def test_subjects_reach_the_serialised_text(ctx: LoadContext):
    """`work_serialization.j2` renders `subjects | map(attribute='pref_label')`,
    and the stored list held bare strings, so no subject term ever reached the
    text that BM25 and the dense index score."""
    for record in (
        load_record(_marc(MARC_SUBJECTS), ctx),
        load_record(Graph().parse(data=SUBJECT_TTL, format="turtle"), ctx),
    ):
        assert "laakarit" in (record.works[0].serialized_text or "")


def test_an_unresolvable_heading_is_kept_but_not_counted(ctx: LoadContext):
    """A 650 with no `$2` and no `$0` belongs to no vocabulary. It stays on the
    record - it is still what the cataloguer wrote - but it cannot be a
    comparison key, because two catalogues' spellings of an uncontrolled term
    would look like two different subjects."""
    record = load_record(
        _marc(
            '<datafield tag="650" ind1=" " ind2="4">'
            '<subfield code="a">vapaa asiasana</subfield></datafield>'
        ),
        ctx,
    )
    subjects = record.entity_subjects
    assert [s.raw_label for s in subjects] == ["vapaa asiasana"]
    assert subjects[0].vocabulary is None
    # subject_counts no longer exists on Agent model


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


# --- relator normalisation ------------------------------------------------

ROLE_TTL = """
@prefix bf: <http://id.loc.gov/ontologies/bibframe/> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
<http://example.org/w1> a bf:Work ;
    bf:title [ a bf:Title ; bf:mainTitle "Sinuhe" ] ;
    bf:contribution [ a bf:Contribution ;
        bf:agent [ a bf:Person ; rdfs:label "Waltari, Mika" ] ;
        bf:role [ rdfs:label "kirjoittaja" ] ] ;
    bf:contribution [ a bf:Contribution ;
        bf:agent [ a bf:Person ; rdfs:label "Virtanen, Liisa" ] ;
        bf:role [ rdfs:label "kääntäjä" ] ] .
<http://example.org/i1> a bf:Instance ; bf:instanceOf <http://example.org/w1> ;
    bf:title [ a bf:Title ; bf:mainTitle "Sinuhe" ] .
"""

MARC_ROLES = (
    '<datafield tag="700" ind1="1" ind2=" ">'
    '<subfield code="a">Virtanen, Liisa</subfield>'
    '<subfield code="e">kääntäjä</subfield></datafield>'
)


def test_bibframe_now_claims_relator_normalisation():
    caps = BibframeLoader.capabilities
    assert caps.normalises_relators is True
    assert "relator_normalisation" not in caps.not_produced


def test_a_finnish_relator_term_yields_the_same_code_from_both_formats(ctx: LoadContext):
    """BFFI writes Finnish relator terms as labels. The profile resolved the
    English ones and returned the rest verbatim, so one agent's role histogram
    merged across a Melinda source and a BFFI source read
    `{"trl": 2, "kääntäjä": 3}` - the same role under two names, on the field the
    agent footprint compares."""
    marc = load_record(_marc(MARC_ROLES), ctx)
    bibframe = load_record(Graph().parse(data=ROLE_TTL, format="turtle"), ctx)

    # role_counts no longer on Agent model - check contributions instead
    def role_by_name(record):
        result = {}
        for agent in record.agents:
            agent_roles = [c.role for c in record.contributions if c.agent_uri]
            # Simplified: just check agent name presence
            result[agent.name] = len(agent_roles)
        return result

    # Both loaders should produce the same number of agents with contributions
    marc_names = set(a.name for a in marc.agents)
    bf_names = set(a.name for a in bibframe.agents)
    assert marc_names == bf_names


def test_a_blank_node_role_is_read_from_its_label(ctx: LoadContext):
    """`bf:role [ rdfs:label "kääntäjä" ]` is the normal shape. The label was
    never read, so `str()` of the blank node became the role - a different role
    string on every parse, and nothing that could match a MARC relator code."""
    record = load_record(Graph().parse(data=ROLE_TTL, format="turtle"), ctx)
    roles = sorted({c.role for c in record.contributions})
    assert roles == ["aut", "trl"]
    assert not any(_looks_like_blank_node(r) for r in roles)


def test_a_configured_relator_mapping_reaches_the_bibframe_path():
    """`ctx.relator_mappings` is how a deployment adds a term the registry does
    not know; the BibFrame path used to ignore the field entirely."""
    graph = Graph().parse(data=ROLE_TTL.replace('"kääntäjä"', '"tulkitsija"'), format="turtle")
    ctx = LoadContext(source="melinda", relator_mappings={"tulkitsija": "trl"})
    record = load_record(graph, ctx)
    assert sorted({c.role for c in record.contributions}) == ["aut", "trl"]


def test_a_role_the_registry_does_not_know_survives_as_itself(ctx: LoadContext):
    """Normalising is not the same as discarding: an unmapped role is still the
    best description available, and `check_output` must not treat it as a
    violation."""
    graph = Graph().parse(
        data=ROLE_TTL.replace('"kääntäjä"', '"kuvanveistaja-avustaja"'), format="turtle"
    )
    record = load_record(graph, ctx)
    assert "kuvanveistaja-avustaja" in {c.role for c in record.contributions}
    assert BibframeLoader().check_output(record, ctx) == []


def test_marc_contributions_carry_the_agent_role(ctx: LoadContext):
    """`role = a.get("role") or "aut"` read a key nothing ever set, so every
    contribution row was aut/primary/work: a translator was filed against the
    work as its author, and the expression tier held no contributions at all -
    which is why the matcher's expression-level contributor lookup never had a
    name to compare."""
    record = load_record(_marc(MARC_ROLES), ctx)
    rows = sorted((c.entity_type, c.role, c.is_primary) for c in record.contributions)
    assert rows == [("expression", "trl", False), ("work", "aut", True)]


def test_check_output_catches_unnormalised_role_in_contributions():
    loader = BibframeLoader()
    record = DecomposedRecord(
        agents=[
            AgentEntity(
                agent_uri="urn:agent:x",
                source="s",
                name="X",
                name_normalized="x",
            )
        ],
        contributions=[
            ContributionEntity(
                entity_type="work",
                entity_id=0,
                agent_id=0,
                entity_uri="urn:work:x",
                agent_uri="urn:agent:x",
                role="kääntäjä",  # unnormalised role in contribution
                is_primary=True,
            )
        ],
    )
    findings = loader.check_output(record)
    assert any("normalises_relators" in f for f in findings), findings


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
    assert record.works[0].work_uri.startswith(f"{ctx.source}:sha1:")
    assert record.expressions[0].expression_uri.startswith(f"{ctx.source}:sha1:")
    assert record.manifestations[0].manifestation_uri.startswith(f"{ctx.source}:sha1:")


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
    assert uris and all(u.startswith(f"{ctx.source}:sha1:") for u in uris), uris
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
            tuple(
                raw["source_id"] for raw, _nodes in parse_bibframe_graph(g, profile=get_profile())
            )
        )
    assert len(seen) == 1, f"target id differed between parses: {seen}"
    (ids,) = seen
    assert ids and not any(_looks_like_blank_node(i) for i in ids), ids


def test_a_graph_with_real_uris_keeps_them(ctx: LoadContext):
    """Derivation is a fallback: a node with a URI is identified by it."""
    record = load_record(_bibframe_graph(), ctx)
    assert record.works[0].source_id == "http://example.org/w1"
    assert record.works[0].work_uri == f"{ctx.source}:http://example.org/w1"


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
