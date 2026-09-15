"""SQLite database management, schema initialization, and WAL pragmas for Reconciliator."""

from __future__ import annotations

import sqlite3
import time
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
        # db_metadata table
        conn.execute("""
            CREATE TABLE IF NOT EXISTS db_metadata (
                key TEXT PRIMARY KEY,
                value TEXT
            );
        """)

        # Registry of ingest sources. record_origin on every WEMI row names one of
        # these, so "is this authoritative?" is a property of the source rather
        # than a literal baked into each row. A corpus being reconciled is just a
        # non-authoritative source.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sources (
                name TEXT PRIMARY KEY,
                is_authoritative BOOLEAN NOT NULL DEFAULT 1,
                first_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_sources_authoritative ON sources(is_authoritative);"
        )

        # ===================================================================
        # WEMI Relational Tables (§4.1)
        # ===================================================================

        # 1. Agents Table (Creators, Contributors, Realizers)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS agents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_uri TEXT NOT NULL UNIQUE,
                source TEXT NOT NULL,
                record_origin TEXT NOT NULL DEFAULT 'unknown',
                wikidata_qid TEXT,
                name TEXT NOT NULL,
                name_normalized TEXT NOT NULL,
                surname TEXT,
                qualifier TEXT,
                dates TEXT,
                birth_year INTEGER,
                death_year INTEGER,
                fuller_name TEXT,
                affiliation TEXT,
                metadata_json TEXT,
                is_personal BOOLEAN NOT NULL DEFAULT 1,
                is_fictional BOOLEAN NOT NULL DEFAULT 0
            );
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_agents_uri ON agents(agent_uri);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_agents_qid ON agents(wikidata_qid);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_agents_name_norm ON agents(name_normalized);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_agents_surname ON agents(surname);")

        # 2. Works Table (Abstract Intellectual Creations)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS works (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                work_uri TEXT NOT NULL UNIQUE,
                source TEXT NOT NULL,
                source_id TEXT NOT NULL,
                record_origin TEXT NOT NULL DEFAULT 'unknown',
                wikidata_qid TEXT,
                title TEXT NOT NULL,
                title_normalized TEXT NOT NULL,
                original_language TEXT,
                primary_agent_id INTEGER REFERENCES agents(id) ON DELETE SET NULL,
                ykl_class TEXT,
                work_type TEXT NOT NULL DEFAULT 'text',
                subjects_json TEXT NOT NULL DEFAULT '[]',
                serialized_text TEXT,
                -- Elected representative of this work's cross-source equivalence
                -- group, set by WorkConsolidator. NULL means the work is its own
                -- canonical. The same_work_as edges in work_relationships record
                -- why the group was formed; this column records the decision.
                canonical_work_id INTEGER REFERENCES works(id) ON DELETE SET NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_works_uri ON works(work_uri);")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_works_canonical ON works(canonical_work_id);"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_works_qid ON works(wikidata_qid);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_works_source_id ON works(source, source_id);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_works_title_norm ON works(title_normalized);")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_works_primary_agent ON works(primary_agent_id);"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_works_type ON works(work_type);")

        # 3. Expressions Table (Realizations: Languages, Translations, Arrangements, Audio, Versions)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS expressions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                work_id INTEGER NOT NULL REFERENCES works(id) ON DELETE CASCADE,
                expression_uri TEXT NOT NULL UNIQUE,
                source TEXT NOT NULL,
                source_id TEXT NOT NULL,
                record_origin TEXT NOT NULL DEFAULT 'unknown',
                wikidata_qid TEXT,
                language TEXT NOT NULL,
                content_type TEXT NOT NULL DEFAULT 'text',
                title TEXT,
                version_designation TEXT,
                arrangement_note TEXT,
                medium_of_performance TEXT,
                serialized_text TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_expressions_work_id ON expressions(work_id);")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_expressions_uri ON expressions(expression_uri);"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_expressions_lookup ON expressions(work_id, language, content_type);"
        )

        # 4. Manifestations Table (Physical/Digital Publications & Container/Component Hierarchy)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS manifestations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                work_id INTEGER REFERENCES works(id) ON DELETE SET NULL,
                container_id INTEGER REFERENCES manifestations(id) ON DELETE CASCADE,
                is_component BOOLEAN NOT NULL DEFAULT 0,
                component_type TEXT,
                manifestation_uri TEXT UNIQUE,
                source TEXT NOT NULL,
                source_id TEXT NOT NULL UNIQUE,
                record_origin TEXT NOT NULL DEFAULT 'unknown',
                title TEXT NOT NULL,
                title_normalized TEXT NOT NULL,
                subtitle TEXT,
                publication_year INTEGER,
                publisher TEXT,
                publication_place TEXT,
                edition_statement TEXT,
                edition_number INTEGER,
                series_title TEXT,
                series_numbering TEXT,
                volume_designation TEXT,
                issue_enumeration TEXT,
                pagination TEXT,
                extent TEXT,
                carrier_type TEXT NOT NULL DEFAULT 'volume',
                host_title TEXT,
                serialized_text TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_manifestations_work_id ON manifestations(work_id);"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_manifestations_container ON manifestations(container_id);"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_manifestations_component ON manifestations(is_component, component_type);"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_manifestations_year ON manifestations(publication_year);"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_manifestations_series ON manifestations(series_title);"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_manifestations_source_id ON manifestations(source, source_id);"
        )

        # 5. Manifestation-to-Expression Association (Supports Multi-Track & Multi-Expression Publications)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS manifestation_expressions (
                manifestation_id INTEGER NOT NULL REFERENCES manifestations(id) ON DELETE CASCADE,
                expression_id INTEGER NOT NULL REFERENCES expressions(id) ON DELETE CASCADE,
                track_number INTEGER,
                disc_number INTEGER DEFAULT 1,
                duration TEXT,
                is_primary BOOLEAN NOT NULL DEFAULT 1,
                PRIMARY KEY (manifestation_id, expression_id)
            );
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_manif_exp_track ON manifestation_expressions(manifestation_id, track_number);"
        )

        # 6. Items Table (Physical Exemplars, Copy Provenance & Sammelbände)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                manifestation_id INTEGER NOT NULL REFERENCES manifestations(id) ON DELETE CASCADE,
                item_uri TEXT UNIQUE,
                source TEXT NOT NULL,
                source_id TEXT NOT NULL,
                record_origin TEXT NOT NULL DEFAULT 'unknown',
                holding_organization TEXT NOT NULL,
                shelf_mark TEXT,
                barcode TEXT,
                custodial_history TEXT,
                item_condition TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_items_manif_id ON items(manifestation_id);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_items_barcode ON items(barcode);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_items_org ON items(holding_organization);")

        # 7. Universal Polymorphic Identifiers
        conn.execute("""
            CREATE TABLE IF NOT EXISTS identifiers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                entity_type TEXT NOT NULL CHECK(entity_type IN ('work', 'expression', 'manifestation', 'item', 'agent')),
                entity_id INTEGER NOT NULL,
                scheme TEXT NOT NULL,
                value TEXT NOT NULL,
                value_normalized TEXT NOT NULL
            );
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_identifiers_lookup ON identifiers(scheme, value_normalized);"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_identifiers_entity ON identifiers(entity_type, entity_id);"
        )

        # Derived domain blocking keys, beside `identifiers`. That table holds
        # externally-meaningful standard numbers; this one holds the keys the
        # clusterer blocks on and the funnels dispatch on, computed at ingest.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS entity_keys (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                entity_type TEXT NOT NULL CHECK(entity_type IN ('work', 'expression', 'manifestation', 'item', 'agent')),
                entity_id INTEGER NOT NULL,
                key_type TEXT NOT NULL,
                key_value TEXT NOT NULL,
                UNIQUE(entity_type, entity_id, key_type, key_value)
            );
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_entity_keys_entity ON entity_keys(entity_type, entity_id);"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_entity_keys_lookup ON entity_keys(key_type, key_value);"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_entity_keys_value ON entity_keys(key_value);"
        )

        # 8. Agent Contributions with Relator Roles
        conn.execute("""
            CREATE TABLE IF NOT EXISTS contributions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                entity_type TEXT NOT NULL CHECK(entity_type IN ('work', 'expression', 'manifestation', 'item')),
                entity_id INTEGER NOT NULL,
                agent_id INTEGER NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
                role TEXT NOT NULL,
                is_primary BOOLEAN NOT NULL DEFAULT 0,
                field_tag TEXT
            );
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_contributions_entity ON contributions(entity_type, entity_id);"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_contributions_agent ON contributions(agent_id, role);"
        )

        # 9. Inter-Work Relationships & Lineages
        conn.execute("""
            CREATE TABLE IF NOT EXISTS work_relationships (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_work_id INTEGER NOT NULL REFERENCES works(id) ON DELETE CASCADE,
                target_work_id INTEGER NOT NULL REFERENCES works(id) ON DELETE CASCADE,
                relationship_type TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_work_rel_source ON work_relationships(source_work_id, relationship_type);"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_work_rel_target ON work_relationships(target_work_id, relationship_type);"
        )

        # 10. Medium of Performance (Music Works)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS work_medium_of_performance (
                work_id INTEGER NOT NULL REFERENCES works(id) ON DELETE CASCADE,
                medium_uri TEXT NOT NULL,
                label TEXT NOT NULL,
                player_count INTEGER,
                is_soloist BOOLEAN NOT NULL DEFAULT 0,
                PRIMARY KEY (work_id, medium_uri)
            );
        """)

        # 11. Tripartite Vector Search Index Mappings (Work, Expression & Manifestation Tiers)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS work_vectors (
                vector_id INTEGER PRIMARY KEY,
                work_id INTEGER NOT NULL REFERENCES works(id) ON DELETE CASCADE
            );
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_work_vectors_work_id ON work_vectors(work_id);"
        )

        conn.execute("""
            CREATE TABLE IF NOT EXISTS expression_vectors (
                vector_id INTEGER PRIMARY KEY,
                expression_id INTEGER NOT NULL REFERENCES expressions(id) ON DELETE CASCADE
            );
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_expression_vectors_exp_id ON expression_vectors(expression_id);"
        )

        conn.execute("""
            CREATE TABLE IF NOT EXISTS manifestation_vectors (
                vector_id INTEGER PRIMARY KEY,
                manifestation_id INTEGER NOT NULL REFERENCES manifestations(id) ON DELETE CASCADE
            );
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_manifestation_vectors_manif_id ON manifestation_vectors(manifestation_id);"
        )

        # 12. Tripartite FTS5 Lexical BM25 Virtual Tables
        conn.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS works_fts USING fts5(
                serialized_text,
                content='works',
                content_rowid='id',
                tokenize='unicode61 remove_diacritics 2'
            );
        """)
        conn.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS expressions_fts USING fts5(
                serialized_text,
                content='expressions',
                content_rowid='id',
                tokenize='unicode61 remove_diacritics 2'
            );
        """)
        conn.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS manifestations_fts USING fts5(
                serialized_text,
                content='manifestations',
                content_rowid='id',
                tokenize='unicode61 remove_diacritics 2'
            );
        """)

        # Triggers to keep FTS5 external content in sync with base tables
        conn.execute("""
            CREATE TRIGGER IF NOT EXISTS works_ai AFTER INSERT ON works BEGIN
                INSERT INTO works_fts(rowid, serialized_text) VALUES (new.id, new.serialized_text);
            END;
        """)
        conn.execute("""
            CREATE TRIGGER IF NOT EXISTS works_ad AFTER DELETE ON works BEGIN
                INSERT INTO works_fts(works_fts, rowid, serialized_text) VALUES('delete', old.id, old.serialized_text);
            END;
        """)
        conn.execute("""
            CREATE TRIGGER IF NOT EXISTS works_au AFTER UPDATE ON works BEGIN
                INSERT INTO works_fts(works_fts, rowid, serialized_text) VALUES('delete', old.id, old.serialized_text);
                INSERT INTO works_fts(rowid, serialized_text) VALUES (new.id, new.serialized_text);
            END;
        """)

        conn.execute("""
            CREATE TRIGGER IF NOT EXISTS expressions_ai AFTER INSERT ON expressions BEGIN
                INSERT INTO expressions_fts(rowid, serialized_text) VALUES (new.id, new.serialized_text);
            END;
        """)
        conn.execute("""
            CREATE TRIGGER IF NOT EXISTS expressions_ad AFTER DELETE ON expressions BEGIN
                INSERT INTO expressions_fts(expressions_fts, rowid, serialized_text) VALUES('delete', old.id, old.serialized_text);
            END;
        """)
        conn.execute("""
            CREATE TRIGGER IF NOT EXISTS expressions_au AFTER UPDATE ON expressions BEGIN
                INSERT INTO expressions_fts(expressions_fts, rowid, serialized_text) VALUES('delete', old.id, old.serialized_text);
                INSERT INTO expressions_fts(rowid, serialized_text) VALUES (new.id, new.serialized_text);
            END;
        """)

        conn.execute("""
            CREATE TRIGGER IF NOT EXISTS manifestations_ai AFTER INSERT ON manifestations BEGIN
                INSERT INTO manifestations_fts(rowid, serialized_text) VALUES (new.id, new.serialized_text);
            END;
        """)
        conn.execute("""
            CREATE TRIGGER IF NOT EXISTS manifestations_ad AFTER DELETE ON manifestations BEGIN
                INSERT INTO manifestations_fts(manifestations_fts, rowid, serialized_text) VALUES('delete', old.id, old.serialized_text);
            END;
        """)
        conn.execute("""
            CREATE TRIGGER IF NOT EXISTS manifestations_au AFTER UPDATE ON manifestations BEGIN
                INSERT INTO manifestations_fts(manifestations_fts, rowid, serialized_text) VALUES('delete', old.id, old.serialized_text);
                INSERT INTO manifestations_fts(rowid, serialized_text) VALUES (new.id, new.serialized_text);
            END;
        """)

        # Corpus staging, clustering and linkage live in this same database.
        # They were a separate file, which prevented joins between a cluster and
        # the works it resolves to, and let reset clear one while the repository
        # used the other.
        init_corpus_db(conn)

        # 13. Persistent Harvest Checkpointing for OAI-PMH and ResourceSync
        conn.execute("""
            CREATE TABLE IF NOT EXISTS harvest_state (
                source_id TEXT PRIMARY KEY,
                source_type TEXT NOT NULL,
                metadata_prefix TEXT NOT NULL,
                set_spec TEXT,
                last_harvest_from TEXT,
                last_harvest_until TEXT,
                resumption_token TEXT,
                records_harvested INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'in_progress',
                completed_slices TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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


def init_corpus_db(conn: sqlite3.Connection) -> None:
    """Initialize target staging and clustering tables in reconcile_corpus.db."""
    with conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS target_records (
                record_id TEXT PRIMARY KEY,
                format TEXT NOT NULL,
                work_type TEXT NOT NULL DEFAULT 'text',
                title TEXT NOT NULL,
                uniform_title TEXT,
                language TEXT,
                author_key TEXT,
                title_stem TEXT NOT NULL,
                year INTEGER,
                volume_designation TEXT,
                is_component BOOLEAN NOT NULL DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_target_records_work_type ON target_records(work_type);"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_target_records_author ON target_records(author_key);"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_target_records_created ON target_records(created_at);"
        )

        conn.execute("""
            CREATE TABLE IF NOT EXISTS target_keys (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                record_id TEXT NOT NULL REFERENCES target_records(record_id) ON DELETE CASCADE,
                key_type TEXT NOT NULL,
                key_value TEXT NOT NULL
            );
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_target_keys_record ON target_keys(record_id);"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_target_keys_lookup ON target_keys(key_type, key_value);"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_target_keys_val ON target_keys(key_value);"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_target_keys_record_type ON target_keys(record_id, key_type);"
        )

        conn.execute("""
            CREATE TABLE IF NOT EXISTS target_payloads (
                record_id TEXT PRIMARY KEY REFERENCES target_records(record_id) ON DELETE CASCADE,
                raw_payload BLOB NOT NULL,
                serialized_work TEXT,
                serialized_expression TEXT,
                serialized_manifestation TEXT
            );
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS corpus_manifestation_clusters (
                cluster_id TEXT PRIMARY KEY,
                canonical_record_id TEXT NOT NULL REFERENCES target_records(record_id),
                publisher TEXT,
                publication_year INTEGER,
                edition_statement TEXT,
                extent TEXT,
                carrier_type TEXT,
                member_record_count INTEGER NOT NULL DEFAULT 1
            );
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS corpus_work_clusters (
                cluster_id TEXT PRIMARY KEY,
                work_type TEXT NOT NULL DEFAULT 'text',
                canonical_work_uri TEXT,
                wikidata_qid TEXT,
                canonical_title TEXT NOT NULL,
                canonical_author TEXT
            );
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS corpus_expression_clusters (
                cluster_id TEXT PRIMARY KEY,
                work_cluster_id TEXT NOT NULL REFERENCES corpus_work_clusters(cluster_id),
                canonical_expression_uri TEXT,
                content_type TEXT NOT NULL DEFAULT 'text',
                language TEXT NOT NULL,
                version_designation TEXT,
                translators_json TEXT NOT NULL DEFAULT '[]',
                performers_json TEXT NOT NULL DEFAULT '[]'
            );
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_exp_cluster_work ON corpus_expression_clusters(work_cluster_id);"
        )

        conn.execute("""
            CREATE TABLE IF NOT EXISTS corpus_linkages (
                record_id TEXT PRIMARY KEY REFERENCES target_records(record_id),
                manifestation_cluster_id TEXT REFERENCES corpus_manifestation_clusters(cluster_id),
                work_cluster_id TEXT NOT NULL REFERENCES corpus_work_clusters(cluster_id),
                expression_cluster_id TEXT NOT NULL REFERENCES corpus_expression_clusters(cluster_id),
                final_work_uri TEXT NOT NULL DEFAULT '',
                final_expression_uri TEXT NOT NULL DEFAULT '',
                sibling_manifestation_ids_json TEXT
            );
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_linkage_manifestation ON corpus_linkages(manifestation_cluster_id);"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_linkage_work ON corpus_linkages(work_cluster_id);"
        )

        conn.execute("""
            CREATE TABLE IF NOT EXISTS sammelband_bindings (
                binding_id TEXT NOT NULL,
                record_id TEXT NOT NULL REFERENCES target_records(record_id),
                sequence_in_binding INTEGER NOT NULL DEFAULT 1,
                binding_notes TEXT,
                PRIMARY KEY (binding_id, record_id)
            );
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_sammelband_binding ON sammelband_bindings(binding_id);"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_sammelband_record ON sammelband_bindings(record_id);"
        )


def reset_all_storage(storage_config: StorageConfig) -> None:
    """Delete every database and vector index, then reinitialize clean schemas.

    All or nothing: partial resets left the stores inconsistent with each other,
    most often a vector index or a staged corpus outliving the rows it referred to.
    """
    # Import locally to prevent circular dependencies
    from reconciliator.indexer.ipc.client import DaemonLifecycleManager, get_default_socket_path
    from reconciliator.progress import init_progress_db
    from reconciliator.vocab_store import init_vocab_db

    # Stop the vector daemon first; it holds indices open.
    try:
        sock_p = storage_config.vector_engine.daemon.socket_path or get_default_socket_path(
            storage_config.authority_db.parent
        )
        lifecycle = DaemonLifecycleManager(sock_p)
        if lifecycle.is_running():
            lifecycle.shutdown(save=False)
            time.sleep(0.1)
    except Exception:
        pass

    for index_path in (
        storage_config.work_index,
        storage_config.expression_index,
        storage_config.manifestation_index,
        storage_config.agent_index,
    ):
        if index_path and index_path.exists():
            try:
                index_path.unlink()
            except OSError:
                pass

    for db_path, initializer in (
        (storage_config.authority_db, init_authority_db),
        (storage_config.vocab_db, init_vocab_db),
        (storage_config.progress_db, init_progress_db),
    ):
        reset_database_file(db_path)
        with get_db_connection(db_path) as conn:
            initializer(conn)
