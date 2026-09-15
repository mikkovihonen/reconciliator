"""Unit tests for WEMI database schema DDL, triggers, and foreign keys."""

import sqlite3
from pathlib import Path

from reconciliator.db import get_db_connection, init_authority_db, init_corpus_db, reset_all_storage


def test_init_authority_db_tables_and_indexes():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_authority_db(conn)

    cursor = conn.execute("SELECT name, type FROM sqlite_master WHERE type IN ('table', 'view');")
    names = {row["name"] for row in cursor}

    # Verify WEMI tables exist
    expected_tables = {
        "agents",
        "works",
        "expressions",
        "manifestations",
        "manifestation_expressions",
        "items",
        "identifiers",
        "contributions",
        "work_relationships",
        "work_medium_of_performance",
        "work_vectors",
        "manifestation_vectors",
        "works_fts",
        "expressions_fts",
        "manifestations_fts",
        "harvest_state",
    }
    for table in expected_tables:
        assert table in names, f"Expected table {table} not found in authority.db"


def test_init_corpus_db_tables():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_corpus_db(conn)

    cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table';")
    names = {row["name"] for row in cursor}

    expected_corpus_tables = {
        "corpus_manifestation_clusters",
        "corpus_work_clusters",
        "corpus_expression_clusters",
        "corpus_linkages",
        "sammelband_bindings",
    }
    for table in expected_corpus_tables:
        assert table in names, f"Expected table {table} not found in reconcile_corpus.db"


def test_foreign_key_cascades():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    init_authority_db(conn)

    # Insert work
    conn.execute(
        """
        INSERT INTO works (id, work_uri, source, source_id, title, title_normalized)
        VALUES (1, 'work_1', 'melinda', 'w1', 'Work Title', 'work title');
        """
    )

    # Insert expression linked to work
    conn.execute(
        """
        INSERT INTO expressions (id, work_id, expression_uri, source, source_id, language, content_type)
        VALUES (10, 1, 'exp_1', 'melinda', 'e1', 'fin', 'text');
        """
    )

    # Insert manifestation linked to work
    conn.execute(
        """
        INSERT INTO manifestations (id, work_id, source, source_id, title, title_normalized)
        VALUES (100, 1, 'melinda', 'm1', 'Manifestation Title', 'manifestation title');
        """
    )

    # Insert manifestation_expressions junction
    conn.execute(
        """
        INSERT INTO manifestation_expressions (manifestation_id, expression_id)
        VALUES (100, 10);
        """
    )

    # Insert item linked to manifestation
    conn.execute(
        """
        INSERT INTO items (id, manifestation_id, source, source_id, holding_organization)
        VALUES (1000, 100, 'koha', 'i1', 'National Library');
        """
    )

    # Insert work vector mapping
    conn.execute("INSERT INTO work_vectors (vector_id, work_id) VALUES (500, 1);")

    # Verify rows exist
    assert conn.execute("SELECT COUNT(*) FROM expressions WHERE work_id = 1;").fetchone()[0] == 1
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM manifestation_expressions WHERE manifestation_id = 100;"
        ).fetchone()[0]
        == 1
    )
    assert (
        conn.execute("SELECT COUNT(*) FROM items WHERE manifestation_id = 100;").fetchone()[0] == 1
    )
    assert conn.execute("SELECT COUNT(*) FROM work_vectors WHERE work_id = 1;").fetchone()[0] == 1

    # Delete work -> should cascade to expression and work_vectors
    conn.execute("DELETE FROM works WHERE id = 1;")
    assert conn.execute("SELECT COUNT(*) FROM expressions WHERE work_id = 1;").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM work_vectors WHERE work_id = 1;").fetchone()[0] == 0
    # manifestation.work_id is ON DELETE SET NULL, so manifestation remains but work_id is NULL
    manif_row = conn.execute("SELECT work_id FROM manifestations WHERE id = 100;").fetchone()
    assert manif_row["work_id"] is None

    # Delete manifestation -> cascades to items and manifestation_expressions
    conn.execute("DELETE FROM manifestations WHERE id = 100;")
    assert (
        conn.execute("SELECT COUNT(*) FROM items WHERE manifestation_id = 100;").fetchone()[0] == 0
    )
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM manifestation_expressions WHERE manifestation_id = 100;"
        ).fetchone()[0]
        == 0
    )


