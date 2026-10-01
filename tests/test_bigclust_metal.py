"""Unit and integration test suite for BigClust on Metal GPU.

Tests:
1. SLMBA nonsmooth bundle optimizer convergence on Metal (MPS).
2. Incremental Global Algorithm (GIA) solving intermediate l = 1..k problems.
3. Automatic optimal k* selection via Davies-Bouldin index minimization.
4. Wide Pilot: Hierarchical Work Family Partitioning & Expression Realization Partitioning.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import numpy as np
import pytest
import torch

from reconciliator.clustering.bigclust import (
    BigClustMetal,
    BigClustMultiplexClusterer,
    MetalSLMBAOptimizer,
    SLMBAParameters,
    compute_validity_indices,
)
from reconciliator.database import WEMIRepository
from reconciliator.models import MatchRecord


@pytest.fixture
def device() -> torch.device:
    """Fixture providing optimal device (preferring Metal MPS)."""
    if torch.backends.mps.is_available():
        return torch.device("mps")
    elif torch.cuda.is_available():
        return torch.device("cuda")
    return torch.device("cpu")


from unittest.mock import MagicMock

@pytest.fixture
def mock_repo() -> MagicMock:
    """Fixture providing a mock WEMIRepository without requiring PostgreSQL."""
    return MagicMock()


class MockSentenceEmbedder:
    """Deterministic mock embedder for clustering unit tests."""

    def __init__(self, dim: int = 64) -> None:
        self.dim = dim

    def embed_texts(self, texts: list[str]) -> np.ndarray:
        """Deterministic pseudo-embeddings based on text tokens."""
        embeddings = np.zeros((len(texts), self.dim), dtype=np.float32)
        for i, text in enumerate(texts):
            # Seed based on content to create well-separated semantic clusters
            low = text.lower()
            if "sinuhe" in low or "waltari" in low:
                base = np.array([2.0, 2.0] + [0.0] * (self.dim - 2), dtype=np.float32)
            elif "tuntematon" in low or "linna" in low or "soldier" in low:
                base = np.array([-2.0, -2.0] + [0.0] * (self.dim - 2), dtype=np.float32)
            elif "kalevala" in low or "lonnrot" in low:
                base = np.array([2.0, -2.0] + [0.0] * (self.dim - 2), dtype=np.float32)
            else:
                base = np.zeros(self.dim, dtype=np.float32)

            # Expression-level variation (e.g. language/translation)
            noise = np.zeros(self.dim, dtype=np.float32)
            if "swedish" in low or "svenska" in low:
                noise[2] = 0.8
            elif "english" in low:
                noise[2] = -0.8
            elif "audio" in low or "spoken_word" in low:
                noise[3] = 0.9

            embeddings[i] = base + noise
        return embeddings


def test_slmba_optimizer_convergence(device):
    """Verify that SLMBA converges and reduces MSSC clustering loss on Metal."""
    torch.manual_seed(42)
    # Generate 3 distinct synthetic clusters in R^16
    c1 = torch.randn(1, 16) + 3.0
    c2 = torch.randn(1, 16) - 3.0
    c3 = torch.randn(1, 16) + torch.tensor([3.0, -3.0] + [0.0] * 14)

    pts1 = c1 + 0.3 * torch.randn(50, 16)
    pts2 = c2 + 0.3 * torch.randn(50, 16)
    pts3 = c3 + 0.3 * torch.randn(50, 16)
    data = torch.cat([pts1, pts2, pts3], dim=0)

    # Suboptimal random initial centers
    init_X = torch.randn(3, 16)

    params = SLMBAParameters(max_iter=150, bundle_size=5)
    optimizer = MetalSLMBAOptimizer(params=params, device=device)

    # Initial loss
    initial_loss, _ = optimizer._eval_cluster_loss_and_grad(init_X.to(device), data.to(device))
    best_X, final_loss = optimizer.optimize_clustering(init_X, data)

    assert final_loss < initial_loss * 0.4, (
        f"Expected significant loss reduction. Initial: {initial_loss}, Final: {final_loss}"
    )
    assert best_X.shape == (3, 16)
    assert best_X.device.type == device.type


def test_bigclust_incremental_fit_and_k_selection(device):
    """Verify GIA incremental loop (l=1..k_max) and automatic k* selection."""
    torch.manual_seed(123)
    dim = 8
    # 4 distinct synthetic clusters of 40 points each
    centers_gt = torch.tensor([
        [5.0, 0.0] + [0.0] * (dim - 2),
        [-5.0, 0.0] + [0.0] * (dim - 2),
        [0.0, 5.0] + [0.0] * (dim - 2),
        [0.0, -5.0] + [0.0] * (dim - 2),
    ])
    points = []
    for c in centers_gt:
        points.append(c.unsqueeze(0) + 0.4 * torch.randn(40, dim))
    data = torch.cat(points, dim=0)

    engine = BigClustMetal(k_max=6, k_min=2, max_candidate_points=10, device=device)
    result = engine.fit(data)

    # 1. Intermediate solutions exist for all l = 1..6
    for l in range(1, 7):
        assert l in result.intermediate_solutions
        sol = result.intermediate_solutions[l]
        assert sol.centroids.shape == (l, dim)
        assert sol.loss > 0.0

    # 2. Optimal k* should match ground truth (4 clusters)
    assert result.optimal_k == 4, f"Expected k*=4, got k*={result.optimal_k}"
    assert result.optimal_centroids.shape == (4, dim)
    assert len(result.assignments) == 160

    # 3. Validity metrics
    assert result.optimal_metrics.davies_bouldin < 1.0
    assert result.optimal_metrics.dunn_index > 0.0


def test_bigclust_multiplex_wide_pilot(mock_repo, device):
    """Verify Wide Pilot: Work Family Partitioning & Expression Realization Partitioning."""
    records = [
        # Work 1: Sinuhe (Waltari) - 3 expressions (FI original, SV translation, Audio)
        MatchRecord(
            record_id="rec_sinuhe_fi",
            format="marcxml",
            work_type="text",
            title="Sinuhe egyptiläinen",
            title_stem="sinuhe egyptilainen",
            author_key="waltari",
            language="fin",
            year=1945,
            raw_payload=b"",
        ),
        MatchRecord(
            record_id="rec_sinuhe_sv",
            format="marcxml",
            work_type="text",
            title="Sinuhe egyptiern",
            title_stem="sinuhe egyptiern",
            author_key="waltari",
            language="swe",
            year=1946,
            raw_payload=b"",
        ),
        MatchRecord(
            record_id="rec_sinuhe_audio",
            format="marcxml",
            work_type="audio_nonmusic",
            title="Sinuhe egyptiläinen (äänikirja)",
            title_stem="sinuhe egyptilainen aanikirja",
            author_key="waltari",
            language="fin",
            year=2000,
            raw_payload=b"",
        ),
        # Work 2: Tuntematon sotilas (Linna) - 2 expressions (FI text, EN translation)
        MatchRecord(
            record_id="rec_tuntematon_fi",
            format="marcxml",
            work_type="text",
            title="Tuntematon sotilas",
            title_stem="tuntematon sotilas",
            author_key="linna",
            language="fin",
            year=1954,
            raw_payload=b"",
        ),
        MatchRecord(
            record_id="rec_tuntematon_en",
            format="marcxml",
            work_type="text",
            title="The Unknown Soldier",
            title_stem="unknown soldier",
            author_key="linna",
            language="eng",
            year=1957,
            raw_payload=b"",
        ),
        # Work 3: Kalevala (Lönnrot) - 1 expression
        MatchRecord(
            record_id="rec_kalevala",
            format="marcxml",
            work_type="text",
            title="Kalevala",
            title_stem="kalevala",
            author_key="lonnrot",
            language="fin",
            year=1849,
            raw_payload=b"",
        ),
    ]

    embedder = MockSentenceEmbedder(dim=64)
    clusterer = BigClustMultiplexClusterer(
        corpus_repo=mock_repo,
        embedder=embedder,
        max_work_clusters=5,
        max_expression_clusters_per_work=4,
        device=device,
    )

    res = clusterer.cluster_records(records, target_work_k=3)

    # Assertions on wide pilot results
    assert res.records_clustered == 6
    assert res.work_clusters_count == 3, f"Expected 3 Work clusters, got {res.work_clusters_count}"
    # Expressions: Sinuhe has 3, Tuntematon has 2, Kalevala has 1 -> Total 6
    assert res.expression_clusters_count >= 3
    assert res.manifestation_clusters_count == 6
    assert res.elapsed_seconds > 0.0
