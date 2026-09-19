"""Stage 7 of plans/aggregates.md: BIBFRAME aggregates.

`bf:hasPart` on a Work is the containment relation — an anthology and its
stories, an album and its tracks. All three shipped schema profiles mapped it to
`has_series_member`, so every BIBFRAME aggregate was recorded as a series; and
the relationship extractor named neither endpoint, so no edge it produced could
be stored at all.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from rdflib import Graph

from reconciliator.constituents import resolve_for_source
from reconciliator.indexer.torch_engine import TorchVectorEngine
from reconciliator.loaders.base import LoadContext
from reconciliator.loaders.bibframe.profiles import get_profile
from reconciliator.loaders.registry import load_record
from reconciliator.database import WEMIRepository

SRC = "bffi"

ANTHOLOGY = """
@prefix bf: <http://id.loc.gov/ontologies/bibframe/> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
<http://example.org/w1> a bf:Work ;
    bf:title [ a bf:Title ; bf:mainTitle "Suomalaisia novelleja" ] ;
    bf:hasPart <http://example.org/p1>, <http://example.org/p2> ;
    bf:partOf <http://example.org/series1> .
<http://example.org/p1> a bf:Work ;
    bf:title [ a bf:Title ; bf:mainTitle "Kuun maisema" ] ;
    bf:contribution [ a bf:Contribution ;
        bf:agent [ a bf:Person ; rdfs:label "Waltari, Mika" ] ] .
<http://example.org/p2> a bf:Work ;
    bf:title [ a bf:Title ; bf:mainTitle "Agnes" ] .
<http://example.org/series1> a bf:Series ;
    bf:title [ a bf:Title ; bf:mainTitle "Sarja" ] .
<http://example.org/i1> a bf:Instance ; bf:instanceOf <http://example.org/w1> ;
    bf:title [ a bf:Title ; bf:mainTitle "Suomalaisia novelleja" ] .
"""

#: A part that is a blank node, which is the common shape in a real export.
BLANK_PART = """
@prefix bf: <http://id.loc.gov/ontologies/bibframe/> .
<http://example.org/w2> a bf:Work ;
    bf:title [ a bf:Title ; bf:mainTitle "Kokoelma" ] ;
    bf:hasPart [ a bf:Work ; bf:title [ a bf:Title ; bf:mainTitle "Nimeton osa" ] ] .
<http://example.org/i2> a bf:Instance ; bf:instanceOf <http://example.org/w2> ;
    bf:title [ a bf:Title ; bf:mainTitle "Kokoelma" ] .
"""


def _ctx(mode: str = "entities", profile: str | None = "bibframe_haspart") -> LoadContext:
    return LoadContext(
        source=SRC,
        record_origin=SRC,
        constituents=resolve_for_source(SRC, profile, mode),
    )


def _load(ttl: str, mode: str = "entities"):
    return load_record(Graph().parse(data=ttl, format="turtle"), _ctx(mode))


@pytest.fixture
def repo(tmp_path: Path) -> WEMIRepository:
    store = WEMIRepository(
        db_path=tmp_path / "authority.db",
        dimension=4,
        work_indexer=TorchVectorEngine(tmp_path / "work.pt", dimension=4),
        manifestation_indexer=TorchVectorEngine(tmp_path / "manif.pt", dimension=4),
    )
    store.register_source(SRC, is_authoritative=True)
    return store


# --- the mis-mapping ------------------------------------------------------


@pytest.mark.parametrize("name", ["bffi_1", "bibframe_2", "bibframe_3"])
def test_part_predicates_mean_containment_in_every_profile(name: str):
    profile = get_profile(name)
    assert profile.relationship_predicates["bf:hasPart"] == "has_constituent"
    assert profile.relationship_predicates["bf:partOf"] == "constituent_of"
    # Series membership keeps its own predicate, and the classes that mark the
    # other end as a series are declared.
    assert profile.relationship_predicates["bf:hasSeries"] == "series_member_of"
    assert "bf:Series" in profile.series_classes


def test_an_aggregate_is_no_longer_recorded_as_a_series():
    edges = {(r.relationship_type, r.target_work_uri) for r in _load(ANTHOLOGY).relationships}
    assert ("has_constituent", f"{SRC}:http://example.org/p1") in edges
    assert ("has_constituent", f"{SRC}:http://example.org/p2") in edges
    assert not any(kind == "has_series_member" for kind, _ in edges)


def test_a_part_of_pointing_at_a_series_is_still_series_membership():
    """The same predicate means containment on a Work and membership on a
    Series, so the distinction is the *other end's* type."""
    edges = {(r.relationship_type, r.target_work_uri) for r in _load(ANTHOLOGY).relationships}
    assert ("series_member_of", f"{SRC}:http://example.org/series1") in edges


