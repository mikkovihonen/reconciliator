"""Unit tests for USearchVectorEngine implementation, quantization, and lifecycle."""

from pathlib import Path

import numpy as np
import pytest

from reconciliator.config import USearchEngineConfig, VectorEngineConfig
from reconciliator.indexer import (
    FAISSIndexEngine,
    USearchVectorEngine,
    create_vector_engine,
)
from reconciliator.indexer.ipc.pool import IndexPoolManager


def test_usearch_basic_lifecycle(tmp_path: Path) -> None:
    index_path = tmp_path / "index_usearch.bin"
    dim = 32

    cfg = VectorEngineConfig(
        backend="usearch",
        usearch=USearchEngineConfig(dtype="f16", metric="cos", mmap=False),
    )
    engine = create_vector_engine(index_path, dimension=dim, config=cfg)
    assert isinstance(engine, USearchVectorEngine)
    assert engine.total_vectors == 0

    # Search on empty index
    scores, indices = engine.search(np.random.randn(2, dim).astype(np.float32), top_k=5)
    assert scores.shape == (2, 0)
    assert indices.shape == (2, 0)

    # Insert 10 vectors with explicit integer IDs
    np.random.seed(42)
    vecs = np.random.randn(10, dim).astype(np.float32)
    ids = list(range(100, 110))
    assigned = engine.add_vectors(vecs, ids=ids)
    assert assigned == ids
    assert engine.total_vectors == 10

    # Query with top_k = 3
    q = vecs[0:1]
    scores, found = engine.search(q, top_k=3)
    assert found.shape == (1, 3)
    assert found[0][0] == 100
    assert pytest.approx(float(scores[0][0]), abs=1e-3) == 1.0

    # Metadata saving and loading
    engine.save(metadata={"model_name": "test-sbert", "template_hash": "hash1234"})
    assert (tmp_path / "index_usearch.bin.meta.json").exists()

    # Verify metadata
    engine.verify_metadata(expected_model="test-sbert", expected_dimension=dim)
    with pytest.raises(ValueError, match="Index model mismatch"):
        engine.verify_metadata(expected_model="wrong-model", expected_dimension=dim)

    # Reload from disk
    reloaded = create_vector_engine(index_path, dimension=dim, config=cfg)
    assert reloaded.total_vectors == 10
    scores2, found2 = reloaded.search(q, top_k=3)
    assert found2[0][0] == 100
    assert pytest.approx(float(scores2[0][0]), abs=1e-3) == 1.0

    # Deletion
    removed = reloaded.remove_ids([100, 105])
    assert removed == 2
    assert reloaded.total_vectors == 8
    scores3, found3 = reloaded.search(q, top_k=3)
    assert 100 not in found3[0]


def test_usearch_auto_assign_ids(tmp_path: Path) -> None:
    index_path = tmp_path / "auto_ids.usearch"
    dim = 16
    engine = USearchVectorEngine(index_path, dimension=dim)

    vecs = np.random.randn(5, dim).astype(np.float32)
    assigned = engine.add_vectors(vecs)
    assert assigned == [0, 1, 2, 3, 4]
    assert engine.total_vectors == 5

    vecs2 = np.random.randn(3, dim).astype(np.float32)
    assigned2 = engine.add_vectors(vecs2)
    assert assigned2 == [5, 6, 7]
    assert engine.total_vectors == 8


def test_usearch_idempotent_updates(tmp_path: Path) -> None:
    index_path = tmp_path / "idempotent.usearch"
    dim = 16
    engine = USearchVectorEngine(index_path, dimension=dim)

    v1 = np.array([[1.0] + [0.0] * (dim - 1)], dtype=np.float32)
    engine.add_vectors(v1, ids=[42])
    assert engine.total_vectors == 1

    # Re-adding with same ID updates without duplicate key error
    v2 = np.array([[0.0, 1.0] + [0.0] * (dim - 2)], dtype=np.float32)
    engine.add_vectors(v2, ids=[42])
    assert engine.total_vectors == 1

    # Search should now find v2
    scores, indices = engine.search(v2, top_k=1)
    assert indices[0][0] == 42
    assert pytest.approx(float(scores[0][0]), abs=1e-3) == 1.0


