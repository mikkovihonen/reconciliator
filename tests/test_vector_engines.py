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


@pytest.mark.parametrize("backend", ["faiss", "torch"])
def test_engine_basic_lifecycle(tmp_path: Path, backend: str) -> None:
    index_path = tmp_path / f"index_{backend}.bin"
    dim = 32

    cfg = VectorEngineConfig(backend=backend, device="cpu")  # type: ignore[arg-type]
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

