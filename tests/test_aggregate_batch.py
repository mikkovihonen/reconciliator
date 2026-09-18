"""Stage 1 of plans/aggregates.md: can the store hold an aggregate at all?

Everything here was impossible before, and invisible while a record yielded one
manifestation and one expression. `insert_wemi_batch` bound every
manifestation-expression association to the *first* manifestation and the *first*
expression, and the junction's primary key is (manifestation_id, expression_id),
so an album's ordered tracklist collapsed into a single row. Work relationships
bound their source to the first work, `container_id` was carried through the
insert but resolved from nothing, and the edge insert was unconditional, so every
re-harvest appended another copy of every edge.

No loader emits any of this yet; that is stage 3. These tests fix the contract it
will be written against.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select

from reconciliator.db import expressions_table, manifestation_expressions_table
from reconciliator.indexer.torch_engine import TorchVectorEngine
from reconciliator.models import (
    ExpressionEntity,
    ManifestationEntity,
    ManifestationExpressionAssociation,
    WorkEntity,
    WorkRelationship,
)
from reconciliator.repository import WEMIRepository

SRC = "fono"
TRACKS = 20


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


def _album() -> dict:
    """One album: a container plus `TRACKS` embodied constituents (shape A).

    A track is a Work realised as an Expression and embodied in the *container's*
    manifestation - it has no carrier of its own - so it gets no manifestation
    row, only a junction row carrying its position.
    """
    container_work = WorkEntity(
        work_uri=f"urn:work:{SRC}:ALBUM-1",
        source=SRC,
        source_id="ALBUM-1",
        title="Cantus arcticus",
        title_normalized="cantus arcticus",
        work_type="music",
    )
    container_expr = ExpressionEntity(
        work_id=0,
        expression_uri=f"urn:exp:{SRC}:ALBUM-1:fin",
        source=SRC,
        source_id="ALBUM-1",
        language="fin",
        content_type="audio_music",
    )
    container_manif = ManifestationEntity(
        manifestation_uri=f"urn:manif:{SRC}:ALBUM-1",
        source=SRC,
        source_id="ALBUM-1",
        title="Cantus arcticus",
        title_normalized="cantus arcticus",
        carrier_type="audio_disc",
    )

    works = [container_work]
    expressions = [container_expr]
    associations = [
        ManifestationExpressionAssociation(
            manifestation_id=0,
            expression_id=0,
            manifestation_uri=container_manif.manifestation_uri,
            expression_uri=container_expr.expression_uri,
            is_primary=True,
        )
    ]
    relationships: list[WorkRelationship] = []

    for n in range(1, TRACKS + 1):
        work_uri = f"urn:work:{SRC}:c:track{n:02d}"
        expr_uri = f"urn:exp:{SRC}:c:track{n:02d}:fin"
        works.append(
            WorkEntity(
                work_uri=work_uri,
                source=SRC,
                source_id=f"c:track{n:02d}",
                title=f"Track {n}",
                title_normalized=f"track {n}",
                work_type="music",
            )
        )
        expressions.append(
            ExpressionEntity(
                work_id=0,
                expression_uri=expr_uri,
                source=SRC,
                source_id=f"c:track{n:02d}",
                language="fin",
                content_type="audio_music",
            )
        )
        associations.append(
            ManifestationExpressionAssociation(
                manifestation_id=0,
                expression_id=0,
                manifestation_uri=container_manif.manifestation_uri,
                expression_uri=expr_uri,
                track_number=n,
                # Two discs, so ordering is tested across the pair rather than
                # by track number alone.
                disc_number=1 if n <= 10 else 2,
                duration=f"{3 + n % 5}:{n:02d}",
                is_primary=False,
            )
        )
        relationships.append(
            WorkRelationship(
                source_work_id=0,
                target_work_id=0,
                source_work_uri=container_work.work_uri,
                target_work_uri=work_uri,
                relationship_type="has_constituent",
            )
        )

    # One shape-B component as well: a part with its own extent, so it gets its
    # own manifestation row hanging off the container.
    component = ManifestationEntity(
        manifestation_uri=f"urn:manif:{SRC}:ALBUM-1:p1",
        source=SRC,
        source_id="ALBUM-1:p1",
        title="Liner notes",
        title_normalized="liner notes",
        container_uri=container_manif.manifestation_uri,
        is_component=True,
        component_type="part",
        pagination="1-12",
    )

    return {
        "works": works,
        "expressions": expressions,
        "manifestations": [container_manif, component],
        "manifestation_expressions": associations,
        "relationships": relationships,
    }


def _ingest(repo: WEMIRepository) -> None:
    repo.insert_wemi_batch(record_origin=SRC, **_album())


def _junction_rows(repo: WEMIRepository, manifestation_id: int) -> list[dict]:
    with repo.get_connection() as conn:
        stmt = (
            select(
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
                manifestation_expressions_table.c.disc_number,
                manifestation_expressions_table.c.track_number,
            )
        )
        return [dict(row) for row in conn.execute(stmt).mappings().fetchall()]


# --- the junction ---------------------------------------------------------


def test_an_ordered_tracklist_survives_the_batch_insert(repo: WEMIRepository):
    _ingest(repo)
    album = repo.get_manifestation_by_source(SRC, "ALBUM-1")
    rows = _junction_rows(repo, album.id)

    # The container's own expression plus one per track.
    assert len(rows) == TRACKS + 1

    tracks = [r for r in rows if r["track_number"] is not None]
    assert [r["track_number"] for r in tracks] == list(range(1, TRACKS + 1))
    assert sorted({r["disc_number"] for r in tracks}) == [1, 2]
    assert tracks[0]["duration"] == "4:01"


def test_the_navigation_method_returns_every_expression(repo: WEMIRepository):
    """`get_expressions_for_manifestation` already ordered by disc and track and
    already had a caller in the matcher; it had simply never been given more than
    one row to return."""
    _ingest(repo)
    album = repo.get_manifestation_by_source(SRC, "ALBUM-1")
    assert len(repo.get_expressions_for_manifestation(album.id)) == TRACKS + 1


def test_associations_bind_by_uri_not_by_position(repo: WEMIRepository):
    """Each junction row must join the two rows its URIs name. Bound
    positionally, all 21 pointed at the first expression and the primary key
    reduced them to one."""
    _ingest(repo)
    album = repo.get_manifestation_by_source(SRC, "ALBUM-1")
    rows = _junction_rows(repo, album.id)
    by_source_id = {r["source_id"]: r for r in rows}
    assert by_source_id["c:track07"]["track_number"] == 7
    assert by_source_id["c:track07"]["disc_number"] == 1
    assert by_source_id["c:track17"]["disc_number"] == 2
    assert by_source_id["ALBUM-1"]["is_primary"] == 1


def test_a_record_with_one_expression_still_binds_without_uris(repo: WEMIRepository):
    """The loaders emit an association with no URIs on it, so the positional
    fallback stays load-bearing until stage 3 replaces it."""
    work = WorkEntity(
        work_uri=f"urn:work:{SRC}:SOLO-1",
        source=SRC,
        source_id="SOLO-1",
        title="Yksi",
        title_normalized="yksi",
    )
    expression = ExpressionEntity(
        work_id=0,
        expression_uri=f"urn:exp:{SRC}:SOLO-1:fin",
        source=SRC,
        source_id="SOLO-1",
        language="fin",
        content_type="text",
    )
    manifestation = ManifestationEntity(
        manifestation_uri=f"urn:manif:{SRC}:SOLO-1",
        source=SRC,
        source_id="SOLO-1",
        title="Yksi",
        title_normalized="yksi",
    )
    repo.insert_wemi_batch(
        works=[work],
        expressions=[expression],
        manifestations=[manifestation],
        manifestation_expressions=[
            ManifestationExpressionAssociation(manifestation_id=0, expression_id=0)
        ],
        record_origin=SRC,
    )
    stored = repo.get_manifestation_by_source(SRC, "SOLO-1")
    assert len(repo.get_expressions_for_manifestation(stored.id)) == 1


def test_an_unbindable_association_is_dropped(repo: WEMIRepository):
    """Rather than stored as a row joining nothing, or failing the whole batch."""
    batch = _album()
    batch["manifestation_expressions"].append(
        ManifestationExpressionAssociation(
            manifestation_id=0,
            expression_id=0,
            manifestation_uri=f"urn:manif:{SRC}:ALBUM-1",
            expression_uri="urn:exp:nowhere:missing:fin",
        )
    )
    repo.insert_wemi_batch(record_origin=SRC, **batch)
    album = repo.get_manifestation_by_source(SRC, "ALBUM-1")
    with repo.get_connection() as conn:
        orphans = conn.exec_driver_sql(
            "SELECT COUNT(*) FROM manifestation_expressions WHERE expression_id <= 0;"
        ).scalar()
    assert orphans == 0
    assert len(_junction_rows(repo, album.id)) == TRACKS + 1


# --- containment ----------------------------------------------------------


def test_a_component_manifestation_is_linked_to_its_container(repo: WEMIRepository):
    """`container_id` has a self-FK, two indexes and a cascade, and until now
    nothing ever put a value in it."""
    _ingest(repo)
    album = repo.get_manifestation_by_source(SRC, "ALBUM-1")
    component = repo.get_manifestation_by_source(SRC, "ALBUM-1:p1")
    assert component.container_id == album.id
    assert component.is_component is True
    assert component.component_type == "part"


def test_the_container_is_inserted_before_its_component(repo: WEMIRepository):
    """Order within the batch must not matter: a component listed first still
    resolves, because containers are inserted in their own pass."""
    batch = _album()
    batch["manifestations"] = list(reversed(batch["manifestations"]))
    repo.insert_wemi_batch(record_origin=SRC, **batch)
    album = repo.get_manifestation_by_source(SRC, "ALBUM-1")
    component = repo.get_manifestation_by_source(SRC, "ALBUM-1:p1")
    assert component.container_id == album.id


def test_a_component_whose_container_is_missing_is_stored_unlinked(
    repo: WEMIRepository,
):
    """Not dropped: the component is still a real publication, and losing it
    would lose the only record that the source described it."""
    batch = _album()
    batch["manifestations"][1].container_uri = "urn:manif:nowhere:missing"
    repo.insert_wemi_batch(record_origin=SRC, **batch)
    component = repo.get_manifestation_by_source(SRC, "ALBUM-1:p1")
    assert component is not None
    assert component.container_id is None


# --- the relationship pair ------------------------------------------------


def test_constituent_edges_bind_both_endpoints(repo: WEMIRepository):
    _ingest(repo)
    container = repo.get_work_by_source(SRC, "ALBUM-1")
    edges = [
        e
        for e in repo.get_work_relationships(container.id)
        if e.relationship_type == "has_constituent"
    ]
    assert len(edges) == TRACKS
    # Bound positionally, every edge pointed from the first work to the first
    # work, so the count would survive and the graph would be meaningless.
    assert len({e.target_work_id for e in edges}) == TRACKS
    assert all(e.source_work_id == container.id for e in edges)


def test_the_inverse_direction_is_available():
    """Both halves of the pair exist, so a constituent can name its container as
    well as the other way round."""
    for kind in ("has_constituent", "constituent_of"):
        assert (
            WorkRelationship(
                source_work_id=1, target_work_id=2, relationship_type=kind
            ).relationship_type
            == kind
        )


def test_re_ingesting_a_record_adds_no_duplicates(repo: WEMIRepository):
    """An edge is a fact about two works: asserting it twice is the same fact.
    The insert was unconditional, so a re-harvest appended another copy of every
    edge - one per record before, twenty per album now."""
    _ingest(repo)
    _ingest(repo)
    with repo.get_connection() as conn:
        counts = {
            table: conn.exec_driver_sql(f"SELECT COUNT(*) FROM {table};").scalar()
            for table in (
                "works",
                "expressions",
                "manifestations",
                "manifestation_expressions",
                "work_relationships",
            )
        }
    assert counts == {
        "works": TRACKS + 1,
        "expressions": TRACKS + 1,
        "manifestations": 2,
        "manifestation_expressions": TRACKS + 1,
        "work_relationships": TRACKS,
    }


def test_deleting_the_source_removes_the_whole_aggregate(repo: WEMIRepository):
    """`ingest_mode: replace` has to be able to undo an aggregate, and the
    junction and the container hierarchy both cascade."""
    _ingest(repo)
    repo.delete_source(SRC)
    with repo.get_connection() as conn:
        for table in (
            "works",
            "expressions",
            "manifestations",
            "manifestation_expressions",
            "work_relationships",
        ):
            remaining = conn.exec_driver_sql(f"SELECT COUNT(*) FROM {table};").scalar()
            assert remaining == 0, f"{table} still holds {remaining} rows"
