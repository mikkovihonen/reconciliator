"""SQLite database management, schema initialization, and WAL pragmas for Reconciliator."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Optional

from reconciliator.config import StorageConfig


def configure_wal_pragmas(conn: sqlite3.Connection) -> None:
    """Apply performance and concurrency pragmas to SQLite connection."""
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA busy_timeout = 30000;")
    conn.execute("PRAGMA synchronous = NORMAL;")
    conn.execute("PRAGMA foreign_keys = ON;")


def get_db_connection(db_path: Path | str) -> sqlite3.Connection:
    """Create a configured SQLite connection with row factory and WAL pragmas."""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30.0)
    conn.row_factory = sqlite3.Row
    configure_wal_pragmas(conn)
    return conn


def init_authority_db(conn: sqlite3.Connection) -> None:
    """Initialize tables, indexes, and FTS5 in authority.db."""
    with conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS authority_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL,
                external_id TEXT NOT NULL UNIQUE,
                title TEXT NOT NULL,
                title_normalized TEXT,
                uniform_title TEXT,
                edition INTEGER,
                series TEXT,
                volume_info TEXT,
                year INTEGER,
                publisher TEXT,
                language TEXT,
                subjects_json TEXT,
                record_json TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_records_source ON authority_records(source);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_records_year ON authority_records(year);")

        conn.execute("""
            CREATE TABLE IF NOT EXISTS authority_identifiers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                record_id INTEGER NOT NULL REFERENCES authority_records(id) ON DELETE CASCADE,
                id_type TEXT NOT NULL,
                id_value TEXT NOT NULL
            );
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_identifiers_type_val ON authority_identifiers(id_type, id_value);"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_identifiers_record_id ON authority_identifiers(record_id);"
        )

        conn.execute("""
            CREATE TABLE IF NOT EXISTS authority_agents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                record_id INTEGER NOT NULL REFERENCES authority_records(id) ON DELETE CASCADE,
                name TEXT NOT NULL,
                name_normalized TEXT NOT NULL,
                surname TEXT,
                role TEXT,
                uri_subfield_0 TEXT,
                uri_subfield_1 TEXT
            );
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_agents_record_id ON authority_agents(record_id);"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_agents_name_norm ON authority_agents(name_normalized);"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_agents_surname ON authority_agents(surname);")

        conn.execute("""
            CREATE TABLE IF NOT EXISTS wikidata_authors (
                qid TEXT PRIMARY KEY,
                name TEXT,
                family_name TEXT,
                asteri_id TEXT,
                isni TEXT,
                viaf TEXT
            );
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS vector_mappings (
                faiss_id INTEGER PRIMARY KEY,
                record_id INTEGER NOT NULL REFERENCES authority_records(id) ON DELETE CASCADE
            );
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_vector_mappings_record ON vector_mappings(record_id);"
        )

        conn.execute("""
            CREATE TABLE IF NOT EXISTS db_metadata (
                key TEXT PRIMARY KEY,
                value TEXT
            );
        """)

        # FTS5 Full-Text Search Virtual Table
        conn.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS authority_fts USING fts5(
                record_id UNINDEXED,
                title,
                uniform_title,
                author_names,
                publisher,
                subjects
            );
        """)


def set_metadata(conn: sqlite3.Connection, key: str, value: str) -> None:
    """Upsert a key-value pair in db_metadata."""
    with conn:
        conn.execute(
            "INSERT INTO db_metadata (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value;",
            (key, value),
        )


def get_metadata(conn: sqlite3.Connection, key: str) -> Optional[str]:
    """Get metadata value by key."""
    cursor = conn.execute("SELECT value FROM db_metadata WHERE key = ?;", (key,))
    row = cursor.fetchone()
    return row["value"] if row else None


def reset_database_file(db_path: Path | str) -> None:
    """Safely unlink a SQLite database file and its WAL / SHM companions."""
    path = Path(db_path)
    wal_path = Path(f"{db_path}-wal")
    shm_path = Path(f"{db_path}-shm")

    for p in (path, wal_path, shm_path):
        if p.exists():
            try:
                p.unlink()
            except OSError:
                pass


def reset_all_storage(
    storage_config: StorageConfig,
    reset_authority: bool = True,
    reset_vocab: bool = True,
    reset_progress: bool = True,
) -> None:
    """Reset specified database files and FAISS index, then reinitialize clean schemas."""
    # Import locally to prevent circular dependencies
    from reconciliator.progress import init_progress_db
    from reconciliator.vocab_store import init_vocab_db

    if reset_authority:
        reset_database_file(storage_config.authority_db)
        if storage_config.index_path.exists():
            try:
                storage_config.index_path.unlink()
            except OSError:
                pass
        with get_db_connection(storage_config.authority_db) as conn:
            init_authority_db(conn)

    if reset_vocab:
        reset_database_file(storage_config.vocab_db)
        with get_db_connection(storage_config.vocab_db) as conn:
            init_vocab_db(conn)

    if reset_progress:
        reset_database_file(storage_config.progress_db)
        with get_db_connection(storage_config.progress_db) as conn:
            init_progress_db(conn)