@pytest.mark.parametrize("dtype", ["f32", "f16", "i8"])
def test_usearch_quantization_dtypes(tmp_path: Path, dtype: str) -> None:
    index_path = tmp_path / f"quant_{dtype}.usearch"
    dim = 64
    np.random.seed(123)

    engine = USearchVectorEngine(index_path, dimension=dim, dtype=dtype)  # type: ignore[arg-type]
    vecs = np.random.randn(20, dim).astype(np.float32)
    ids = list(range(20))
    engine.add_vectors(vecs, ids=ids)
    assert engine.total_vectors == 20

    # Query with one of the vectors
    q = vecs[5:6]
    scores, found = engine.search(q, top_k=5)
    assert found[0][0] == 5
    # i8 scalar quantization has small quantization loss, but similarity should still be > 0.95
    assert scores[0][0] > 0.95


def test_usearch_mmap_view_mode(tmp_path: Path) -> None:
    index_path = tmp_path / "mmap_test.usearch"
    dim = 32
    np.random.seed(42)

    # 1. Build and save index
    builder = USearchVectorEngine(index_path, dimension=dim, mmap=False)
    vecs = np.random.randn(25, dim).astype(np.float32)
    builder.add_vectors(vecs, ids=list(range(25)))
    builder.save()

    # 2. Open with mmap=True (view=True)
    viewer = USearchVectorEngine(index_path, dimension=dim, mmap=True)
    assert viewer._is_view is True
    assert viewer.total_vectors == 25

    q = vecs[7:8]
    scores, found = viewer.search(q, top_k=3)
    assert found[0][0] == 7
    assert pytest.approx(float(scores[0][0]), abs=1e-3) == 1.0

    # 3. Test seamless mutation: adding vectors to a viewed index auto-transitions to mutable
    new_v = np.random.randn(2, dim).astype(np.float32)
    viewer.add_vectors(new_v, ids=[100, 101])
    assert viewer.total_vectors == 27
    assert viewer._is_view is False


def test_usearch_parity_with_faiss(tmp_path: Path) -> None:
    dim = 32
    np.random.seed(42)
    vecs = np.random.randn(50, dim).astype(np.float32)
    queries = np.random.randn(5, dim).astype(np.float32)
    ids = list(range(50))

    faiss_engine = FAISSIndexEngine(tmp_path / "faiss.index", dimension=dim)
    usearch_engine = USearchVectorEngine(
        tmp_path / "usearch.index", dimension=dim, dtype="f32", expansion_search=128
    )

    faiss_engine.add_vectors(vecs, ids=ids)
    usearch_engine.add_vectors(vecs, ids=ids)

    f_scores, f_ids = faiss_engine.search(queries, top_k=5)
    u_scores, u_ids = usearch_engine.search(queries, top_k=5)

    # Top-1 should match across all 5 queries
    for q_idx in range(5):
        assert f_ids[q_idx][0] == u_ids[q_idx][0]
        assert pytest.approx(float(f_scores[q_idx][0]), abs=1e-3) == float(u_scores[q_idx][0])


def test_usearch_ipc_pool_manager(tmp_path: Path) -> None:
    dim = 16
    cfg = VectorEngineConfig(
        backend="usearch",
        usearch=USearchEngineConfig(dtype="f16", metric="cos"),
    )
    pool = IndexPoolManager(cfg)
    idx_path = tmp_path / "ipc_usearch.bin"

    managed = pool.get_or_open_index(idx_path, dimension=dim)
    assert isinstance(managed.engine, USearchVectorEngine)

    vecs = np.random.randn(5, dim).astype(np.float32)
    managed.engine.add_vectors(vecs, ids=[1, 2, 3, 4, 5])
    assert managed.engine.total_vectors == 5

    scores, found = managed.engine.search(vecs[2:3], top_k=1)
    assert found[0][0] == 3
