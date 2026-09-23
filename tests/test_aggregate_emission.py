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
from sqlalchemy import delete, func, nulls_first, select

from reconciliator.constituents import resolve_for_source
from reconciliator.database import (
    WEMIRepository,
    expressions_table,
    manifestation_expressions_table,
    manifestations_table,
    work_relationships_table,
    works_table,
)
from reconciliator.embedding.template import WEMISerializationEngine
from reconciliator.indexer.torch_engine import TorchVectorEngine
from reconciliator.loaders.base import DecomposedRecord, LoadContext
from reconciliator.loaders.registry import (
    BibframeLoader,
    MarcAuthorityLoader,
    MarcBibliographicLoader,
    load_record,
)

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
        stmt = (
            select(
                expressions_table.c.title,
                expressions_table.c.source_id,
                manifestation_expressions_table.c.track_number,
                manifestation_expressions_table.c.disc_number,
                manifestation_expressions_table.c.duration,
                manifestation_expressions_table.c.is_primary,
            )
            .select_from(
                manifestation_expressions_table.join(
                    expressions_table,
                    expressions_table.c.id == manifestation_expressions_table.c.expression_id,
                )
            )
            .where(manifestation_expressions_table.c.manifestation_id == manifestation_id)
            .order_by(
                nulls_first(manifestation_expressions_table.c.disc_number),
                nulls_first(manifestation_expressions_table.c.track_number),
            )
        )
        return [dict(row) for row in conn.execute(stmt).mappings().fetchall()]


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
    track = next(w for w in _all_works(repo) if w["title"] == "Angel of light")
    edges = repo.get_work_relationships(track["id"])
    assert [(e.source_work_id, e.relationship_type) for e in edges] == [
        (container.id, "has_constituent")
    ]


def _all_works(repo: WEMIRepository) -> list[dict]:
    with repo.get_connection() as conn:
        return [
            dict(r)
            for r in conn.execute(select(works_table).order_by(works_table.c.id))
            .mappings()
            .fetchall()
        ]


# --- identity -------------------------------------------------------------


def test_two_releases_of_one_track_converge_on_one_work(repo: WEMIRepository):
    """The point of a content-derived identity: it is one work, and a tracklist
    that matched only within one release would be no use."""
    for album in (ALBUM_A, ALBUM_B):
        _ingest(repo, load_record(album, _ctx("entities")))

    works = _all_works(repo)
    # Two containers, three distinct constituents: Cantus arcticus is shared.
    assert len(works) == 5
    cantus = [
        w for w in works if w["title"] == "Cantus arcticus" and w["source_id"].startswith("c:")
    ]
    assert len(cantus) == 1

    with repo.get_connection() as conn:
        containers = conn.execute(
            select(func.count())
            .select_from(work_relationships_table)
            .where(
                work_relationships_table.c.target_work_id == cantus[0]["id"],
                work_relationships_table.c.relationship_type == "has_constituent",
            )
        ).scalar()
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
            table: conn.exec_driver_sql(f"SELECT COUNT(*) FROM {table};").scalar()
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


def test_the_record_loaders_claim_constituents_and_the_authority_one_does_not():
    """Stage 7 gave the BibFrame path `bf:hasPart`. An authority record describes
    a work rather than a publication with contents, so it declares the gap."""
    assert MarcBibliographicLoader.capabilities.emits_constituents is True
    assert BibframeLoader.capabilities.emits_constituents is True
    assert "constituents" not in BibframeLoader.capabilities.not_produced
    assert MarcAuthorityLoader.capabilities.emits_constituents is False
    assert "constituents" in MarcAuthorityLoader.capabilities.not_produced


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


# --- stage 5: whose contribution is it -----------------------------------

