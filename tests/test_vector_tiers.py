"""The work and manifestation vector tiers: building, wiring and using them.

Before this, `work_indexer=` appeared nowhere in `src/`: every CLI command built
the repository with only a db path and an FTS config, so the matcher's dense tier
was unreachable in production and every match fell through to FTS. The embedding
configuration, engine selection and IPC daemon all existed to serve a path
nothing reached.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from lxml import etree

from reconciliator.config import ReconcileConfig, RerankConfig
from reconciliator.indexer import TorchVectorEngine
from reconciliator.loaders.marcxml import build_target_record_from_marc, decompose_marc_wemi
from reconciliator.matcher import WorkMatcher
from reconciliator.repository import WEMIRepository

MARC_NS = "http://www.loc.gov/MARC21/slim"
DIM = 16


class _BagOfWordsEmbedder:
    """Deterministic embedder: shared tokens give similar vectors."""

    dimension = DIM

    def embed_texts(self, texts, batch_size=None):  # noqa: ANN001, ANN201
        out = np.zeros((len(texts), DIM), dtype=np.float32)
        for i, text in enumerate(texts):
            for token in set((text or "").lower().split()):
                out[i, hash(token) % DIM] += 1.0
            norm = float(np.linalg.norm(out[i]))
            if norm:
                out[i] /= norm
        return out


def _record(control: str, title: str, author: str = "Kivi, Aleksis") -> etree._Element:
    return etree.fromstring(
        f"""<record xmlns="{MARC_NS}">
            <controlfield tag="001">{control}</controlfield>
            <datafield tag="100" ind1="1" ind2=" "><subfield code="a">{author}</subfield></datafield>
            <datafield tag="245" ind1="1" ind2="0"><subfield code="a">{title}</subfield></datafield>
        </record>""".encode()
    )


@pytest.fixture
def repo(tmp_path: Path) -> WEMIRepository:
    store = WEMIRepository(
        db_path=tmp_path / "authority.db",
        dimension=DIM,
        work_indexer=TorchVectorEngine(tmp_path / "work.pt", dimension=DIM),
        manifestation_indexer=TorchVectorEngine(tmp_path / "manif.pt", dimension=DIM),
    )
    store.register_source("melinda", is_authoritative=True)
    return store


def _ingest(store: WEMIRepository, control: str, title: str) -> None:
    w, e, m, _itms, agts, _rels, idents, _assoc, _mops, contribs = decompose_marc_wemi(
        _record(control, title), source="melinda", record_origin="melinda"
    )
    store.insert_wemi_batch(
        works=[w], expressions=[e], manifestations=[m], agents=agts,
        identifiers=idents, contributions=contribs, record_origin="melinda",
    )


# --- building -------------------------------------------------------------


def test_build_wemi_indexes_indexes_both_tiers(repo: WEMIRepository):
    _ingest(repo, "A1", "Seitseman veljesta")
    _ingest(repo, "A2", "Nummisuutarit")
    assert repo.work_indexer.total_vectors == 0

    counts = repo.build_wemi_indexes(
        embedder=_BagOfWordsEmbedder(),
        work_indexer=repo.work_indexer,
        manifestation_indexer=repo.manifestation_indexer,
    )
    assert counts == {"works": 2, "manifestations": 2}
    assert repo.work_indexer.total_vectors == 2
    assert repo.manifestation_indexer.total_vectors == 2


def test_build_wemi_indexes_can_build_one_tier_only(repo: WEMIRepository):
    _ingest(repo, "A1", "Seitseman veljesta")
    counts = repo.build_wemi_indexes(
        embedder=_BagOfWordsEmbedder(), work_indexer=repo.work_indexer
    )
    assert counts == {"works": 1, "manifestations": 0}
    assert repo.manifestation_indexer.total_vectors == 0


def test_build_wemi_indexes_skips_rows_without_serialized_text(repo: WEMIRepository):
    """A blank serialization must be skipped, not embedded as an empty string."""
    from reconciliator.models import WorkEntity

    repo.insert_works_batch(
        [
            WorkEntity(
                work_uri="urn:work:melinda:blank", source="melinda", source_id="blank",
                record_origin="melinda", title="T", title_normalized="t",
                serialized_text=None,
            )
        ]
    )
    counts = repo.build_wemi_indexes(
        embedder=_BagOfWordsEmbedder(), work_indexer=repo.work_indexer
    )
    assert counts["works"] == 0


def test_built_index_maps_vectors_back_to_their_rows(repo: WEMIRepository):
    """The mapping table is what makes a hit resolvable to a work."""
    _ingest(repo, "A1", "Seitseman veljesta kertomus")
    embedder = _BagOfWordsEmbedder()
    repo.build_wemi_indexes(embedder=embedder, work_indexer=repo.work_indexer)

    # The indexed text is the work's rendered serialization, not its bare title,
    # so the score is a partial overlap; what matters is that the hit resolves
    # back to the right row through the vector -> work_id mapping table.
    hits = repo.search_work_vectors(embedder.embed_texts(["Seitseman veljesta kertomus"]), top_k=5)
    assert hits
    work, score = hits[0]
    assert work.source_id == "A1"
    assert score > 0.0


# --- the dense tier being reachable at all --------------------------------


def _unmatchable_by_earlier_tiers(store: WEMIRepository):
    """A record with no shared identifier and a different title stem.

    Tier 1 and tier 2 cannot resolve it, so whether it matches at all is exactly
    the question of whether the dense tier runs.
    """
    target = build_target_record_from_marc(
        _record("T1", "Seitseman veljesta kertomus"), source="corpus"
    )
    return target.model_copy(update={"author_key": None, "title_stem": "wholly different stem"})


def test_dense_tier_fires_when_an_index_is_wired(repo: WEMIRepository):
    _ingest(repo, "A1", "Seitseman veljesta kertomus")
    embedder = _BagOfWordsEmbedder()
    repo.build_wemi_indexes(embedder=embedder, work_indexer=repo.work_indexer)

    matcher = WorkMatcher(
        wemi_repo=repo,
        embedder=embedder,
        config=ReconcileConfig(target_file=Path("x"), threshold=0.5),
    )
    assert matcher.match_target(_unmatchable_by_earlier_tiers(repo)).match_tier == (
        "tier2_vector_fallback"
    )


def test_without_an_embedder_the_dense_tier_cannot_run(repo: WEMIRepository):
    """Pins the old CLI behaviour, so a regression to it is visible."""
    _ingest(repo, "A1", "Seitseman veljesta kertomus")
    repo.build_wemi_indexes(
        embedder=_BagOfWordsEmbedder(), work_indexer=repo.work_indexer
    )
    matcher = WorkMatcher(
        wemi_repo=repo, embedder=None,
        config=ReconcileConfig(target_file=Path("x"), threshold=0.5),
    )
    assert matcher.match_target(_unmatchable_by_earlier_tiers(repo)).match_tier == "unmatched"


def test_an_unbuilt_index_is_harmless(repo: WEMIRepository):
    """Wiring indexes before build-index has run must not break matching."""
    _ingest(repo, "A1", "Seitseman veljesta")
    matcher = WorkMatcher(
        wemi_repo=repo, embedder=_BagOfWordsEmbedder(),
        config=ReconcileConfig(target_file=Path("x")),
    )
    assert repo.work_indexer.total_vectors == 0
    assert matcher.match_target(_unmatchable_by_earlier_tiers(repo)).match_tier == "unmatched"


# --- retrieval depth ------------------------------------------------------


def test_retrieval_depth_comes_from_rerank_top_k(repo: WEMIRepository):
    """It was hardcoded to 5 while the config declared 50."""
    for configured, expected in ((5, 5), (50, 50), (200, 200)):
        matcher = WorkMatcher(
            wemi_repo=repo,
            config=ReconcileConfig(
                target_file=Path("x"), rerank=RerankConfig(top_k=configured)
            ),
        )
        assert matcher._retrieval_top_k() == expected


def test_retrieval_depth_defaults_without_config(repo: WEMIRepository):
    assert WorkMatcher(wemi_repo=repo)._retrieval_top_k() == 50


# --- clustering uses the manifestation tier -------------------------------


def test_manifestation_vector_merge_joins_groups_the_keys_left_apart(tmp_path: Path):
    """Why manifestation embeddings are kept.

    The manifestation key is `standard::year::volume`. Two catalogues' records of
    one edition that share no standard number never meet in a key group, so the
    index is consulted to union them.
    """
    from reconciliator.clusterer import CorpusClusterer
    from reconciliator.models import ManifestationEntity, MatchRecord

    store = WEMIRepository(
        db_path=tmp_path / "authority.db",
        dimension=DIM,
        manifestation_indexer=TorchVectorEngine(tmp_path / "manif.pt", dimension=DIM),
    )
    embedder = _BagOfWordsEmbedder()
    serialization = "Seitseman veljesta Otava 1870"

    # Two manifestation rows describing one edition, under different sources and
    # with no shared standard number.
    store.insert_manifestations_batch(
        [
            ManifestationEntity(
                manifestation_uri=f"urn:manif:{src}:{rid}", source=src, source_id=rid,
                record_origin=src, title="Seitseman veljesta",
                title_normalized="seitseman veljesta", serialized_text=serialization,
            )
            for src, rid in (("cat_a", "r1"), ("cat_b", "r2"))
        ]
    )
    built = store.build_wemi_indexes(
        embedder=embedder, manifestation_indexer=store.manifestation_indexer
    )
    assert built["manifestations"] == 2

    class _Matcher:
        embedder = None

    fake_matcher = _Matcher()
    fake_matcher.embedder = embedder  # type: ignore[attr-defined]
    clusterer = CorpusClusterer(corpus_repo=store, matcher=fake_matcher)  # type: ignore[arg-type]

    def _rec(rid: str, standard: str) -> MatchRecord:
        return MatchRecord(
            record_id=rid, format="marcxml", title="Seitseman veljesta",
            title_stem="seitseman veljesta", keys={"standard": [standard]},
            serialized_manifestation=serialization,
        )

    # Distinct standard numbers put these in separate key groups.
    groups = {"isbn-a::0::": [_rec("r1", "isbn-a")], "isbn-b::0::": [_rec("r2", "isbn-b")]}
    merged = clusterer._merge_manifestation_groups_by_vector(groups)

    assert len(merged) == 1, "identical manifestations must end in one group"
    assert sorted(r.record_id for r in next(iter(merged.values()))) == ["r1", "r2"]


def test_manifestation_vector_merge_is_a_no_op_without_an_index(tmp_path: Path):
    """Clustering must still work before build-index has ever run."""
    from reconciliator.clusterer import CorpusClusterer
    from reconciliator.models import MatchRecord

    store = WEMIRepository(db_path=tmp_path / "authority.db", dimension=DIM)

    class _Matcher:
        embedder = _BagOfWordsEmbedder()

    clusterer = CorpusClusterer(corpus_repo=store, matcher=_Matcher())  # type: ignore[arg-type]
    groups = {
        "a::0::": [MatchRecord(record_id="r1", format="marcxml", title="T", title_stem="t")],
        "b::0::": [MatchRecord(record_id="r2", format="marcxml", title="T", title_stem="t")],
    }
    assert len(clusterer._merge_manifestation_groups_by_vector(groups)) == 2


def test_manifestation_vector_merge_keeps_distinct_editions_apart(tmp_path: Path):
    """Additive only: it joins groups, and must not join unrelated ones."""
    from reconciliator.clusterer import CorpusClusterer
    from reconciliator.models import ManifestationEntity, MatchRecord

    store = WEMIRepository(
        db_path=tmp_path / "authority.db",
        dimension=DIM,
        manifestation_indexer=TorchVectorEngine(tmp_path / "manif.pt", dimension=DIM),
    )
    embedder = _BagOfWordsEmbedder()
    store.insert_manifestations_batch(
        [
            ManifestationEntity(
                manifestation_uri=f"urn:manif:s:{rid}", source="s", source_id=rid,
                record_origin="s", title=title, title_normalized=title.lower(),
                serialized_text=text,
            )
            for rid, title, text in (
                ("r1", "Seitseman veljesta", "Seitseman veljesta Otava 1870"),
                ("r2", "Nummisuutarit", "Nummisuutarit Weilin Goos 1902 komedia"),
            )
        ]
    )
    store.build_wemi_indexes(
        embedder=embedder, manifestation_indexer=store.manifestation_indexer
    )

    class _Matcher:
        embedder = None

    fake = _Matcher()
    fake.embedder = embedder  # type: ignore[attr-defined]
    clusterer = CorpusClusterer(corpus_repo=store, matcher=fake)  # type: ignore[arg-type]

    groups = {
        "a::0::": [
            MatchRecord(
                record_id="r1", format="marcxml", title="Seitseman veljesta",
                title_stem="seitseman veljesta",
                serialized_manifestation="Seitseman veljesta Otava 1870",
            )
        ],
        "b::0::": [
            MatchRecord(
                record_id="r2", format="marcxml", title="Nummisuutarit",
                title_stem="nummisuutarit",
                serialized_manifestation="Nummisuutarit Weilin Goos 1902 komedia",
            )
        ],
    }
    assert len(clusterer._merge_manifestation_groups_by_vector(groups)) == 2
