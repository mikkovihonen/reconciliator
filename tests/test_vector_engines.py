"""Unit tests for vector indexing engines parity and factory resolution."""

from pathlib import Path

import numpy as np
import pytest
import torch

from reconciliator.config import VectorEngineConfig
from reconciliator.indexer import (
    FAISSIndexEngine,
    TorchVectorEngine,
    create_vector_engine,
)


@pytest.mark.parametrize("backend", ["faiss", "torch", "usearch"])
def test_engine_basic_lifecycle(tmp_path: Path, backend: str) -> None:
    index_path = tmp_path / f"index_{backend}.bin"
    dim = 32

    cfg = VectorEngineConfig(backend=backend, device="cpu", mode="embedded")  # type: ignore[arg-type]
    engine = create_vector_engine(index_path, dimension=dim, config=cfg)
    assert engine.total_vectors == 0

    # Search on empty index
    scores, indices = engine.search(np.random.randn(2, dim).astype(np.float32), top_k=5)
    assert scores.shape == (2, 0)
    assert indices.shape == (2, 0)

    # Insert 10 vectors
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
    assert pytest.approx(float(scores[0][0]), abs=1e-4) == 1.0

    # Metadata saving and loading
    engine.save(metadata={"model_name": "test-model", "template_hash": "abc12345"})
    assert (tmp_path / f"index_{backend}.bin.meta.json").exists()

    # Verify metadata
    engine.verify_metadata(expected_model="test-model", expected_dimension=dim)
    with pytest.raises(ValueError, match="Index model mismatch"):
        engine.verify_metadata(expected_model="wrong-model", expected_dimension=dim)

    # Reload from disk
    reloaded = create_vector_engine(index_path, dimension=dim, config=cfg)
    assert reloaded.total_vectors == 10
    scores2, found2 = reloaded.search(q, top_k=3)
    assert found2[0][0] == 100

    # Deletion
    removed = reloaded.remove_ids([100, 105])
    assert removed == 2
    assert reloaded.total_vectors == 8
    scores3, found3 = reloaded.search(q, top_k=3)
    assert 100 not in found3[0]


def test_engines_search_score_parity(tmp_path: Path) -> None:
    dim = 16
    np.random.seed(42)
    vecs = np.random.randn(20, dim).astype(np.float32)
    queries = np.random.randn(5, dim).astype(np.float32)

    faiss_engine = FAISSIndexEngine(tmp_path / "faiss.index", dimension=dim)
    torch_engine = TorchVectorEngine(tmp_path / "torch.index", dimension=dim, device="cpu")

    faiss_engine.add_vectors(vecs, ids=list(range(20)))
    torch_engine.add_vectors(vecs, ids=list(range(20)))

    f_scores, f_ids = faiss_engine.search(queries, top_k=5)
    t_scores, t_ids = torch_engine.search(queries, top_k=5)

    np.testing.assert_array_equal(f_ids, t_ids)
    np.testing.assert_allclose(f_scores, t_scores, atol=1e-5)


def test_torch_mps_hardware_acceleration(tmp_path: Path) -> None:
    if not (hasattr(torch.backends, "mps") and torch.backends.mps.is_available()):
        pytest.skip("Apple Silicon MPS hardware acceleration not available")

    dim = 64
    engine = TorchVectorEngine(tmp_path / "mps.index", dimension=dim, device="mps")
    assert str(engine.device) == "mps"

    vecs = np.random.randn(15, dim).astype(np.float32)
    engine.add_vectors(vecs, ids=list(range(15)))
    assert engine.total_vectors == 15

    scores, found = engine.search(vecs[2:3], top_k=3)
    assert found[0][0] == 2
    assert pytest.approx(float(scores[0][0]), abs=1e-4) == 1.0

    # Safetensors save & load roundtrip on MPS
    engine.save({"test": "mps"})
    reloaded = TorchVectorEngine(tmp_path / "mps.index", dimension=dim, device="mps")
    assert reloaded.total_vectors == 15
    scores2, found2 = reloaded.search(vecs[2:3], top_k=1)
    assert found2[0][0] == 2