ANTHOLOGY = etree.fromstring(
    """<record xmlns="http://www.loc.gov/MARC21/slim">
      <leader>00000cam a2200000 i 4500</leader>
      <controlfield tag="001">ANTH-1</controlfield>
      <controlfield tag="008">200101s2020    fi |||||||||||||||fin d</controlfield>
      <datafield tag="245" ind1="0" ind2="0">
        <subfield code="a">Suomalaisia novelleja</subfield></datafield>
      <datafield tag="700" ind1="1" ind2="2"><subfield code="a">Waltari, Mika</subfield>
        <subfield code="t">Kuun maisema</subfield></datafield>
      <datafield tag="700" ind1="1" ind2="2"><subfield code="a">Canth, Minna</subfield>
        <subfield code="t">Agnes</subfield></datafield>
      <datafield tag="700" ind1="1" ind2=" "><subfield code="a">Virtanen, Liisa</subfield>
        <subfield code="e">toimittaja</subfield></datafield>
    </record>""".encode()
)

#: A composer who is both the album's main entry and named beside one of its
#: own pieces.
OWN_WORKS = etree.fromstring(
    """<record xmlns="http://www.loc.gov/MARC21/slim">
      <leader>00000cjm a2200000 i 4500</leader>
      <controlfield tag="001">ALB-9</controlfield>
      <controlfield tag="007">sd fsngnnmmned</controlfield>
      <controlfield tag="008">200101s2020    fi |||||||||||||||fin d</controlfield>
      <datafield tag="100" ind1="1" ind2=" ">
        <subfield code="a">Rautavaara, Einojuhani</subfield>
        <subfield code="e">säveltäjä</subfield></datafield>
      <datafield tag="245" ind1="1" ind2="0"><subfield code="a">Teoksia</subfield></datafield>
      <datafield tag="336" ind1=" " ind2=" "><subfield code="b">prm</subfield></datafield>
      <datafield tag="700" ind1="1" ind2="2">
        <subfield code="a">Rautavaara, Einojuhani</subfield>
        <subfield code="t">Cantus arcticus</subfield></datafield>
      <datafield tag="700" ind1="1" ind2="2"><subfield code="a">Sibelius, Jean</subfield>
        <subfield code="t">Finlandia</subfield></datafield>
    </record>""".encode()
)


def _contributions(record: DecomposedRecord) -> set[tuple[str, str, bool]]:
    """(agent name, work title or URI, is_primary) for every work contribution."""
    title_by_uri = {w.work_uri: w.title for w in record.works}
    name_by_uri = {a.agent_uri: a.name for a in record.agents}
    return {
        (
            name_by_uri.get(c.agent_uri or "", "?"),
            title_by_uri.get(c.entity_uri or "", c.entity_uri or "?"),
            bool(c.is_primary),
        )
        for c in record.contributions
        if c.entity_type == "work"
    }


def test_an_anthologys_story_authors_were_stored_as_its_own_authors():
    """The defect, in `keys_only`: Waltari and Canth as primary authors of
    "Suomalaisia novelleja", which neither of them wrote."""
    assert _contributions(load_record(ANTHOLOGY, _ctx("keys_only"))) == {
        ("Waltari, Mika", "Suomalaisia novelleja", True),
        ("Canth, Minna", "Suomalaisia novelleja", True),
    }


def test_each_story_author_now_contributes_to_their_own_story():
    assert _contributions(load_record(ANTHOLOGY, _ctx("entities"))) == {
        ("Waltari, Mika", "Kuun maisema", True),
        ("Canth, Minna", "Agnes", True),
    }


def test_an_editor_stays_with_the_volume():
    """A 700 without `$t` is a plain added entry: the editor edited the volume,
    and nothing about this fix touches them."""
    record = load_record(ANTHOLOGY, _ctx("entities"))
    editor = next(c for c in record.contributions if c.role == "edt")
    assert editor.entity_type == "expression"


