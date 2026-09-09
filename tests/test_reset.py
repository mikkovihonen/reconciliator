"""Unit tests for reset mechanics and schema reinitialization."""

from pathlib import Path
from reconciliator.config import StorageConfig
from reconciliator.db import get_db_connection, init_authority_db, reset_all_storage


def test_reset_all_storage(tmp_path: Path) -> None:
    storage = StorageConfig(
        authority_db=tmp_path / "auth.db",
        finto_db=tmp_path / "finto.db",
        progress_db=tmp_path / "prog.db",
        index_path=tmp_path / "auth.faiss",
    )

    # Initialize and populate a test row
    with get_db_connection(storage.authority_db) as conn:
        init_authority_db(conn)
        conn.execute("INSERT INTO db_metadata (key, value) VALUES ('test', '123');")

    storage.index_path.write_bytes(b"dummy index content")

    # Perform full reset
    reset_all_storage(storage)

    # Verify files exist and fresh schema is empty
    assert storage.authority_db.exists()
    assert storage.finto_db.exists()
    assert storage.progress_db.exists()
    assert not storage.index_path.exists()

    with get_db_connection(storage.authority_db) as conn:
        row = conn.execute("SELECT * FROM db_metadata WHERE key = 'test';").fetchone()
        assert row is None

