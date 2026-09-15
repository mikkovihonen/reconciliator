"""Unit tests verifying SQLite WAL mode concurrent access across threads."""

import concurrent.futures
from pathlib import Path

from reconciliator.db import (
    get_db_connection,
    get_metadata,
    init_authority_db,
    set_metadata,
)


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
                    INSERT INTO works (
                        work_uri, source, source_id, title, title_normalized, work_type
                    ) VALUES (?, 'melinda', ?, ?, ?, 'text');
                    """,
                    (
                        f"urn:work:melinda:REC_{worker_id}_{i}",
                        f"REC_{worker_id}_{i}",
                        f"Title {worker_id} - {i}",
                        f"title {worker_id} - {i}",
                    ),
                )
        return worker_id

    def read_worker() -> int:
        with get_db_connection(db_path) as conn:
            cursor = conn.execute("SELECT COUNT(*) as cnt FROM works;")
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
        total = conn.execute("SELECT COUNT(*) as cnt FROM works;").fetchone()["cnt"]
        assert total == 4 * 20


def test_db_metadata_persistence(tmp_path: Path) -> None:
    """db_metadata survives the AuthorityStore removal and still round-trips."""
    db_path = tmp_path / "authority.db"
    with get_db_connection(db_path) as conn:
        init_authority_db(conn)
        set_metadata(conn, "embedding_model_name", "TurkuNLP/sbert-finnish")
        set_metadata(conn, "embedding_dimension", "768")
        set_metadata(conn, "embedding_template_hash", "abc123hash")

    # Re-open to prove the values were committed, not just cached.
    with get_db_connection(db_path) as conn:
        assert get_metadata(conn, "embedding_model_name") == "TurkuNLP/sbert-finnish"
        assert get_metadata(conn, "embedding_dimension") == "768"
        assert get_metadata(conn, "embedding_template_hash") == "abc123hash"
        assert get_metadata(conn, "no_such_key") is None

    # Upsert replaces rather than duplicates.
    with get_db_connection(db_path) as conn:
        set_metadata(conn, "embedding_dimension", "1024")
        assert get_metadata(conn, "embedding_dimension") == "1024"