def test_factory_auto_resolution(tmp_path: Path) -> None:
    engine = create_vector_engine(tmp_path / "auto.index", dimension=32)
    # On Apple Silicon with MPS available, auto resolves to TorchVectorEngine on mps
    if hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
        assert isinstance(engine, TorchVectorEngine)
        assert engine.device_str == "mps"
    elif torch.cuda.is_available():
        assert isinstance(engine, TorchVectorEngine)
        assert engine.device_str == "cuda"
    else:
        assert isinstance(engine, FAISSIndexEngine)


def test_torch_engine_preallocation_and_capacity_growth(tmp_path: Path) -> None:
    dim = 32
    engine = TorchVectorEngine(
        tmp_path / "growth.index", dimension=dim, device="cpu", initial_capacity=1000
    )
    assert engine.total_vectors == 0
    assert engine._capacity == 1000

    # Ingest 3,000 vectors across 6 batches of 500
    np.random.seed(42)
    all_vecs = []
    for i in range(6):
        batch = np.random.randn(500, dim).astype(np.float32)
        all_vecs.append(batch)
        ids = list(range(i * 500, (i + 1) * 500))
        assigned = engine.add_vectors(batch, ids=ids)
        assert assigned == ids

    assert engine.total_vectors == 3000
    assert engine._capacity >= 3000

    # Query first vector
    scores, found = engine.search(all_vecs[0][0:1], top_k=3)
    assert found[0][0] == 0
    assert pytest.approx(float(scores[0][0]), abs=1e-4) == 1.0

    # Save & reload
    engine.save()
    reloaded = TorchVectorEngine(tmp_path / "growth.index", dimension=dim, device="cpu")
    assert reloaded.total_vectors == 3000
    scores_r, found_r = reloaded.search(all_vecs[0][0:1], top_k=3)
    assert found_r[0][0] == 0


def test_torch_engine_in_place_id_updates(tmp_path: Path) -> None:
    dim = 16
    engine = TorchVectorEngine(tmp_path / "updates.index", dimension=dim, device="cpu")

    v1 = np.ones((1, dim), dtype=np.float32)
    v2 = np.ones((1, dim), dtype=np.float32) * 2.0
    v3 = np.ones((1, dim), dtype=np.float32) * 3.0
    batch1 = np.vstack([v1, v2, v3])

    engine.add_vectors(batch1, ids=[10, 20, 30])
    assert engine.total_vectors == 3

    # Re-insert with id 20 and 30 updated, plus new id 40
    v2_new = np.zeros((1, dim), dtype=np.float32)
    v2_new[0, 0] = 99.0
    v4 = np.ones((1, dim), dtype=np.float32) * 4.0
    batch2 = np.vstack([v2_new, v4])

    engine.add_vectors(batch2, ids=[20, 40])
    assert engine.total_vectors == 4

    # Search for v2_new
    scores, found = engine.search(v2_new, top_k=1)
    assert found[0][0] == 20
    assert pytest.approx(float(scores[0][0]), abs=1e-4) == 1.0


def test_torch_engine_zero_metal_leak_under_high_batch_churn(tmp_path: Path) -> None:
    if not (hasattr(torch.backends, "mps") and torch.backends.mps.is_available()):
        pytest.skip("Apple Silicon MPS hardware acceleration not available")

    dim = 128
    engine = TorchVectorEngine(tmp_path / "metal_leak.index", dimension=dim, device="mps")

    # Ingest 30 batches of 500 vectors (15,000 vectors)
    np.random.seed(123)
    for i in range(30):
        batch = np.random.randn(500, dim).astype(np.float32)
        engine.add_vectors(batch, ids=list(range(i * 500, (i + 1) * 500)))

    assert engine.total_vectors == 15000

    # Metal driver allocated memory should remain strictly under base context (~9 MB) because vectors are stored on host RAM
    if hasattr(torch.mps, "driver_allocated_memory"):
        driver_mem = torch.mps.driver_allocated_memory()
        # In the old code, this would have allocated > 2.6 GB. Now it stays strictly at the base context.
        assert driver_mem < 25 * 1024 * 1024, (
            f"Expected < 25 MB Metal driver memory, got {driver_mem / (1024 * 1024):.1f} MB"
        )

    # Search triggers MPS acceleration cleanly without leaking
    q = np.random.randn(5, dim).astype(np.float32)
    scores, found = engine.search(q, top_k=5)
    assert found.shape == (5, 5)