def test_fts5_triggers_lifecycle():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_authority_db(conn)

    # 1. Insert Work
    conn.execute(
        """
        INSERT INTO works (id, work_uri, source, source_id, title, title_normalized, serialized_text)
        VALUES (1, 'w1', 'melinda', 's1', 'Sota ja rauha', 'sota ja rauha', 'Leo Tolstoi Sota ja rauha klassikko');
        """
    )
    res = conn.execute(
        "SELECT rowid, serialized_text FROM works_fts WHERE works_fts MATCH 'Tolstoi';"
    ).fetchall()
    assert len(res) == 1
    assert res[0]["rowid"] == 1

    # 2. Update Work
    conn.execute(
        """
        UPDATE works SET serialized_text = 'Leo Tolstoi Sota ja rauha päivitetty versio' WHERE id = 1;
        """
    )
    res = conn.execute(
        "SELECT rowid, serialized_text FROM works_fts WHERE works_fts MATCH 'päivitetty';"
    ).fetchall()
    assert len(res) == 1
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM works_fts WHERE works_fts MATCH 'klassikko';"
        ).fetchone()[0]
        == 0
    )

    # 3. Delete Work
    conn.execute("DELETE FROM works WHERE id = 1;")
    assert (
        conn.execute("SELECT COUNT(*) FROM works_fts WHERE works_fts MATCH 'Tolstoi';").fetchone()[
            0
        ]
        == 0
    )


def test_reset_all_storage_wemi(tmp_path: Path):
    from reconciliator.config import StorageConfig

    auth_db = tmp_path / "authority.db"
    w_idx = tmp_path / "work.index"
    m_idx = tmp_path / "manifestation.index"
    a_idx = tmp_path / "agent.index"

    storage_cfg = StorageConfig(
        data_dir=tmp_path,
        authority_db=auth_db,
        work_index=w_idx,
        manifestation_index=m_idx,
        agent_index=a_idx,
    )

    # Create dummy files
    for p in [auth_db, w_idx, m_idx, a_idx]:
        p.write_text("test")
        assert p.exists()

    reset_all_storage(storage_config=storage_cfg)

    # Database files exist and are re-initialized with fresh empty schemas
    assert auth_db.exists()

    with get_db_connection(auth_db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM works;").fetchone()[0] == 0

    with get_db_connection(auth_db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM entity_keys;").fetchone()[0] == 0

    # Vector indices are deleted
    for p in [w_idx, m_idx, a_idx]:
        assert not p.exists()


def test_entity_keys_are_not_cascade_deleted_with_their_owner():
    """entity_keys is polymorphic, so it carries no foreign key and no cascade.

    entity_type/entity_id can name a work, expression, manifestation, item or
    agent, which SQLite cannot express as one foreign key. Deleting an owning row
    therefore leaves its keys behind, and anything that removes rows - the
    `replace` ingest mode in particular - has to delete them explicitly.
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    init_authority_db(conn)

    conn.execute(
        """
        INSERT INTO manifestations (manifestation_uri, source, source_id, title, title_normalized)
        VALUES ('urn:manif:s:r1', 's', 'r1', 'T', 't');
        """
    )
    mid = conn.execute("SELECT id FROM manifestations WHERE source_id = 'r1';").fetchone()["id"]
    for key_type, value in (("patent", "FI129456B"), ("patent", "EP999888"), ("custom", "xyz")):
        conn.execute(
            "INSERT INTO entity_keys (entity_type, entity_id, key_type, key_value) VALUES (?,?,?,?);",
            ("manifestation", mid, key_type, value),
        )
    assert conn.execute(
        "SELECT COUNT(*) FROM entity_keys WHERE entity_id = ?;", (mid,)
    ).fetchone()[0] == 3

    conn.execute("DELETE FROM manifestations WHERE id = ?;", (mid,))
    assert conn.execute(
        "SELECT COUNT(*) FROM entity_keys WHERE entity_id = ?;", (mid,)
    ).fetchone()[0] == 3, "no cascade: cleanup must be explicit"


def test_entity_keys_reject_duplicate_rows():
    """The UNIQUE constraint is what makes re-ingesting a record idempotent."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_authority_db(conn)
    for _ in range(2):
        conn.execute(
            """
            INSERT OR IGNORE INTO entity_keys (entity_type, entity_id, key_type, key_value)
            VALUES ('manifestation', 1, 'work', 'abc');
            """
        )
    assert conn.execute("SELECT COUNT(*) FROM entity_keys;").fetchone()[0] == 1


def test_extensible_types_in_authority_db():
    """Verify that authority.db supports newly added work_type and content_type without CHECK violations."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_authority_db(conn)

    # Novel work_type and content_type should succeed without DDL errors
    conn.execute(
        """
        INSERT INTO works (id, work_uri, source, source_id, title, title_normalized, work_type)
        VALUES (1, 'urn:work:future_01', 'custom_src', 'f1', 'Future Simulation', 'future simulation', 'quantum_sim');
        """
    )
    conn.execute(
        """
        INSERT INTO expressions (id, work_id, expression_uri, source, source_id, language, content_type)
        VALUES (1, 1, 'urn:exp:future_01', 'custom_src', 'ef1', 'und', 'neural_field_weights');
        """
    )
    assert conn.execute("SELECT work_type FROM works WHERE id = 1;").fetchone()[0] == "quantum_sim"
    assert conn.execute("SELECT content_type FROM expressions WHERE id = 1;").fetchone()[0] == "neural_field_weights"

