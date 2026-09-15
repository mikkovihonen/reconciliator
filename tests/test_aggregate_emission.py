"""Stage 4 of plans/aggregates.md: constituents become rows.

An album track and an anthology story are Works realised as Expressions inside
the *container's* manifestation — no carrier of their own, so no manifestation
row — joined through `manifestation_expressions` with their track number, disc
and duration, and tied to the container by a `has_constituent` edge.

`mode: keys_only` must reproduce what ingest produced before any of this existed,
because that is what makes the change bisectable per source.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from lxml import etree

from reconciliator.constituents import resolve_for_source
from reconciliator.indexer.torch_engine import TorchVectorEngine
from reconciliator.loaders.base import DecomposedRecord, LoadContext
from reconciliator.loaders.registry import (
    BibframeLoader,
    MarcAuthorityLoader,
    MarcBibliographicLoader,
    load_record,
)
from reconciliator.repository import WEMIRepository
from reconciliator.template import WEMISerializationEngine

SRC = "fono"


def _album(
    control: str,
    title: str,
    tracks: list[tuple[str, str | None, str | None]],
    *,
    main_entry: str | None = "Rautavaara, Einojuhani",
) -> etree._Element:
    groups = ""
    for track_title, responsibility, duration in tracks:
        groups += f'<subfield code="t">{track_title}</subfield>'
        if responsibility:
            groups += f'<subfield code="r">{responsibility}</subfield>'
        if duration:
            groups += f'<subfield code="g">({duration})</subfield>'
    heading = (
        f'<datafield tag="100" ind1="1" ind2=" "><subfield code="a">{main_entry}</subfield>'
        f'<subfield code="e">saveltaja</subfield></datafield>'
        if main_entry
        else ""
    )
    return etree.fromstring(
        f"""<record xmlns="http://www.loc.gov/MARC21/slim">
          <leader>00000cjm a2200000 i 4500</leader>
          <controlfield tag="001">{control}</controlfield>
          <controlfield tag="007">sd fsngnnmmned</controlfield>
          <controlfield tag="008">200101s2020    fi |||||||||||||||fin d</controlfield>
          {heading}
          <datafield tag="245" ind1="1" ind2="0"><subfield code="a">{title}</subfield></datafield>
          <datafield tag="336" ind1=" " ind2=" "><subfield code="b">prm</subfield></datafield>
          <datafield tag="505" ind1="0" ind2="0">{groups}</datafield>
        </record>""".encode()
    )


ALBUM_A = _album(
    "ALB-1",
    "Cantus arcticus",
    [("Cantus arcticus", "Rautavaara", "18:20"), ("Angel of light", None, "31:05")],
)
#: A different release carrying one of the same works, its note naming no
#: composer and giving a slightly different timing.
ALBUM_B = _album(
    "ALB-2",
    "Rautavaara collection",
    [("Isle of bliss", None, "12:00"), ("Cantus arcticus", None, "18:25")],
)


def _ctx(mode: str, profile: str | None = "marc_enhanced") -> LoadContext:
    return LoadContext(
        source=SRC,
        record_origin=SRC,
        serialization_engine=WEMISerializationEngine(),
        constituents=resolve_for_source(SRC, profile, mode),
    )


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


def _ingest(repo: WEMIRepository, record: DecomposedRecord) -> None:
    repo.insert_wemi_batch(
        works=record.works,
        expressions=record.expressions,
        manifestations=record.manifestations,
        items=record.items,
        agents=record.agents,
        relationships=record.relationships,
        identifiers=record.identifiers,
        manifestation_expressions=record.associations,
        contributions=record.contributions,
        entity_keys=record.entity_keys,
        record_origin=SRC,
    )


def _junction(repo: WEMIRepository, manifestation_id: int) -> list[dict]:
    with repo.get_connection() as conn:
        return [
            dict(row)
            for row in conn.execute(
                """
                SELECT e.title, e.source_id, me.track_number, me.disc_number,
                       me.duration, me.is_primary
                FROM manifestation_expressions me
                JOIN expressions e ON e.id = me.expression_id
                WHERE me.manifestation_id = ?
                ORDER BY me.disc_number, me.track_number;
                """,
                (manifestation_id,),
            )
        ]


# --- the mode gate --------------------------------------------------------


def test_keys_only_reproduces_what_ingest_produced_before():
    """One work, one expression, one manifestation, one association, no edges —
    and the same blocking keys. This is what makes the change bisectable."""
    record = load_record(ALBUM_A, _ctx("keys_only"))
    assert (len(record.works), len(record.expressions), len(record.manifestations)) == (1, 1, 1)
    assert len(record.associations) == 1
    assert record.relationships == []
    assert [k.key_value for k in record.entity_keys if k.key_type == "analytic_title"] == [
        "Cantus arcticus",
        "Angel of light",
    ]


def test_a_source_with_no_profile_is_untouched():
    record = load_record(ALBUM_A, _ctx("entities", profile=None))
    assert len(record.works) == 1
    assert record.relationships == []


def test_mode_off_writes_no_rows_even_with_a_profile():
    record = load_record(ALBUM_A, _ctx("off"))
    assert len(record.works) == 1
    assert record.relationships == []


def test_the_blocking_keys_are_the_same_in_every_mode():
    """The entity rows are additive. The tracklist tier's key lookup is cheap and
    works, and stage 4 does not take it away."""
    keys = {
        mode: sorted(
            k.key_value
            for k in load_record(ALBUM_A, _ctx(mode)).entity_keys
            if k.key_type in ("analytic", "analytic_title")
        )
        for mode in ("off", "keys_only", "entities")
    }
    assert keys["off"] == keys["keys_only"] == keys["entities"]


# --- what entities mode writes --------------------------------------------


def test_an_album_yields_one_work_per_track_plus_its_own():
    record = load_record(ALBUM_A, _ctx("entities"))
    assert len(record.works) == 3
    assert len(record.expressions) == 3
    # A track has no carrier of its own, so no manifestation row.
    assert len(record.manifestations) == 1


def test_the_tracklist_is_ordered_and_keeps_its_durations(repo: WEMIRepository):
    _ingest(repo, load_record(ALBUM_A, _ctx("entities")))
    album = repo.get_manifestation_by_source(SRC, "ALB-1")
    rows = _junction(repo, album.id)

    assert [(r["track_number"], r["title"]) for r in rows] == [
        (None, "Cantus arcticus"),  # the container's own expression
        (1, "Cantus arcticus"),
        (2, "Angel of light"),
    ]
    assert [r["duration"] for r in rows] == [None, "18:20", "31:05"]
    # The container's expression is what this manifestation *is*; a track is not
    # a second candidate for that.
    assert [bool(r["is_primary"]) for r in rows] == [True, False, False]


def test_each_track_is_tied_to_the_album_by_an_edge(repo: WEMIRepository):
    _ingest(repo, load_record(ALBUM_A, _ctx("entities")))
    container = repo.get_work_by_source(SRC, "ALB-1")
    edges = [
        e
        for e in repo.get_work_relationships(container.id)
        if e.relationship_type == "has_constituent"
    ]
    assert len(edges) == 2
    assert len({e.target_work_id for e in edges}) == 2


def test_a_constituent_finds_its_container_without_a_second_edge(repo: WEMIRepository):
    """`get_work_relationships` returns edges in both directions, so one row per
    constituent is enough and the inverse `constituent_of` need not be stored."""
    _ingest(repo, load_record(ALBUM_A, _ctx("entities")))
    container = repo.get_work_by_source(SRC, "ALB-1")
    track = next(
        w for w in _all_works(repo) if w["title"] == "Angel of light"
    )
    edges = repo.get_work_relationships(track["id"])
    assert [(e.source_work_id, e.relationship_type) for e in edges] == [
        (container.id, "has_constituent")
    ]


def _all_works(repo: WEMIRepository) -> list[dict]:
    with repo.get_connection() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM works ORDER BY id;")]


# --- identity -------------------------------------------------------------


def test_two_releases_of_one_track_converge_on_one_work(repo: WEMIRepository):
    """The point of a content-derived identity: it is one work, and a tracklist
    that matched only within one release would be no use."""
    for album in (ALBUM_A, ALBUM_B):
        _ingest(repo, load_record(album, _ctx("entities")))

    works = _all_works(repo)
    # Two containers, three distinct constituents: Cantus arcticus is shared.
    assert len(works) == 5
    cantus = [w for w in works if w["title"] == "Cantus arcticus" and w["source_id"].startswith("c:")]
    assert len(cantus) == 1

    with repo.get_connection() as conn:
        containers = conn.execute(
            "SELECT COUNT(*) c FROM work_relationships "
            "WHERE target_work_id = ? AND relationship_type = 'has_constituent';",
            (cantus[0]["id"],),
        ).fetchone()["c"]
    assert containers == 2


def test_the_shared_work_keeps_each_release_s_own_timing(repo: WEMIRepository):
    """Duration belongs to the embodiment, not to the work: two pressings of one
    piece are timed differently, and the junction row is where that lives."""
    for album in (ALBUM_A, ALBUM_B):
        _ingest(repo, load_record(album, _ctx("entities")))

    durations = {}
    for control in ("ALB-1", "ALB-2"):
        manifestation = repo.get_manifestation_by_source(SRC, control)
        for row in _junction(repo, manifestation.id):
            if row["title"] == "Cantus arcticus" and row["track_number"]:
                durations[control] = row["duration"]
    assert durations == {"ALB-1": "18:20", "ALB-2": "18:25"}


def test_a_track_naming_no_composer_inherits_the_main_entry():
    """One release names a track's composer in its note and the next does not.
    Without the inheritance they are two works; the cataloguing rule is that
    contents under a single main entry are that main entry's."""
    a = load_record(ALBUM_A, _ctx("entities"))
    b = load_record(ALBUM_B, _ctx("entities"))
    shared_a = {w.source_id for w in a.works if w.title == "Cantus arcticus"}
    shared_b = {w.source_id for w in b.works if w.title == "Cantus arcticus"}
    assert shared_a & shared_b


