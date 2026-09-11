"""Unit tests for FAISS direct ID mapping and idempotent updates."""

from pathlib import Path

import faiss
import numpy as np

from reconciliator.indexer import FAISSIndexManager
from reconciliator.models import Agent, BibliographicRecord
from reconciliator.repository import AuthorityStore


def test_faiss_index_manager_explicit_ids(tmp_path: Path) -> None:
    index_file = tmp_path / "test.faiss"
    dim = 64
    mgr = FAISSIndexManager(index_file, dimension=dim)
    assert mgr.total_vectors == 0
    assert isinstance(mgr.index, (faiss.IndexIDMap, faiss.IndexIDMap2))

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
    reloaded = FAISSIndexManager(index_file, dimension=dim)
    assert reloaded.total_vectors == 2
    scores, found_ids = reloaded.search(vectors[0:1], top_k=1)
    assert found_ids[0][0] == 101


def test_authority_store_idempotent_ingest(tmp_path: Path) -> None:
    db_path = tmp_path / "authority.db"
    index_path = tmp_path / "authority.faiss"
    dim = 64

    store = AuthorityStore(db_path, index_path, dimension=dim)
    rec1 = BibliographicRecord(
        source_id="MEL-001",
        source="melinda",
        title="First Title",
        agents=[Agent(name="Author One", surname="One", role="aut")],
    )
    v1 = np.random.randn(1, dim).astype(np.float32)

    # Initial insert
    rec_ids = store.insert_records_batch([rec1], vectors=v1)
    assert len(rec_ids) == 1
    orig_id = rec_ids[0]
    assert store.indexer.total_vectors == 1

    # Search should find rec1
    cands = store.search_vector_candidates(v1, top_k=5)
    assert len(cands) == 1
    assert cands[0][0].source_id == "MEL-001"
    assert cands[0][0].title == "First Title"

    # Update rec1 with modified title and new vector
    rec1_updated = BibliographicRecord(
        source_id="MEL-001",
        source="melinda",
        title="Updated Title",
        agents=[Agent(name="Author One", surname="One", role="aut")],
    )
    v1_updated = np.random.randn(1, dim).astype(np.float32)
    updated_rec_ids = store.insert_records_batch([rec1_updated], vectors=v1_updated)
    assert updated_rec_ids == [orig_id]
    # Total vectors must remain 1, not 2!
    assert store.indexer.total_vectors == 1

    # Verify vector mappings in sqlite
    with store.get_connection() as conn:
        rows = conn.execute("SELECT faiss_id, record_id FROM vector_mappings;").fetchall()
        assert len(rows) == 1
        assert rows[0]["faiss_id"] == orig_id
        assert rows[0]["record_id"] == orig_id

        # Verify FTS was cleaned up and updated
        fts_rows = conn.execute(
            "SELECT title FROM authority_fts WHERE record_id = ?;", (orig_id,)
        ).fetchall()
        assert len(fts_rows) == 1
        assert fts_rows[0]["title"] == "Updated Title"

    # Search with new vector
    cands_updated = store.search_vector_candidates(v1_updated, top_k=5)
    assert len(cands_updated) == 1
    assert cands_updated[0][0].source_id == "MEL-001"
    assert cands_updated[0][0].title == "Updated Title"

