"""Unit and integration tests for Hybrid Coarse-to-Fine Clustering and Persistent Vector Caching."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional, Sequence
from unittest.mock import MagicMock, patch

import numpy as np
import pytest
from typer.testing import CliRunner

from reconciliator.clustering.factory import create_corpus_clusterer
from reconciliator.clustering.hybrid import HybridBigClustGraphClusterer
from reconciliator.commands import app
from reconciliator.config.matching import HybridClusterConfig, WEMIClusterConfig
from reconciliator.database import WEMIRepository
from reconciliator.embedding.protocol import BaseVectorEmbedder
from reconciliator.models import MatchRecord


class MockDeterministicEmbedder(BaseVectorEmbedder):
    """Deterministic mock embedder conforming to VectorEmbedder for testing."""

    def __init__(self, dim: int = 32) -> None:
        self._dim = dim
        self.call_count = 0
        self.config = MagicMock()
        self.config.model_name = "mock-sentence-transformer"

    @property
    def dimension(self) -> int:
        return self._dim

    def serialize_record(self, record: Any) -> str:
        return str(getattr(record, "title", ""))

    def embed_texts(
        self, texts: Sequence[str] | list[str], batch_size: Optional[int] = None
    ) -> np.ndarray:
        self.call_count += len(texts)
        embeddings = np.zeros((len(texts), self.dimension), dtype=np.float32)
        for i, text in enumerate(texts):
            low = text.lower()
            if "sinuhe" in low or "waltari" in low:
                embeddings[i, 0] = 3.0
                embeddings[i, 1] = 3.0
            elif "tuntematon" in low or "linna" in low:
                embeddings[i, 0] = -3.0
                embeddings[i, 1] = -3.0
            elif "kalevala" in low or "lonnrot" in low:
                embeddings[i, 0] = 3.0
                embeddings[i, 1] = -3.0
            else:
                embeddings[i, 2] = 1.0
        return embeddings


@pytest.fixture
def sqlite_repo(tmp_path: Path) -> WEMIRepository:
    """Fixture providing a real SQLite-backed WEMIRepository."""
    db_file = tmp_path / "cache_test.db"
    return WEMIRepository(f"sqlite:///{db_file}")


def test_wemi_repository_vector_cache_crud(sqlite_repo: WEMIRepository) -> None:
    """Verify that WEMIRepository can persist, retrieve, and delete cached dense vectors."""
    v1 = np.array([0.1, 0.2, 0.3, 0.4], dtype=np.float32)
    v2 = np.array([0.5, 0.6, 0.7, 0.8], dtype=np.float32)

    records = [
        {
            "content_hash": "hash_sinuhe",
            "model_name": "test-model-v1",
            "entity_type": "work",
            "dimension": 4,
            "embedding_bytes": v1,
        },
        {
            "content_hash": "hash_tuntematon",
            "model_name": "test-model-v1",
            "entity_type": "work",
            "dimension": 4,
            "embedding_bytes": v2,
        },
    ]

    stored = sqlite_repo.store_cached_embeddings(records)
    assert stored >= 2

    # Query back
    cached = sqlite_repo.get_cached_embeddings(
        ["hash_sinuhe", "hash_tuntematon", "hash_missing"],
        model_name="test-model-v1",
    )
    assert len(cached) == 2
    assert "hash_sinuhe" in cached
    assert "hash_tuntematon" in cached
    assert "hash_missing" not in cached

    np.testing.assert_allclose(cached["hash_sinuhe"], v1, atol=1e-6)
    np.testing.assert_allclose(cached["hash_tuntematon"], v2, atol=1e-6)

    # Different model name returns empty
    other_cached = sqlite_repo.get_cached_embeddings(
        ["hash_sinuhe"],
        model_name="different-model",
    )
    assert len(other_cached) == 0

    # Delete cached
    deleted = sqlite_repo.delete_cached_embeddings(model_name="test-model-v1")
    assert deleted >= 2

    cached_after = sqlite_repo.get_cached_embeddings(
        ["hash_sinuhe"],
        model_name="test-model-v1",
    )
    assert len(cached_after) == 0


def test_bigclust_multiplex_cached_embeddings_integration(sqlite_repo: WEMIRepository) -> None:
    """Verify BigClustMultiplexClusterer serves embeddings from repository cache on repeat calls."""
    from reconciliator.clustering.bigclust import BigClustMultiplexClusterer

    embedder = MockDeterministicEmbedder(dim=16)
    clusterer = BigClustMultiplexClusterer(
        corpus_repo=sqlite_repo,
        wemi_repo=sqlite_repo,
        embedder=embedder,
        max_work_clusters=5,
    )

    records = [
        MatchRecord(
            record_id="rec_s1",
            format="marcxml",
            work_type="text",
            title="Sinuhe egyptilainen",
            title_stem="sinuhe egyptilainen",
            author_key="waltari mika",
            year=1945,
        ),
        MatchRecord(
            record_id="rec_s2",
            format="marcxml",
            work_type="text",
            title="The Egyptian",
            title_stem="the egyptian",
            author_key="waltari mika",
            year=1949,
        ),
    ]

    # First run: cache is empty, embedder is called
    initial_calls = embedder.call_count
    res1 = clusterer.cluster_records(records, persist=False)
    assert res1.records_clustered == 2
    calls_run1 = embedder.call_count - initial_calls
    assert calls_run1 > 0

    # Second run: all texts are now cached in sqlite_repo!
    calls_before_run2 = embedder.call_count
    res2 = clusterer.cluster_records(records, persist=False)
    assert res2.records_clustered == 2
    calls_run2 = embedder.call_count - calls_before_run2
    # embedder.embed_texts should NOT be called for work embeddings on run 2
    assert calls_run2 == 0


def test_hybrid_clusterer_coarse_macro_partition_and_fine_graph_reranking(
    sqlite_repo: WEMIRepository,
) -> None:
    """Verify that HybridBigClustGraphClusterer groups by GPU embeddings but enforces fine vetoes."""
    embedder = MockDeterministicEmbedder(dim=16)
    cfg = WEMIClusterConfig(
        method="hybrid_bigclust_graph",
        hybrid=HybridClusterConfig(boundary_margin=0.20, fine_threshold=0.70),
    )

    clusterer = HybridBigClustGraphClusterer(
        corpus_repo=sqlite_repo,
        wemi_repo=sqlite_repo,
        embedder=embedder,
        config=cfg,
        max_work_clusters=10,
        min_work_clusters=2,
    )

    records = [
        # 2 Sinuhe records -> Same author + high similarity -> should merge into 1 Work
        MatchRecord(
            record_id="sinuhe_1",
            format="marcxml",
            work_type="text",
            title="Sinuhe egyptiläinen",
            title_stem="sinuhe egyptilainen",
            author_key="waltari mika",
            year=1945,
            keys={"isbn": ["978-951-0-12345-1"]},
        ),
        MatchRecord(
            record_id="sinuhe_2",
            format="marcxml",
            work_type="text",
            title="Sinuhe egyptiläinen : viisitoista kirjaa",
            title_stem="sinuhe egyptilainen viisitoista kirjaa",
            author_key="waltari mika",
            year=1946,
            keys={"isbn": ["978-951-0-12345-1"]},
        ),
        # 2 Tuntematon sotilas records with conflicting volumes -> GPU groups them coarsely,
        # but fine reranking detects volume_conflict veto and separates them!
        MatchRecord(
            record_id="tuntematon_v1",
            format="marcxml",
            work_type="text",
            title="Tuntematon sotilas. Osa 1",
            title_stem="tuntematon sotilas osa 1",
            author_key="linna vaino",
            year=1954,
            volume_designation="1",
        ),
        MatchRecord(
            record_id="tuntematon_v2",
            format="marcxml",
            work_type="text",
            title="Tuntematon sotilas. Osa 2",
            title_stem="tuntematon sotilas osa 2",
            author_key="linna vaino",
            year=1954,
            volume_designation="2",
        ),
        # 1 Kalevala record -> distinct cluster
        MatchRecord(
            record_id="kalevala_1",
            format="marcxml",
            work_type="text",
            title="Kalevala",
            title_stem="kalevala",
            author_key="lonnrot elias",
            year=1849,
        ),
    ]

    res = clusterer.cluster_records(records, persist=False)
    assert res.records_clustered == 5
    # Sinuhe (1) + Tuntematon v1 (1) + Tuntematon v2 (1) + Kalevala (1) = 4 works
    assert res.work_clusters_count == 4


def test_hybrid_clusterer_boundary_candidate_linking(sqlite_repo: WEMIRepository) -> None:
    """Verify that boundary candidates spanning coarse partitions merge if sharing strong identifier."""
    embedder = MockDeterministicEmbedder(dim=16)
    cfg = WEMIClusterConfig(
        method="hybrid",
        hybrid=HybridClusterConfig(boundary_margin=0.99, fine_threshold=0.70),
    )

    clusterer = HybridBigClustGraphClusterer(
        corpus_repo=sqlite_repo,
        wemi_repo=sqlite_repo,
        embedder=embedder,
        config=cfg,
        max_work_clusters=5,
        min_work_clusters=2,
    )

    # Two records with different titles/embeddings but identical ISBN
    records = [
        MatchRecord(
            record_id="rec_a",
            format="marcxml",
            work_type="text",
            title="Sinuhe egyptiläinen",
            title_stem="sinuhe egyptilainen",
            author_key="waltari mika",
            year=1945,
            keys={"isbn": ["978-0-12345-999-0"]},
        ),
        MatchRecord(
            record_id="rec_b",
            format="marcxml",
            work_type="text",
            title="The Egyptian : Sinuhe",
            title_stem="the egyptian sinuhe",
            author_key="waltari mika",
            year=1949,
            keys={"isbn": ["978-0-12345-999-0"]},
        ),
    ]

    res = clusterer.cluster_records(records, persist=False)
    assert res.records_clustered == 2
    # Shared ISBN guarantees fine pairwise score of 1.0, merging into 1 Work
    assert res.work_clusters_count == 1


def test_create_corpus_clusterer_factory_hybrid(sqlite_repo: WEMIRepository) -> None:
    """Verify create_corpus_clusterer factory dispatches HybridBigClustGraphClusterer."""
    embedder = MockDeterministicEmbedder(dim=16)

    # 1. method = "hybrid"
    cfg1 = WEMIClusterConfig(method="hybrid")
    clusterer1 = create_corpus_clusterer(
        config=cfg1,
        corpus_repo=sqlite_repo,
        embedder=embedder,
    )
    assert isinstance(clusterer1, HybridBigClustGraphClusterer)

    # 2. method = "hybrid_bigclust_graph"
    cfg2 = WEMIClusterConfig(method="hybrid_bigclust_graph")
    clusterer2 = create_corpus_clusterer(
        config=cfg2,
        corpus_repo=sqlite_repo,
        embedder=embedder,
    )
    assert isinstance(clusterer2, HybridBigClustGraphClusterer)


def test_cli_cluster_corpus_hybrid_routing(tmp_path: Path) -> None:
    """Verify that cluster-corpus with --method hybrid invokes HybridBigClustGraphClusterer."""
    runner = CliRunner()
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        f"""
storage:
  data_dir: "{tmp_path}"