def test_a_compilation_with_no_main_entry_inherits_nothing():
    """A various-artists compilation is catalogued under title, and inheriting a
    creator from it would attribute every track to one artist."""
    compilation = _album(
        "VA-1", "Suomalaista musiikkia", [("Finlandia", None, "8:00")], main_entry=None
    )
    named = _album("SOLO-1", "Finlandia", [("Finlandia", "Sibelius", "8:00")])
    va = load_record(compilation, _ctx("entities"))
    solo = load_record(named, _ctx("entities"))
    va_ids = {w.source_id for w in va.works if w.source_id.startswith("c:")}
    solo_ids = {w.source_id for w in solo.works if w.source_id.startswith("c:")}
    assert va_ids and solo_ids
    assert not (va_ids & solo_ids)


def test_a_single_work_release_makes_no_self_constituent():
    """Its note lists exactly one work, itself, so the container row already
    represents that work and a second one would be a duplicate with an edge to
    itself."""
    single = _album("SING-1", "Cantus arcticus", [("Cantus arcticus", None, "18:20")])
    record = load_record(single, _ctx("entities"))
    assert len(record.works) == 1
    assert record.relationships == []


def test_an_album_named_after_one_of_its_works_still_lists_that_work():
    """The narrower case the first guard got wrong: "Cantus arcticus" is both
    ALBUM_A's title and one of its two pieces, and that piece belongs in the
    tracklist."""
    record = load_record(ALBUM_A, _ctx("entities"))
    titles = [w.title for w in record.works if w.source_id.startswith("c:")]
    assert titles == ["Cantus arcticus", "Angel of light"]


