"""Stage 9 of plans/aggregates.md: what constituents do to the work index.

Measured on 200 albums of 12 tracks rather than assumed:

    works              200 -> 1088   (5.4x, after convergence merged shared tracks)
    manifestations     200 ->  200   (1.0x - an embodied constituent has no carrier)
    junction rows      200 -> 2600
    entity_keys       5600 -> 5600   (1.0x - constituents emit no keys)

    work index rows: 1088, of which constituents: 888 (82%)

    BM25, query 'Finlandia':
       1. constituent 'Finlandia nro 4'
       2. constituent 'Finlandia nro 3'
       ...  six tracks, no album

Tier 3 takes the top *k*, so for a target album with a short or common title the
album candidates are pushed out of the window entirely — a recall regression in
album matching caused by indexing things that are not albums. Constituents stay
reachable exactly: by `analytic_title` key, and from a matched container.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from reconciliator.config import AppConfig
from reconciliator.indexer.torch_engine import TorchVectorEngine
from reconciliator.models import (
    ExpressionEntity,
    ManifestationEntity,
    WorkEntity,
    WorkRelationship,
)
from reconciliator.repository import WEMIRepository

SRC = "fono"


class _FakeEmbedder:
    """Deterministic stand-in: what is under test is the *selection*."""

    def embed_texts(self, texts, batch_size=None):  # noqa: ANN001
        return np.zeros((len(texts), 4), dtype="float32")


@pytest.fixture
def store(tmp_path: Path) -> WEMIRepository:
    repo = WEMIRepository(db_path=tmp_path / "authority.db", dimension=4)
    repo.register_source(SRC, is_authoritative=True)
    return repo


def _work(source_id: str, title: str) -> WorkEntity:
    return WorkEntity(
        work_uri=f"urn:work:{SRC}:{source_id}", source=SRC, source_id=source_id,
        record_origin=SRC, title=title, title_normalized=title.lower(),
        work_type="music", serialized_text=f"Nimeke: {title}",
    )


def _album_with_a_track(store: WEMIRepository, *, track_has_manifestation: bool = False):
    """A container, one embodied constituent, and the edge between them."""
    container = _work("ALB", "Kokoelma")
    track = _work("c:deadbeef", "Yksi kappale")
    manifestations = [
        ManifestationEntity(
            manifestation_uri=f"urn:manif:{SRC}:ALB", source=SRC, source_id="ALB",
            record_origin=SRC, title="Kokoelma", title_normalized="kokoelma",
            work_uri=container.work_uri, serialized_text="Nimeke: Kokoelma",
        )
    ]
    if track_has_manifestation:
        # The track was also released on its own, so it has a carrier of its own
        # and is a publication a record can match.
        manifestations.append(
            ManifestationEntity(
                manifestation_uri=f"urn:manif:{SRC}:SINGLE", source=SRC,
                source_id="SINGLE", record_origin=SRC, title="Yksi kappale",
                title_normalized="yksi kappale", work_uri=track.work_uri,
                serialized_text="Nimeke: Yksi kappale",
            )
        )
    store.insert_wemi_batch(
        works=[container, track],
        expressions=[
            ExpressionEntity(
                work_id=0, work_uri=track.work_uri,
                expression_uri=f"urn:exp:{SRC}:c:deadbeef:fin", source=SRC,
                source_id="c:deadbeef", record_origin=SRC, language="fin",
                content_type="audio_music", title="Yksi kappale",
            )
        ],
        manifestations=manifestations,
        relationships=[
            WorkRelationship(
                source_work_id=0, target_work_id=0,
                source_work_uri=container.work_uri, target_work_uri=track.work_uri,
                relationship_type="has_constituent",
            )
        ],
        record_origin=SRC,
    )


def _build(store: WEMIRepository, tmp_path: Path, **kwargs) -> int:
    counts = store.build_wemi_indexes(
        embedder=_FakeEmbedder(),
        work_indexer=TorchVectorEngine(tmp_path / f"w{kwargs}.pt", dimension=4),
        **kwargs,
    )
    return counts["works"]


def test_constituent_works_stay_out_of_the_work_index(
    store: WEMIRepository, tmp_path: Path
):
    """82% of the index on an album corpus, and they crowd albums out of the
    top-k window for short queries."""
    _album_with_a_track(store)
    assert _build(store, tmp_path) == 1  # the container only


def test_the_flag_puts_them_back(store: WEMIRepository, tmp_path: Path):
    """A source whose *targets* are tracks rather than releases wants the
    opposite, so this is a setting and not a rule."""
    _album_with_a_track(store)
    assert _build(store, tmp_path, include_constituent_works=True) == 2


def test_a_constituent_that_was_also_published_is_indexed(
    store: WEMIRepository, tmp_path: Path
):
    """The test for "only a constituent" is having no manifestation of its own: a
    work nobody published separately is not something a record can match at the
    manifestation level, while a track released as a single is a publication."""
    _album_with_a_track(store, track_has_manifestation=True)
    assert _build(store, tmp_path) == 2


def test_manifestations_are_never_filtered(store: WEMIRepository, tmp_path: Path):
    """An embodied constituent adds no manifestation at all, so the
    manifestation index - which serves the clusterer - is unaffected either way.
    Measured: 200 albums of 12 tracks leave manifestation rows at 1.0x."""
    _album_with_a_track(store)
    counts = store.build_wemi_indexes(
        embedder=_FakeEmbedder(),
        manifestation_indexer=TorchVectorEngine(tmp_path / "m.pt", dimension=4),
    )
    assert counts["manifestations"] == 1


def test_the_default_is_to_exclude():
    assert AppConfig().constituents.index_works is False


def test_an_excluded_constituent_is_still_reachable(store: WEMIRepository):
    """Nothing is lost by keeping them out of the index: a constituent is found
    exactly, from its container."""
    _album_with_a_track(store)
    container = store.get_work_by_source(SRC, "ALB")
    assert [w.title for w in store.get_constituent_works(container.id)] == [
        "Yksi kappale"
    ]