def test_torch_engine_float16_and_mmap(tmp_path: Path) -> None:
    dim = 32
    index_path = tmp_path / "f16.index"
    engine = TorchVectorEngine(index_path, dimension=dim, device="cpu", dtype="float16")
    assert engine.torch_dtype == torch.float16
    assert engine.metadata["dtype"] == "float16"

    vecs = np.random.randn(20, dim).astype(np.float32)
    engine.add_vectors(vecs, ids=list(range(20)))
    assert engine.total_vectors == 20
    assert engine.vectors.dtype == torch.float16

    scores, found = engine.search(vecs[0:1], top_k=3)
    assert found[0][0] == 0
    assert pytest.approx(float(scores[0][0]), abs=1e-3) == 1.0

    engine.save()

    # Load with mmap=True
    mmap_engine = TorchVectorEngine(
        index_path, dimension=dim, device="cpu", dtype="float16", mmap=True
    )
    assert mmap_engine.total_vectors == 20
    scores_m, found_m = mmap_engine.search(vecs[0:1], top_k=3)
    assert found_m[0][0] == 0
    assert pytest.approx(float(scores_m[0][0]), abs=1e-3) == 1.0


def test_faiss_engine_sq8_and_mmap(tmp_path: Path) -> None:
    dim = 32
    index_path = tmp_path / "sq8.index"
    engine = FAISSIndexEngine(index_path, dimension=dim, index_type="sq8")
    assert engine.index_type == "sq8"

    vecs = np.random.randn(50, dim).astype(np.float32)
    engine.add_vectors(vecs, ids=list(range(50)))
    assert engine.total_vectors == 50

    scores, found = engine.search(vecs[0:1], top_k=3)
    assert found[0][0] == 0

    engine.save()

    # Load with mmap=True
    mmap_engine = FAISSIndexEngine(index_path, dimension=dim, index_type="sq8", mmap=True)
    assert mmap_engine.total_vectors == 50
    scores_m, found_m = mmap_engine.search(vecs[0:1], top_k=3)
    assert found_m[0][0] == 0


def test_factory_nested_config_propagation(tmp_path: Path) -> None:
    from reconciliator.config import FAISSEngineConfig, TorchEngineConfig

    cfg = VectorEngineConfig(
        backend="torch",
        device="cpu",
        mode="embedded",
        torch=TorchEngineConfig(dtype="float16", chunk_size=25000, initial_capacity=5000),
    )
    t_engine = create_vector_engine(tmp_path / "cfg_torch.index", dimension=16, config=cfg)
    assert isinstance(t_engine, TorchVectorEngine)
    assert t_engine.torch_dtype == torch.float16
    assert t_engine.chunk_size == 25000
    assert t_engine.initial_capacity == 5000

    cfg_faiss = VectorEngineConfig(
        backend="faiss",
        mode="embedded",
        faiss=FAISSEngineConfig(index_type="sq8", mmap=True, nprobe=8),
    )
    f_engine = create_vector_engine(tmp_path / "cfg_faiss.index", dimension=16, config=cfg_faiss)
    assert isinstance(f_engine, FAISSIndexEngine)
    assert f_engine.index_type == "sq8"
    assert f_engine.mmap is True
    assert f_engine.nprobe == 8