def test_identity_survives_a_reingest(repo: WEMIRepository):
    for _ in range(2):
        _ingest(repo, load_record(ALBUM_A, _ctx("entities")))
    with repo.get_connection() as conn:
        counts = {
            table: conn.execute(f"SELECT COUNT(*) c FROM {table};").fetchone()["c"]
            for table in ("works", "manifestation_expressions", "work_relationships")
        }
    assert counts == {"works": 3, "manifestation_expressions": 3, "work_relationships": 2}


# --- the contract the base class applies ----------------------------------


def test_constituent_works_are_origin_stamped_and_isbd_stripped():
    """`finalise` runs over every work in the record, so a constituent gets the
    datastore contract without the emitter having to remember it."""
    record = load_record(
        _album("ISBD-1", "Kokoelma", [("Sinuhe egyptilainen /", None, None)]),
        _ctx("entities"),
    )
    constituent = next(w for w in record.works if w.source_id.startswith("c:"))
    assert constituent.record_origin == SRC
    assert constituent.title == "Sinuhe egyptilainen"


def test_constituent_works_are_serialised():
    """`serialized_text` is what BM25 and the dense index read; a constituent
    without it is a work no vector search can reach."""
    record = load_record(ALBUM_A, _ctx("entities"))
    for work in record.works:
        assert work.serialized_text


def test_only_the_bibliographic_loader_claims_constituents():
    assert MarcBibliographicLoader.capabilities.emits_constituents is True
    for loader in (MarcAuthorityLoader, BibframeLoader):
        assert loader.capabilities.emits_constituents is False
        assert "constituents" in loader.capabilities.not_produced


def test_a_component_shaped_constituent_is_not_emitted_yet():
    """773 yields shape `component`, which needs its own manifestation and a
    resolved container — stage 6. It must be skipped, not half-written."""
    article = etree.fromstring(
        """<record xmlns="http://www.loc.gov/MARC21/slim">
          <leader>00000cab a2200000 i 4500</leader>
          <controlfield tag="001">ART-1</controlfield>
          <datafield tag="245" ind1="1" ind2="0"><subfield code="a">Artikkeli</subfield></datafield>
          <datafield tag="773" ind1="0" ind2=" ">
            <subfield code="t">Virittaja</subfield><subfield code="g">s. 12-34</subfield>
          </datafield>
        </record>""".encode()
    )
    record = load_record(article, _ctx("entities"))
    assert len(record.works) == 1
    assert record.relationships == []