"""
    )

    from reconciliator.models import MultiplexClusterResult

    mock_res = MultiplexClusterResult(
        work_clusters_count=4,
        expression_clusters_count=6,
        manifestation_clusters_count=10,
        records_clustered=10,
        elapsed_seconds=0.08,
    )

    with patch("reconciliator.commands.wemi._wemi_repo") as mock_repo_fn, \
         patch("reconciliator.embedding.create_vector_embedder") as mock_embedder_fn, \
         patch("reconciliator.clustering.hybrid.HybridBigClustGraphClusterer") as mock_hybrid_cls:
        mock_embedder = MagicMock()
        mock_embedder_fn.return_value = mock_embedder
        mock_repo = MagicMock()
        mock_repo.get_all_match_records.return_value = [MagicMock()]
        mock_repo.non_authoritative_sources.return_value = ["test_corpus"]
        mock_repo_fn.return_value = mock_repo

        mock_instance = MagicMock()
        mock_instance.cluster_records.return_value = mock_res
        mock_hybrid_cls.return_value = mock_instance

        result = runner.invoke(
            app,
            [
                "cluster-corpus",
                "--config",
                str(cfg_file),
                "--method",
                "hybrid",
                "--dry-run",
            ],
        )
        assert result.exit_code == 0, result.output
        assert "Evaluated 10 staged record(s) into 4 Work cluster(s)" in result.output
        mock_hybrid_cls.assert_called_once()
        mock_instance.cluster_records.assert_called_once_with(
            mock_repo.get_all_match_records.return_value, persist=False
        )


def test_hybrid_neural_boundary_adjudication_twilight_zone(sqlite_repo: WEMIRepository) -> None:
    """Verify neural boundary adjudication rescues twilight pairs and preserves hard vetoes."""
    embedder = MockDeterministicEmbedder(dim=16)

    # 1. Base test: twilight pair without reranker should NOT merge
    rec_a = MatchRecord(
        record_id="rec_twilight_a",
        format="marcxml",
        work_type="text",
        title="Sinuhe egyptiläinen romaani",
        title_stem="sinuhe egyptilainen romaani",
        author_key="waltari mika",
        year=1945,
    )
    rec_b = MatchRecord(
        record_id="rec_twilight_b",
        format="marcxml",
        work_type="text",
        title="Sinuhe",
        title_stem="sinuhe",
        author_key="waltari mika",
        year=1945,
    )

    # 1. Base test: twilight pair without reranker should NOT merge
    clusterer_no_rerank = HybridBigClustGraphClusterer(
        corpus_repo=sqlite_repo,
        embedder=embedder,
        fine_threshold=0.85,
        twilight_lower=0.50,
        neural_boundary_threshold=0.75,
        rerank_scorer=None,
    )
    clusterer_no_rerank.edge_evaluator.evaluate = MagicMock(
        return_value=(0.65, "twilight_similarity")
    )

    res_no_rerank = clusterer_no_rerank.cluster_records([rec_a, rec_b], persist=False)
    assert res_no_rerank.records_clustered == 2
    # Base score 0.65 is in [0.50, 0.85). Without reranker -> separated
    assert res_no_rerank.work_clusters_count == 2

    # 2. Rescued by neural reranker: scorer returning 0.90 >= 0.75 merges them
    mock_scorer_high = MagicMock()
    mock_scorer_high.score.return_value = 0.90

    clusterer_rescued = HybridBigClustGraphClusterer(
        corpus_repo=sqlite_repo,
        embedder=embedder,
        fine_threshold=0.85,
        twilight_lower=0.50,
        neural_boundary_threshold=0.75,
        rerank_scorer=mock_scorer_high,
    )
    clusterer_rescued.edge_evaluator.evaluate = MagicMock(
        return_value=(0.65, "twilight_similarity")
    )

    res_rescued = clusterer_rescued.cluster_records([rec_a, rec_b], persist=False)
    assert res_rescued.records_clustered == 2
    assert res_rescued.work_clusters_count == 1
    mock_scorer_high.score.assert_called()

    # 3. Neural reranker rejecting: scorer returning 0.60 < 0.75 does NOT merge
    mock_scorer_low = MagicMock()
    mock_scorer_low.score.return_value = 0.60

    clusterer_rejected = HybridBigClustGraphClusterer(
        corpus_repo=sqlite_repo,
        embedder=embedder,
        fine_threshold=0.85,
        twilight_lower=0.50,
        neural_boundary_threshold=0.75,
        rerank_scorer=mock_scorer_low,
    )
    clusterer_rejected.edge_evaluator.evaluate = MagicMock(
        return_value=(0.65, "twilight_similarity")
    )

    res_rejected = clusterer_rejected.cluster_records([rec_a, rec_b], persist=False)
    assert res_rejected.records_clustered == 2
    assert res_rejected.work_clusters_count == 2

    # 4. Hard veto protection: conflicting volumes never merge even if neural scorer returns 1.0
    rec_v1 = MatchRecord(
        record_id="rec_v1",
        format="marcxml",
        work_type="text",
        title="Tuntematon sotilas. Osa 1",
        title_stem="tuntematon sotilas osa 1",
        author_key="linna vaino",
        year=1954,
        volume_designation="1",
    )
    rec_v2 = MatchRecord(
        record_id="rec_v2",
        format="marcxml",
        work_type="text",
        title="Tuntematon sotilas. Osa 2",
        title_stem="tuntematon sotilas osa 2",
        author_key="linna vaino",
        year=1954,
        volume_designation="2",
    )

    clusterer_veto = HybridBigClustGraphClusterer(
        corpus_repo=sqlite_repo,
        embedder=embedder,
        fine_threshold=0.85,
        twilight_lower=0.50,
        neural_boundary_threshold=0.75,
        rerank_scorer=mock_scorer_high,
    )
    clusterer_veto.edge_evaluator.evaluate = MagicMock(
        return_value=(0.0, "volume_conflict")
    )

    res_veto = clusterer_veto.cluster_records([rec_v1, rec_v2], persist=False)
    assert res_veto.records_clustered == 2
    # Hard volume veto prevents edge formation despite high neural scorer
    assert res_veto.work_clusters_count == 2


def test_incremental_warm_start_centroid_clustering(sqlite_repo: WEMIRepository) -> None:
    """Verify that incremental clustering warm-starts from persisted cluster centroids."""
    embedder = MockDeterministicEmbedder(dim=16)
    model_name = embedder.config.model_name

    clusterer = HybridBigClustGraphClusterer(
        corpus_repo=sqlite_repo,
        wemi_repo=sqlite_repo,
        embedder=embedder,
        fine_threshold=0.70,
    )

    # Batch 1: 2 Sinuhe records -> creates 1 Work cluster and stores its centroid
    batch1 = [
        MatchRecord(
            record_id="sinuhe_b1_1",
            format="marcxml",
            work_type="text",
            title="Sinuhe egyptiläinen",
            title_stem="sinuhe egyptilainen",
            author_key="waltari mika",
            year=1945,
        ),
        MatchRecord(
            record_id="sinuhe_b1_2",
            format="marcxml",
            work_type="text",
            title="Sinuhe egyptiläinen : viisitoista kirjaa",
            title_stem="sinuhe egyptilainen viisitoista kirjaa",
            author_key="waltari mika",
            year=1946,
        ),
    ]

    res1 = clusterer.cluster_records(batch1, persist=True, incremental=False)
    assert res1.records_clustered == 2
    assert res1.work_clusters_count == 1

    # Verify centroid stored in repository
    centroids_after_b1 = sqlite_repo.get_cluster_centroids(model_name=model_name)
    assert len(centroids_after_b1) == 1
    sinuhe_cluster_id = list(centroids_after_b1.keys())[0]

    # Batch 2: 1 Sinuhe record (matches centroid) + 1 novel record (Kalevala)
    batch2 = [
        MatchRecord(
            record_id="sinuhe_b2_new",
            format="marcxml",
            work_type="text",
            title="Sinuhe the Egyptian",
            title_stem="sinuhe the egyptian",
            author_key="waltari mika",
            year=1949,
        ),
        MatchRecord(
            record_id="kalevala_novel",
            format="marcxml",
            work_type="text",
            title="Kalevala",
            title_stem="kalevala",
            author_key="lonnrot elias",
            year=1849,
        ),
    ]

    res2 = clusterer.cluster_records(batch2, persist=True, incremental=True)
    assert res2.records_clustered == 2
    # 1 assigned to existing Sinuhe cluster + 1 new Kalevala work cluster = 2 work clusters
    assert res2.work_clusters_count == 2

    # Verify that sinuhe_b2_new was assigned to the existing Sinuhe cluster!
    sinuhe_link = sqlite_repo.get_corpus_linkage("sinuhe_b2_new")
    kalevala_link = sqlite_repo.get_corpus_linkage("kalevala_novel")
    assert sinuhe_link is not None
    assert kalevala_link is not None

    assert sinuhe_link.work_cluster_id == sinuhe_cluster_id
    assert kalevala_link.work_cluster_id != sinuhe_cluster_id

    # Verify repository now has 2 centroids
    centroids_after_b2 = sqlite_repo.get_cluster_centroids(model_name=model_name)
    assert len(centroids_after_b2) == 2
    assert sinuhe_cluster_id in centroids_after_b2


def test_cluster_records_stream_out_of_core(sqlite_repo: WEMIRepository) -> None:
    """Verify cluster_records_stream processes records in streaming chunks with centroid warm-starting."""
    embedder = MockDeterministicEmbedder(dim=16)

    clusterer = HybridBigClustGraphClusterer(
        corpus_repo=sqlite_repo,
        wemi_repo=sqlite_repo,
        embedder=embedder,
        fine_threshold=0.70,
    )

    records = [
        # Chunk 1: 2 Sinuhe records
        MatchRecord(
            record_id="stream_s1",
            format="marcxml",
            work_type="text",
            title="Sinuhe egyptiläinen",
            title_stem="sinuhe egyptilainen",
            author_key="waltari mika",
            year=1945,
        ),
        MatchRecord(
            record_id="stream_s2",
            format="marcxml",
            work_type="text",
            title="Sinuhe egyptiläinen 2",
            title_stem="sinuhe egyptilainen 2",
            author_key="waltari mika",
            year=1946,
        ),
        # Chunk 2: 1 Sinuhe record + 1 Tuntematon record
        MatchRecord(
            record_id="stream_s3",
            format="marcxml",
            work_type="text",
            title="Sinuhe egyptiläinen 3",
            title_stem="sinuhe egyptilainen 3",
            author_key="waltari mika",
            year=1947,
        ),
        MatchRecord(
            record_id="stream_t1",
            format="marcxml",
            work_type="text",
            title="Tuntematon sotilas",
            title_stem="tuntematon sotilas",
            author_key="linna vaino",
            year=1954,
        ),
    ]

    def records_generator():
        for r in records:
            yield r

    results = list(clusterer.cluster_records_stream(records_generator(), chunk_size=2, persist=True))

    assert len(results) == 2
    # Chunk 1: 2 records clustered into 1 work
    assert results[0].records_clustered == 2
    assert results[0].work_clusters_count == 1

    # Chunk 2: 2 records clustered (1 attached to Sinuhe from Chunk 1, 1 new Tuntematon)
    assert results[1].records_clustered == 2
    assert results[1].work_clusters_count == 2

    # Check linkages in SQLite: all 3 Sinuhe records must share the exact same work_cluster_id
    l_s1 = sqlite_repo.get_corpus_linkage("stream_s1")
    l_s2 = sqlite_repo.get_corpus_linkage("stream_s2")
    l_s3 = sqlite_repo.get_corpus_linkage("stream_s3")
    l_t1 = sqlite_repo.get_corpus_linkage("stream_t1")

    assert l_s1 is not None and l_s2 is not None and l_s3 is not None and l_t1 is not None
    assert l_s1.work_cluster_id == l_s2.work_cluster_id
    assert l_s2.work_cluster_id == l_s3.work_cluster_id
    assert l_t1.work_cluster_id != l_s1.work_cluster_id


def test_repo_get_match_records_stream_and_entity_keys_for_ids(sqlite_repo: WEMIRepository) -> None:
    """Verify streaming match records and keyset pagination in WEMIRepository."""
    from reconciliator.models import EntityKey, ManifestationEntity, WorkEntity

    w_id = sqlite_repo.insert_work(
        WorkEntity(
            source="test",
            source_id="w1",
            title="Test Work",
            title_normalized="test work",
            work_type="text",
        )
    )
    m1 = sqlite_repo.insert_manifestation(
        ManifestationEntity(
            source="test",
            source_id="m1",
            title="Test Book 1",
            title_normalized="test book 1",
            work_id=w_id,
            publication_year=2020,
        )
    )
    m2 = sqlite_repo.insert_manifestation(
        ManifestationEntity(
            source="test",
            source_id="m2",
            title="Test Book 2",
            title_normalized="test book 2",
            work_id=w_id,
            publication_year=2021,
        )
    )
    sqlite_repo.insert_entity_keys_batch([
        EntityKey(entity_type="manifestation", entity_id=m1, key_type="isbn", key_value="978-0-123"),
        EntityKey(entity_type="manifestation", entity_id=m2, key_type="isbn", key_value="978-0-456"),
    ])

    # Test get_entity_keys_for_ids
    keys = sqlite_repo.get_entity_keys_for_ids([m1, m2], "manifestation")
    assert keys[m1]["isbn"] == ["978-0-123"]
    assert keys[m2]["isbn"] == ["978-0-456"]

    # Test get_match_records_stream with chunk_size=1
    streamed = list(sqlite_repo.get_match_records_stream(chunk_size=1))
    assert len(streamed) == 2
    rec_ids = {r.record_id for r in streamed}
    assert rec_ids == {"m1", "m2"}

    # Test get_all_match_records delegating to stream
    all_recs = sqlite_repo.get_all_match_records()
    assert len(all_recs) == 2


def test_cli_cluster_corpus_streaming_and_incremental_flags(tmp_path: Path) -> None:
    """Verify CLI cluster-corpus accepts --streaming, --chunk-size, and --incremental."""
    runner = CliRunner()
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(f"""
storage:
  data_dir: "{tmp_path}"
