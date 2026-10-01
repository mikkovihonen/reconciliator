"""Unit tests for GPU-accelerated clustering engines on Metal MPS, CUDA, and CPU."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional, Sequence

import numpy as np
import pytest
import torch

from reconciliator.clustering.bipartite import BipartiteCoClusterer, GPUBipartiteSpectralEngine
from reconciliator.clustering.correlation import CCPivotEngine, CorrelationCorpusClusterer
from reconciliator.clustering.density import DensityCorpusClusterer, GPUDBSCANEngine
from reconciliator.clustering.factory import create_corpus_clusterer
from reconciliator.clustering.gpu import (
    clear_accelerator_memory,
    compute_pairwise_distances,
    compute_sparse_similarity_graph,
    is_gpu_available,
    resolve_compute_device,
)
from reconciliator.clustering.hierarchical import (
    GPUHierarchicalEngine,
    HierarchicalTree,
    HierarchicalWEMIClusterer,
)
from reconciliator.clustering.mcl import GPUMarkovClusterEngine, MarkovCorpusClusterer
from reconciliator.config.matching import (
    BipartiteCoClusterConfig,
    CorrelationClusteringConfig,
    DBSCANClusterConfig,
    HierarchicalClusterConfig,
    MarkovClusterConfig,
    WEMIClusterConfig,
)
from reconciliator.database import WEMIRepository
from reconciliator.embedding.protocol import BaseVectorEmbedder
from reconciliator.models import MatchRecord


class MockDeterministicEmbedder(BaseVectorEmbedder):
    """Deterministic mock embedder conforming to VectorEmbedder for testing."""

    def __init__(self, dim: int = 16) -> None:
        self._dim = dim

    @property
    def dimension(self) -> int:
        return self._dim

    def serialize_record(self, record: Any) -> str:
        return str(getattr(record, "title", ""))

    def embed_texts(
        self, texts: Sequence[str] | list[str], batch_size: Optional[int] = None
    ) -> np.ndarray:
        embeddings = np.zeros((len(texts), self.dimension), dtype=np.float32)
        for i, text in enumerate(texts):
            low = text.lower()
            if "sinuhe" in low or "waltari" in low:
                embeddings[i, 0] = 5.0
                embeddings[i, 1] = 5.0
            elif "tuntematon" in low or "linna" in low:
                embeddings[i, 0] = -5.0
                embeddings[i, 1] = -5.0
            elif "kalevala" in low or "lonnrot" in low:
                embeddings[i, 2] = 5.0
                embeddings[i, 3] = 5.0
            else:
                embeddings[i, 4] = 2.0
        return embeddings


@pytest.fixture
def sqlite_repo(tmp_path: Path) -> WEMIRepository:
    """Fixture providing a SQLite-backed WEMIRepository."""
    db_file = tmp_path / "gpu_cluster_test.db"
    return WEMIRepository(f"sqlite:///{db_file}")


@pytest.fixture
def sample_records() -> list[MatchRecord]:
    """Sample records representing 3 distinct work families."""
    return [
        MatchRecord(
            record_id="rec_sinuhe_1",
            format="marcxml",
            title="Sinuhe egyptiläinen",
            title_stem="sinuhe egyptilainen",
            author_key="Waltari, Mika",
            year=1945,
            language="fin",
            work_type="text",
        ),
        MatchRecord(
            record_id="rec_sinuhe_2",
            format="marcxml",
            title="Sinuhe the Egyptian",
            title_stem="sinuhe the egyptian",
            author_key="Waltari, Mika",
            year=1949,
            language="eng",
            work_type="text",
        ),
        MatchRecord(
            record_id="rec_linna_1",
            format="marcxml",
            title="Tuntematon sotilas",
            title_stem="tuntematon sotilas",
            author_key="Linna, Väinö",
            year=1954,
            language="fin",
            work_type="text",
        ),
        MatchRecord(
            record_id="rec_linna_2",
            format="marcxml",
            title="The Unknown Soldier",
            title_stem="unknown soldier",
            author_key="Linna, Väinö",
            year=1957,
            language="eng",
            work_type="text",
        ),
        MatchRecord(
            record_id="rec_kalevala_1",
            format="marcxml",
            title="Kalevala",
            title_stem="kalevala",
            author_key="Lönnrot, Elias",
            year=1849,
            language="fin",
            work_type="text",
        ),
    ]


# ---------------------------------------------------------------------------
# 1. Device and GPU Memory Tests
# ---------------------------------------------------------------------------


def test_device_resolution():
    """Verify compute device resolution handles auto, cpu, mps, and cuda correctly."""
    cpu_dev = resolve_compute_device("cpu")
    assert cpu_dev.type == "cpu"

    auto_dev = resolve_compute_device("auto")
    assert auto_dev.type in ("cpu", "mps", "cuda")

    # Fallback when requesting unavailable hardware
    res_cuda = resolve_compute_device("cuda")
    assert res_cuda.type in ("cuda", "cpu")

    res_mps = resolve_compute_device("mps")
    assert res_mps.type in ("mps", "cpu")

    # Clear memory should execute without error
    clear_accelerator_memory(auto_dev)
    assert isinstance(is_gpu_available(), bool)


# ---------------------------------------------------------------------------
# 2. Distance Computation Tests
# ---------------------------------------------------------------------------


def test_compute_pairwise_distances():
    """Verify GPU chunked pairwise distance matrices for cosine and euclidean metrics."""
    x = torch.tensor(
        [
            [1.0, 0.0],
            [0.0, 1.0],
            [1.0, 0.0],
        ],
        dtype=torch.float32,
    )
    cos_dist = compute_pairwise_distances(x, metric="cosine")
    assert cos_dist.shape == (3, 3)
    assert torch.isclose(cos_dist[0, 0], torch.tensor(0.0), atol=1e-4)
    assert torch.isclose(cos_dist[0, 2], torch.tensor(0.0), atol=1e-4)
    assert torch.isclose(cos_dist[0, 1], torch.tensor(1.0), atol=1e-4)

    euc_dist = compute_pairwise_distances(x, metric="euclidean")
    assert euc_dist.shape == (3, 3)
    assert torch.isclose(euc_dist[0, 0], torch.tensor(0.0), atol=1e-4)
    assert torch.isclose(euc_dist[0, 2], torch.tensor(0.0), atol=1e-4)


def test_compute_sparse_similarity_graph():
    """Verify sparse graph edge generation above threshold."""
    x = torch.tensor(
        [
            [1.0, 0.0],
            [1.0, 0.0],
            [0.0, 1.0],
        ],
        dtype=torch.float32,
    )
    rows, cols, weights = compute_sparse_similarity_graph(x, threshold=0.9, metric="cosine")
    # Row 0 connected to 1, row 1 connected to 0
    assert rows.numel() == 2
    assert cols.numel() == 2
    assert torch.all(weights >= 0.9)


# ---------------------------------------------------------------------------
# 3. Correlation Clustering (CC-Pivot) Tests
# ---------------------------------------------------------------------------


def test_cc_pivot_engine_with_vetoes():
    """Verify CCPivotEngine honors positive edges and negative vetoes."""
    x = torch.tensor(
        [
            [1.0, 0.0],
            [0.9, 0.1],  # Highly similar to 0
            [-1.0, 0.0], # Distant from 0
        ],
        dtype=torch.float32,
    )
    # Without veto, 0 and 1 group together
    engine = CCPivotEngine(positive_threshold=0.8, algorithm="parallel_pivot")
    res = engine.fit(x)
    assert res.num_clusters == 2
    assert res.assignments[0] == res.assignments[1]
    assert res.assignments[0] != res.assignments[2]

    # With negative veto between 0 and 1, they must separate
    veto = torch.zeros((3, 3), dtype=torch.bool)
    veto[0, 1] = True
    veto[1, 0] = True
    res_veto = engine.fit(x, negative_mask=veto)
    assert res_veto.num_clusters == 3
    assert res_veto.assignments[0] != res_veto.assignments[1]


def test_correlation_corpus_clusterer(
    sqlite_repo: WEMIRepository, sample_records: list[MatchRecord]
):
    """Verify CorrelationCorpusClusterer partitions sample records into 3 works."""
    embedder = MockDeterministicEmbedder()
    config = CorrelationClusteringConfig(positive_threshold=0.7)
    clusterer = CorrelationCorpusClusterer(
        corpus_repo=sqlite_repo,
        embedder=embedder,
        config=config,
    )

    result = clusterer.cluster_records(sample_records, persist=True)
    assert result.work_clusters_count == 3
    assert result.records_clustered == 5

    # Check persistence
    works = sqlite_repo.get_all_work_clusters()
    assert len(works) == 3


# ---------------------------------------------------------------------------
# 4. Multi-Tier Hierarchical Clustering Tests
# ---------------------------------------------------------------------------


def test_hierarchical_engine_and_cutter():
    """Verify GPUHierarchicalEngine constructs dendrogram and cuts at thresholds."""
    x = torch.tensor(
        [
            [1.0, 0.0],
            [0.99, 0.01],
            [-1.0, 0.0],
            [-0.99, 0.01],
        ],
        dtype=torch.float32,
    )
    engine = GPUHierarchicalEngine(linkage="average", metric="cosine")
    tree = engine.fit(x)
    assert isinstance(tree, HierarchicalTree)
    assert tree.num_samples == 4
    assert len(tree.linkage_matrix) == 3

    # Cut at tight threshold -> 2 clusters: (0, 1) and (2, 3)
    assigns = tree.cut_tree(distance_threshold=0.1)
    assert assigns[0] == assigns[1]
    assert assigns[2] == assigns[3]
    assert assigns[0] != assigns[2]


def test_hierarchical_wemi_clusterer(
    sqlite_repo: WEMIRepository, sample_records: list[MatchRecord]
):
    """Verify HierarchicalWEMIClusterer multi-tier partitioning."""
    embedder = MockDeterministicEmbedder()
    config = HierarchicalClusterConfig(
        work_distance_threshold=0.35,
        expression_distance_threshold=0.15,
    )
    clusterer = HierarchicalWEMIClusterer(
        corpus_repo=sqlite_repo,
        embedder=embedder,
        config=config,
    )

    result = clusterer.cluster_records(sample_records, persist=True)
    assert result.work_clusters_count == 3
    assert result.records_clustered == 5


# ---------------------------------------------------------------------------
# 5. Density-Based Clustering (DBSCAN) Tests
# ---------------------------------------------------------------------------


def test_gpu_dbscan_engine():
    """Verify GPUDBSCANEngine correctly identifies core points, clusters, and noise."""
    x = torch.tensor(
        [
            [1.0, 0.0],
            [1.0, 0.05],
            [1.0, -0.05],
            [0.0, 1.0],  # Outlier / noise point
        ],
        dtype=torch.float32,
    )
    # eps=0.2, min_samples=3: first 3 are core points, point 3 is noise
    engine = GPUDBSCANEngine(eps=0.2, min_samples=3, metric="cosine", noise_strategy="singleton")
    res = engine.fit(x)
    assert res.core_mask[0] and res.core_mask[1] and res.core_mask[2]
    assert not res.core_mask[3]
    assert res.assignments[0] == res.assignments[1] == res.assignments[2]
    assert res.assignments[3] != res.assignments[0]


def test_density_corpus_clusterer(
    sqlite_repo: WEMIRepository, sample_records: list[MatchRecord]
):
    """Verify DensityCorpusClusterer clusters staged records."""
    embedder = MockDeterministicEmbedder()
    config = DBSCANClusterConfig(eps=0.3, min_samples=2, noise_strategy="singleton")
    clusterer = DensityCorpusClusterer(
        corpus_repo=sqlite_repo,
        embedder=embedder,
        config=config,
    )

    result = clusterer.cluster_records(sample_records, persist=True)
    assert result.records_clustered == 5
    assert result.work_clusters_count >= 3


# ---------------------------------------------------------------------------
# 6. Markov Cluster Algorithm (MCL) Tests
# ---------------------------------------------------------------------------


def test_gpu_mcl_engine():
    """Verify GPUMarkovClusterEngine simulates flow and extracts clusters."""
    x = torch.tensor(
        [
            [1.0, 0.0],
            [0.95, 0.05],
            [0.0, 1.0],
            [0.05, 0.95],
        ],
        dtype=torch.float32,
    )
    engine = GPUMarkovClusterEngine(expansion_power=2, inflation_power=2.0, max_iterations=20)
    res = engine.fit(embeddings=x)
    assert res.num_clusters == 2
    assert res.assignments[0] == res.assignments[1]
    assert res.assignments[2] == res.assignments[3]
    assert res.assignments[0] != res.assignments[2]


def test_markov_corpus_clusterer(
    sqlite_repo: WEMIRepository, sample_records: list[MatchRecord]
):
    """Verify MarkovCorpusClusterer clusters sample records."""
    embedder = MockDeterministicEmbedder()
    config = MarkovClusterConfig(expansion_power=2, inflation_power=2.0)
    clusterer = MarkovCorpusClusterer(
        corpus_repo=sqlite_repo,
        embedder=embedder,
        config=config,
    )

    result = clusterer.cluster_records(sample_records, persist=True)
    assert result.work_clusters_count == 3
    assert result.records_clustered == 5


# ---------------------------------------------------------------------------
# 7. Bipartite Spectral Work-Agent Co-Clustering Tests
# ---------------------------------------------------------------------------


def test_bipartite_spectral_engine():
    """Verify GPUBipartiteSpectralEngine partitions bipartite incidence matrix."""
    # 4 works x 2 agents: works 0,1 with agent 0; works 2,3 with agent 1
    b = torch.tensor(
        [
            [1.0, 0.0],
            [1.0, 0.0],
            [0.0, 1.0],
            [0.0, 1.0],
        ],
        dtype=torch.float32,
    )
    engine = GPUBipartiteSpectralEngine(latent_dimensions=4, work_k=2, agent_k=2, iterations=20)
    res = engine.fit(b)
    assert res.num_work_clusters == 2
    assert res.work_assignments[0] == res.work_assignments[1]
    assert res.work_assignments[2] == res.work_assignments[3]
    assert res.work_assignments[0] != res.work_assignments[2]


def test_bipartite_co_clusterer(
    sqlite_repo: WEMIRepository, sample_records: list[MatchRecord]
):
    """Verify BipartiteCoClusterer partitions records by author associations."""
    config = BipartiteCoClusterConfig(latent_dimensions=8, work_k=3, agent_k=3)
    clusterer = BipartiteCoClusterer(
        corpus_repo=sqlite_repo,
        config=config,
    )

    result = clusterer.cluster_records(sample_records, persist=True)
    assert result.work_clusters_count == 3
    assert result.records_clustered == 5


# ---------------------------------------------------------------------------
# 8. Factory Routing Tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "method,expected_class",
    [
        ("correlation_pivot", CorrelationCorpusClusterer),
        ("hierarchical_wemi", HierarchicalWEMIClusterer),
        ("gpu_dbscan", DensityCorpusClusterer),
        ("gpu_mcl", MarkovCorpusClusterer),
        ("bipartite_coclust", BipartiteCoClusterer),
    ],
)
def test_create_corpus_clusterer_routing(
    sqlite_repo: WEMIRepository, method: str, expected_class: type
):
    """Verify that factory creates the correct clusterer instance for each configured method."""
    cfg = WEMIClusterConfig(method=method)  # type: ignore[arg-type]
    clusterer = create_corpus_clusterer(config=cfg, corpus_repo=sqlite_repo)
    assert isinstance(clusterer, expected_class)


# ---------------------------------------------------------------------------
# 9. Configuration Integrity Tests (Zero Hardcoding)
# ---------------------------------------------------------------------------


def test_clustering_configuration_defaults_and_overrides():
    """Verify defaults match defaults.yaml and allow clean parameter overrides."""
    from reconciliator.config.defaults import get_bundled_defaults

    raw_defaults = get_bundled_defaults()
    cluster_yaml = raw_defaults["reconcile"]["cluster"]

    corr_cfg = CorrelationClusteringConfig()
    assert corr_cfg.algorithm == cluster_yaml["correlation"]["algorithm"]
    assert corr_cfg.positive_threshold == cluster_yaml["correlation"]["positive_threshold"]
    assert corr_cfg.max_parallel_pivots == cluster_yaml["correlation"]["max_parallel_pivots"]
    assert corr_cfg.device == cluster_yaml["correlation"]["device"]

    # Verify custom overrides
    custom_corr = CorrelationClusteringConfig(positive_threshold=0.88, max_parallel_pivots=512)
    assert custom_corr.positive_threshold == 0.88
    assert custom_corr.max_parallel_pivots == 512

    hac_cfg = HierarchicalClusterConfig()
    assert hac_cfg.linkage == cluster_yaml["hierarchical"]["linkage"]
    assert hac_cfg.work_distance_threshold == cluster_yaml["hierarchical"]["work_distance_threshold"]
    assert hac_cfg.expression_distance_threshold == cluster_yaml["hierarchical"]["expression_distance_threshold"]

    dbscan_cfg = DBSCANClusterConfig()
    assert dbscan_cfg.eps == cluster_yaml["dbscan"]["eps"]
    assert dbscan_cfg.min_samples == cluster_yaml["dbscan"]["min_samples"]
    assert dbscan_cfg.noise_strategy == cluster_yaml["dbscan"]["noise_strategy"]

    mcl_cfg = MarkovClusterConfig()
    assert mcl_cfg.expansion_power == cluster_yaml["mcl"]["expansion_power"]
    assert mcl_cfg.inflation_power == cluster_yaml["mcl"]["inflation_power"]

    bip_cfg = BipartiteCoClusterConfig()
    assert bip_cfg.latent_dimensions == cluster_yaml["bipartite"]["latent_dimensions"]
    assert bip_cfg.work_k == cluster_yaml["bipartite"]["work_k"]
    assert bip_cfg.agent_k == cluster_yaml["bipartite"]["agent_k"]

