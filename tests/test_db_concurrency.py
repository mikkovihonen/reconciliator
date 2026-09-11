"""Unit tests verifying SQLite WAL mode concurrent access across threads."""

import concurrent.futures
from pathlib import Path

from reconciliator.db import get_db_connection, init_authority_db


def test_concurrent_wal_writes_and_reads(tmp_path: Path) -> None:
    db_path = tmp_path / "concurrency_test.db"

    # Initialize schema
    with get_db_connection(db_path) as conn:
        init_authority_db(conn)

    def write_worker(worker_id: int) -> int:
        with get_db_connection(db_path) as conn:
            for i in range(20):
                conn.execute(
                    """
                    INSERT INTO authority_records (
                        source, source_id, title
                    ) VALUES ('melinda', ?, ?);
                    """,
                    (f"REC_{worker_id}_{i}", f"Title {worker_id} - {i}"),
                )
        return worker_id

    def read_worker() -> int:
        with get_db_connection(db_path) as conn:
            cursor = conn.execute("SELECT COUNT(*) as cnt FROM authority_records;")
            row = cursor.fetchone()
            return int(row["cnt"])

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        write_futures = [executor.submit(write_worker, wid) for wid in range(4)]
        read_futures = [executor.submit(read_worker) for _ in range(4)]

        for f in write_futures:
            f.result()
        for f in read_futures:
            f.result()

    # Verify total rows committed
    with get_db_connection(db_path) as conn:
        total = conn.execute("SELECT COUNT(*) as cnt FROM authority_records;").fetchone()["cnt"]
        assert total == 4 * 20


def test_db_metadata_persistence(tmp_path: Path) -> None:
    from reconciliator.repository import AuthorityStore

    store = AuthorityStore(
        db_path=tmp_path / "authority.db",
        index_path=tmp_path / "authority.faiss",
        dimension=768,
    )
    store.set_embedding_metadata(
        model_name="TurkuNLP/sbert-finnish",
        dimension=768,
        template_hash="abc123hash",
    )

    meta = store.get_embedding_metadata()
    assert meta["model_name"] == "TurkuNLP/sbert-finnish"
    assert meta["dimension"] == "768"
    assert meta["template_hash"] == "abc123hash"