def test_the_agent_rows_are_kept():
    """Waltari is a real agent and carries the `$0`/`$1` authority links. What
    was wrong is the tier the contribution named, not the agent."""
    for mode in ("keys_only", "entities"):
        record = load_record(ANTHOLOGY, _ctx(mode))
        assert [a.name for a in record.agents] == [
            "Waltari, Mika",
            "Canth, Minna",
            "Virtanen, Liisa",
        ]


def test_the_main_entry_keeps_its_contribution_and_the_constituent_gains_one():
    """A composer's album listing his own pieces: he contributed both, so this is
    a second contribution rather than a move."""
    got = _contributions(load_record(OWN_WORKS, _ctx("entities")))
    assert ("Rautavaara, Einojuhani", "Teoksia", True) in got
    assert ("Rautavaara, Einojuhani", "Cantus arcticus", True) in got
    assert ("Sibelius, Jean", "Finlandia", True) in got
    # And Sibelius stops being an author of the album he has one piece on.
    assert ("Sibelius, Jean", "Teoksia", True) not in got


def test_nothing_moves_in_keys_only_mode():
    """There is nowhere for the contribution to go, and dropping it would lose
    the agent link."""
    assert _contributions(load_record(OWN_WORKS, _ctx("keys_only"))) == {
        ("Rautavaara, Einojuhani", "Teoksia", True),
        ("Sibelius, Jean", "Teoksia", True),
    }


def test_the_blocking_keys_do_not_move():
    """Deliberately out of scope. `author_key` comes from the parsed agent list,
    not from contributions, and the target path shares that derivation without
    carrying a constituent profile - so changing it would make the two sides
    disagree. See the plan's open questions."""
    keys = {
        mode: sorted(
            (k.key_type, k.key_value)
            for k in load_record(ANTHOLOGY, _ctx(mode)).entity_keys
            if k.key_type in ("author", "work", "title_stem")
        )
        for mode in ("keys_only", "entities")
    }
    assert keys["keys_only"] == keys["entities"]
    assert ("author", "waltari") in keys["entities"]


# --- stage 6: a record that is itself a part ------------------------------

HOST_ID = "(FI-MELINDA)000900111"

ISSUE = etree.fromstring(
    """<record xmlns="http://www.loc.gov/MARC21/slim">
      <leader>00000cas a2200000 i 4500</leader>
      <controlfield tag="001">000900111</controlfield>
      <controlfield tag="003">FI-MELINDA</controlfield>
      <controlfield tag="008">200101c20209999fi |||||||||||||||fin d</controlfield>
      <datafield tag="022" ind1=" " ind2=" "><subfield code="a">0042-6806</subfield></datafield>
      <datafield tag="245" ind1="0" ind2="0"><subfield code="a">Virittaja</subfield></datafield>
    </record>""".encode()
)


def _article(control: str, title: str, host: str | None = HOST_ID) -> etree._Element:
    link = f'<subfield code="w">{host}</subfield>' if host else ""
    return etree.fromstring(
        f"""<record xmlns="http://www.loc.gov/MARC21/slim">
          <leader>00000cab a2200000 i 4500</leader>
          <controlfield tag="001">{control}</controlfield>
          <controlfield tag="003">FI-MELINDA</controlfield>
          <controlfield tag="008">200101s2020    fi |||||||||||||||fin d</controlfield>
          <datafield tag="100" ind1="1" ind2=" ">
            <subfield code="a">Virtanen, Liisa</subfield></datafield>
          <datafield tag="245" ind1="1" ind2="0"><subfield code="a">{title}</subfield></datafield>
          <datafield tag="773" ind1="0" ind2=" ">
            <subfield code="t">Virittaja</subfield>
            <subfield code="x">0042-6806</subfield>
            <subfield code="q">12</subfield>
            {link}
          </datafield>
        </record>""".encode()
    )


