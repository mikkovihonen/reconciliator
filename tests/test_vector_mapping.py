"""Unit tests for vector index ID mapping, idempotent updates, and engine parity."""

from pathlib import Path

import numpy as np
import pytest

from reconciliator.config import VectorEngineConfig
from reconciliator.indexer import FAISSIndexEngine, TorchVectorEngine
from reconciliator.repository import WEMIRepository


def test_faiss_index_engine_explicit_ids(tmp_path: Path) -> None:
    index_file = tmp_path / "test.index"
    dim = 64
    mgr = FAISSIndexEngine(index_file, dimension=dim)
    assert mgr.total_vectors == 0

    # Add 3 vectors with explicit non-sequential IDs
    vectors = np.random.randn(3, dim).astype(np.float32)
    ids = [101, 202, 303]
    added_ids = mgr.add_vectors(vectors, ids=ids)
    assert added_ids == ids
    assert mgr.total_vectors == 3

    # Search nearest neighbors
    scores, found_ids = mgr.search(vectors[0:1], top_k=1)
    assert found_ids[0][0] == 101

    # Idempotent update: update ID 202 with a new vector
    new_v202 = np.random.randn(1, dim).astype(np.float32)
    mgr.add_vectors(new_v202, ids=[202])
    assert mgr.total_vectors == 3

    scores, found_ids = mgr.search(new_v202, top_k=1)
    assert found_ids[0][0] == 202

    # Remove ID 303
    removed = mgr.remove_ids([303])
    assert removed == 1
    assert mgr.total_vectors == 2

    # Save and reload
    mgr.save()
    reloaded = FAISSIndexEngine(index_file, dimension=dim)
    assert reloaded.total_vectors == 2
    scores, found_ids = reloaded.search(vectors[0:1], top_k=1)
    assert found_ids[0][0] == 101


def test_torch_vector_engine_explicit_ids(tmp_path: Path) -> None:
    index_file = tmp_path / "test_torch.index"
    dim = 64
    mgr = TorchVectorEngine(index_file, dimension=dim, device="cpu")
    assert mgr.total_vectors == 0

    # Add 3 vectors with explicit non-sequential IDs
    vectors = np.random.randn(3, dim).astype(np.float32)
    ids = [101, 202, 303]
    added_ids = mgr.add_vectors(vectors, ids=ids)
    assert added_ids == ids
    assert mgr.total_vectors == 3

    # Search nearest neighbors
    scores, found_ids = mgr.search(vectors[0:1], top_k=1)
    assert found_ids[0][0] == 101

    # Idempotent update: update ID 202 with a new vector
    new_v202 = np.random.randn(1, dim).astype(np.float32)
    mgr.add_vectors(new_v202, ids=[202])
    assert mgr.total_vectors == 3

    scores, found_ids = mgr.search(new_v202, top_k=1)
    assert found_ids[0][0] == 202

    # Remove ID 303
    removed = mgr.remove_ids([303])
    assert removed == 1
    assert mgr.total_vectors == 2

    # Save with safetensors and reload
    mgr.save()
    reloaded = TorchVectorEngine(index_file, dimension=dim, device="cpu")
    assert reloaded.total_vectors == 2
    scores, found_ids = reloaded.search(vectors[0:1], top_k=1)
    assert found_ids[0][0] == 101


@pytest.mark.parametrize("backend", ["faiss", "torch"])
def test_wemi_repository_idempotent_ingest(tmp_path: Path, backend: str) -> None:
    from reconciliator.models import WorkEntity

    db_path = tmp_path / f"authority_{backend}.db"
    index_path = tmp_path / f"authority_{backend}.index"
    dim = 64

    cfg = VectorEngineConfig(backend=backend, device="cpu")  # type: ignore[arg-type]
    store = WEMIRepository(db_path, index_path, dimension=dim, vector_engine_config=cfg)
    v1 = np.random.randn(1, dim).astype(np.float32)

    # Initial insert
    work = WorkEntity(
        work_uri="http://example.org/work/MEL-001",
        source="melinda",
        source_id="MEL-001",
        title="First Title",
        title_normalized="first title",
        work_type="text",
    )
    work_id = store.insert_work(work, vector=v1[0])
    assert work_id is not None
    assert store.work_indexer.total_vectors == 1

    # Search should find the work
    cands = store.search_work_vectors(v1, top_k=5)
    assert len(cands) == 1
    assert cands[0][0].source_id == "MEL-001"
    assert cands[0][0].title == "First Title"

    # Update work with modified title and new vector
    v1_updated = np.random.randn(1, dim).astype(np.float32)
    work_updated = WorkEntity(
        work_uri="http://example.org/work/MEL-001",
        source="melinda",
        source_id="MEL-001",
        title="Updated Title",
        title_normalized="updated title",
        work_type="text",
    )
    updated_work_id = store.insert_work(work_updated, vector=v1_updated[0])
    assert updated_work_id == work_id
    # Total vectors must remain 1, not 2!
    assert store.work_indexer.total_vectors == 1

    # Verify vector mappings in sqlite
    with store.get_connection() as conn:
        rows = conn.execute("SELECT work_id FROM work_vectors;").fetchall()
        assert len(rows) == 1

    # Search with new vector
    cands2 = store.search_work_vectors(v1_updated, top_k=5)
    assert len(cands2) == 1
    assert cands2[0][0].source_id == "MEL-001"
    assert cands2[0][0].title == "Updated Title"