def test_both_endpoints_are_named():
    """They used to be left at id 0 with no URI, so every edge violated the
    foreign key on its target and the batch failed."""
    for rel in _load(ANTHOLOGY).relationships:
        assert rel.source_work_uri == f"{SRC}:http://example.org/w1"
        assert rel.target_work_uri


def test_an_endpoint_uses_the_uri_the_work_is_stored_under():
    """Computed any other way it names a row that does not exist, which is how
    the first attempt produced `urn:work:bffi:http://example.org/w1` for a work
    stored as `http://example.org/w1`."""
    record = _load(ANTHOLOGY)
    stored = {w.work_uri for w in record.works}
    assert record.relationships[0].source_work_uri in stored


def test_two_parts_are_two_edges(repo: WEMIRepository):
    """The decomposer deduplicated edges on `(type, source_id, target_id)`, and
    those are all 0 before binding, so two parts collapsed into one edge. It was
    invisible while the edges named no target."""
    record = _load(ANTHOLOGY)
    repo.insert_wemi_batch(
        works=record.works,
        expressions=record.expressions,
        manifestations=record.manifestations,
        relationships=record.relationships,
        manifestation_expressions=record.associations,
        agents=record.agents,
        contributions=record.contributions,
        entity_keys=record.entity_keys,
        record_origin=SRC,
    )
    container = repo.get_work_by_source(SRC, "http://example.org/w1")
    kinds = [
        e.relationship_type
        for e in repo.get_work_relationships(container.id)
        if e.relationship_type == "has_constituent"
    ]
    assert len(kinds) == 2


def test_the_same_edge_is_not_stated_twice():
    """`bf:hasPart` is both where the parts are found and a relationship the
    decomposer reads, so one fact arrives from two directions."""
    record = _load(ANTHOLOGY)
    keys = [
        (r.relationship_type, r.source_work_uri, r.target_work_uri) for r in record.relationships
    ]
    assert len(keys) == len(set(keys))


# --- the parts themselves -------------------------------------------------


def test_parts_become_works_with_their_own_uris():
    """A part is a node with a URI of its own, and the graph saying so is better
    evidence than a digest of its title."""
    record = _load(ANTHOLOGY)
    assert [(w.source_id, w.title) for w in record.works] == [
        ("http://example.org/w1", "Suomalaisia novelleja"),
        ("http://example.org/p1", "Kuun maisema"),
        ("http://example.org/p2", "Agnes"),
    ]


def test_a_part_title_is_read_from_behind_its_title_node():
    """`bf:title` points at a Title node whose string sits behind
    `bf:mainTitle`, so reading the predicate alone yields a node."""
    titles = [w.title for w in _load(ANTHOLOGY).works]
    assert "Kuun maisema" in titles


def test_a_parts_creator_is_read_by_the_dialects_own_rules():
    record = _load(ANTHOLOGY)
    part = next(w for w in record.works if w.source_id == "http://example.org/p1")
    # The creator is used for identity and serialisation; the part keeps its own
    # node URI either way.
    assert part.work_uri == f"{SRC}:http://example.org/p1"


def test_a_blank_node_part_falls_back_to_a_content_digest():
    record = _load(BLANK_PART)
    part = next(w for w in record.works if w.title == "Nimeton osa")
    assert part.source_id.startswith("c:")
    assert part.work_uri == f"{SRC}:{part.source_id}"


def test_parts_are_embodied_in_the_containers_manifestation():
    record = _load(ANTHOLOGY)
    assert len(record.manifestations) == 1
    assert len(record.associations) == 3  # the container's own plus two parts


def test_keys_only_writes_no_part_rows():
    record = _load(ANTHOLOGY, mode="keys_only")
    assert [w.title for w in record.works] == ["Suomalaisia novelleja"]
    # The relationships the decomposer reads are not gated by the mode: they are
    # what the graph says, and they were always meant to be stored.
    assert any(r.relationship_type == "has_constituent" for r in record.relationships)


def test_a_source_with_no_profile_reads_no_parts():
    record = load_record(Graph().parse(data=ANTHOLOGY, format="turtle"), _ctx(profile=None))
    assert [w.title for w in record.works] == ["Suomalaisia novelleja"]