""")

    from reconciliator.models import MultiplexClusterResult

    mock_res = MultiplexClusterResult(
        work_clusters_count=2,
        expression_clusters_count=2,
        manifestation_clusters_count=4,
        records_clustered=4,
        elapsed_seconds=0.05,
    )

    with (
        patch("reconciliator.commands.wemi._wemi_repo") as mock_repo_fn,
        patch("reconciliator.embedding.create_vector_embedder") as mock_embedder_fn,
        patch("reconciliator.clustering.hybrid.HybridBigClustGraphClusterer") as mock_hybrid_cls,
    ):
        mock_embedder_fn.return_value = MagicMock()
        mock_repo = MagicMock()
        mock_repo.get_match_records_stream.return_value = iter([MagicMock(), MagicMock()])
        mock_repo.non_authoritative_sources.return_value = ["test_corpus"]
        mock_repo_fn.return_value = mock_repo

        mock_instance = MagicMock()
        mock_instance.cluster_records_stream.return_value = iter([mock_res])
        mock_hybrid_cls.return_value = mock_instance

        result = runner.invoke(
            app,
            [
                "cluster-corpus",
                "--config",
                str(cfg_file),
                "--method",
                "hybrid",
                "--streaming",
                "--chunk-size",
                "500",
                "--incremental",
                "--dry-run",
            ],
        )
        assert result.exit_code == 0, result.output
        assert "via streaming into 2 Work cluster(s)" in result.output
        mock_repo.get_match_records_stream.assert_called_once_with(
            origins=["test_corpus"], chunk_size=500
        )
        mock_instance.cluster_records_stream.assert_called_once()


def test_base_clusterer_formatting_and_builder_consolidation(sqlite_repo: WEMIRepository) -> None:
    """Verify BaseCorpusClusterer formatting methods and WEMIClusterBuilder work cluster assembly."""
    from reconciliator.clustering.bigclust.multiplex import BigClustMultiplexClusterer
    from reconciliator.clustering.wemi_builder import WEMIClusterBuilder
    from reconciliator.utils.device import clear_accelerator_memory

    builder = WEMIClusterBuilder(sqlite_repo)
    clusterer = BigClustMultiplexClusterer(corpus_repo=sqlite_repo, wemi_repo=sqlite_repo)

    rec = MatchRecord(
        record_id="rec_fmt_1",
        format="marcxml",
        work_type="text",
        title="Test Title",
        title_stem="test title",
        author_key="Author, Test",
        language="fin",
        keys={"translators_signature": ["Translator A"]},
        volume_designation="Vol. 1",
    )

    w_text = clusterer.format_work_text(rec)
    assert "Author: Author, Test" in w_text
    assert "Title: Test Title" in w_text

    e_text = clusterer.format_expression_text(rec)
    assert "Language: fin" in e_text
    assert "Translator: Translator A" in e_text

    # Verify build_work_cluster_from_records
    wc = builder.build_work_cluster_from_records([rec])
    assert wc.canonical_title == "Test Title"
    assert wc.canonical_author == "Author, Test"
    assert wc.work_type == "text"
    assert wc.canonical_access_point is not None

    # Test accelerator cache purge lifecycle hook
    clear_accelerator_memory()


def test_agent_clusterer_semantic_disambiguation(sqlite_repo: WEMIRepository) -> None:
    """Verify AgentClusterer semantic disambiguation detects homonym collisions and applies veto."""
    from reconciliator.clustering.agent import AgentClusterer
    from reconciliator.config.matching import AgentClusterConfig

    class MockAgentEmbedder:
        def __init__(self) -> None:
            self.dimension = 4

        def embed_texts(self, texts: list[str]) -> np.ndarray:
            vecs = []
            for t in texts:
                if "astronomer" in t.lower():
                    vecs.append(np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32))
                elif "musician" in t.lower():
                    vecs.append(np.array([0.0, 1.0, 0.0, 0.0], dtype=np.float32))
                elif "physicist" in t.lower():
                    vecs.append(np.array([0.9, 0.1, 0.0, 0.0], dtype=np.float32))
                else:
                    vecs.append(np.array([0.0, 0.0, 1.0, 0.0], dtype=np.float32))
            return np.array(vecs, dtype=np.float32)

    embedder = MockAgentEmbedder()
    cfg = AgentClusterConfig(
        enabled=True,
        enable_semantic_disambiguation=True,
        semantic_homonym_veto_threshold=0.40,
        semantic_boost_threshold=0.85,
    )
    clusterer = AgentClusterer(repo=sqlite_repo, config=cfg, embedder=embedder)

    # 1. Homonym collision: two John Smiths with identical name but divergent biographical text
    a1 = {
        "id": 1,
        "name": "John Smith",
        "name_normalized": "john smith",
        "birth_year": None,
        "death_year": None,
    }
    a2 = {
        "id": 2,
        "name": "John Smith",
        "name_normalized": "john smith",
        "birth_year": None,
        "death_year": None,
    }
    attrs1: dict[str, list[str]] = {}
    attrs2: dict[str, list[str]] = {}
    affils1 = {"Royal Observatory"}
    affils2 = {"Philharmonic Orchestra"}
    concepts1 = {"astronomer"}
    concepts2 = {"musician"}

    score, method = clusterer._score_pair(
        a1,
        a2,
        attrs1,
        attrs2,
        affils1,
        affils2,
        concepts1,
        concepts2,
        set(),
        set(),
        set(),
        set(),
    )
    # Cosine sim is 0.0 < 0.40 veto threshold, so it must be vetoed
    assert score == 0.0
    assert method == "veto_semantic_homonym_divergence"

    # 2. Corroborated match: two John Smiths with highly aligned semantic text (astronomer vs physicist)
    concepts3 = {"physicist"}
    affils3 = {"Cambridge"}
    score_boost, method_boost = clusterer._score_pair(
        a1,
        a2,
        attrs1,
        attrs2,
        affils1,
        affils3,
        concepts1,
        concepts3,
        set(),
        set(),
        set(),
        set(),
    )
    # Cosine sim is ~0.99 >= 0.85 boost threshold
    assert score_boost > 0.0
    assert method_boost == "weighted_scoring_semantic_corroborated"