def test_a_host_item_entry_describes_the_container_not_a_new_row():
    """Shape B is not "emit more rows": a host-item entry describes the
    container, so what it yields is a fact about the record already in hand."""
    record = load_record(_article("000900222", "Suomen murteista"), _ctx("entities"))
    assert len(record.works) == 1
    assert len(record.manifestations) == 1
    manifestation = record.manifestations[0]
    assert manifestation.is_component is True
    assert manifestation.component_type == "article"
    assert manifestation.host_title == "Virittaja"
    assert manifestation.container_uri == f"urn:manif:{SRC}:{HOST_ID}"


def test_an_article_finds_a_host_ingested_in_an_earlier_batch(repo: WEMIRepository):
    """The usual case by a wide margin: a journal issue is ingested long before
    the articles that cite it, so batch-local resolution is not enough."""
    _ingest(repo, load_record(ISSUE, _ctx("entities")))
    _ingest(repo, load_record(_article("000900222", "Suomen murteista"), _ctx("entities")))

    issue = repo.get_manifestation_by_source(SRC, HOST_ID)
    article = repo.get_manifestation_by_source(SRC, "(FI-MELINDA)000900222")
    assert article.container_id == issue.id
    assert article.component_type == "article"


def test_the_container_can_be_asked_for_its_parts(repo: WEMIRepository):
    """Which was the whole complaint: `container_id` had a self-FK, two indexes
    and a cascade, and no row ever carried a value."""
    _ingest(repo, load_record(ISSUE, _ctx("entities")))
    for control, title in (("000900222", "Suomen murteista"), ("000900333", "Nimistontutkimus")):
        _ingest(repo, load_record(_article(control, title), _ctx("entities")))

    issue = repo.get_manifestation_by_source(SRC, HOST_ID)
    with repo.get_connection() as conn:
        parts = list(
            conn.execute(
                select(manifestations_table.c.source_id)
                .where(manifestations_table.c.container_id == issue.id)
                .order_by(manifestations_table.c.source_id)
            ).scalars()
        )
    assert parts == ["(FI-MELINDA)000900222", "(FI-MELINDA)000900333"]


def test_an_article_naming_no_host_record_is_still_a_component(repo: WEMIRepository):
    """It is a component - that is what a 773 says - it just has nothing to
    point at. Dropping it would lose a real publication."""
    _ingest(repo, load_record(_article("000900444", "Irrallinen", host=None), _ctx("entities")))
    article = repo.get_manifestation_by_source(SRC, "(FI-MELINDA)000900444")
    assert article.is_component is True
    assert article.container_id is None


def test_a_host_that_is_not_in_the_store_leaves_the_component_unlinked(repo: WEMIRepository):
    _ingest(repo, load_record(_article("000900555", "Orpo"), _ctx("entities")))
    article = repo.get_manifestation_by_source(SRC, "(FI-MELINDA)000900555")
    assert article.container_id is None
    assert article.host_title == "Virittaja"


def test_deleting_the_host_cascades_to_its_parts(repo: WEMIRepository):
    """`container_id` is declared `ON DELETE CASCADE`, and now that rows carry
    values the cascade is reachable."""
    _ingest(repo, load_record(ISSUE, _ctx("entities")))
    _ingest(repo, load_record(_article("000900222", "Suomen murteista"), _ctx("entities")))
    issue = repo.get_manifestation_by_source(SRC, HOST_ID)
    with repo.get_connection() as conn:
        conn.execute(delete(manifestations_table).where(manifestations_table.c.id == issue.id))
        remaining = conn.exec_driver_sql("SELECT COUNT(*) FROM manifestations;").scalar()
    assert remaining == 0


def test_containment_needs_no_profile_change_for_a_source_with_none():
    """A source with no constituent profile keeps the behaviour it had:
    `is_component` from the presence of a 773, and no container."""
    record = load_record(_article("000900666", "Vanhaan tapaan"), _ctx("entities", profile=None))
    manifestation = record.manifestations[0]
    assert manifestation.is_component is True
    assert manifestation.component_type is None
    assert manifestation.container_uri is None
