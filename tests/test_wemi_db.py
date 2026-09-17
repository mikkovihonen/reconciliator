"""Unit tests for WEMI database schema DDL, full-text indexes, and foreign keys in PostgreSQL."""

from reconciliator.config import StorageConfig
from reconciliator.db import (
    get_db_connection,
    reset_all_storage,
)


def test_init_authority_db_tables_and_indexes(tmp_storage: StorageConfig):
    with get_db_connection(tmp_storage.database_url) as conn:
        cursor = conn.exec_driver_sql(
            """
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = CURRENT_SCHEMA;
            """
        ).mappings()
        names = {row["table_name"] for row in cursor}

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
        "harvest_state",
    }
    for table in expected_tables:
        assert table in names, f"Expected table {table} not found in PostgreSQL schema"


def test_init_corpus_db_tables(tmp_storage: StorageConfig):
    with get_db_connection(tmp_storage.database_url) as conn:
        cursor = conn.exec_driver_sql(
            """
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = CURRENT_SCHEMA;
            """
        ).mappings()
        names = {row["table_name"] for row in cursor}

    expected_corpus_tables = {
        "corpus_manifestation_clusters",
        "corpus_work_clusters",
        "corpus_expression_clusters",
        "corpus_linkages",
        "sammelband_bindings",
    }
    for table in expected_corpus_tables:
        assert table in names, f"Expected table {table} not found in PostgreSQL schema"


def test_foreign_key_cascades(tmp_storage: StorageConfig):
    with get_db_connection(tmp_storage.database_url) as conn:
        # Insert work
        conn.exec_driver_sql(
            """
            INSERT INTO works (id, source, source_id, title, title_normalized)
            VALUES (1, 'melinda', 'w1', 'Work Title', 'work title')
            ON CONFLICT (source, source_id) DO NOTHING;
            """
        )

        # Insert expression linked to work
        conn.exec_driver_sql(
            """
            INSERT INTO expressions (id, work_id, source, source_id, language, content_type)
            VALUES (10, 1, 'melinda', 'e1', 'fin', 'text')
            ON CONFLICT (source, source_id) DO NOTHING;
            """
        )

        # Insert manifestation linked to work
        conn.exec_driver_sql(
            """
            INSERT INTO manifestations (id, work_id, source, source_id, title, title_normalized)
            VALUES (100, 1, 'melinda', 'm1', 'Manifestation Title', 'manifestation title')
            ON CONFLICT (source, source_id) DO NOTHING;
            """
        )

        # Insert manifestation_expressions junction
        conn.exec_driver_sql(
            """
            INSERT INTO manifestation_expressions (manifestation_id, expression_id)
            VALUES (100, 10)
            ON CONFLICT (manifestation_id, expression_id) DO NOTHING;
            """
        )

        # Insert item linked to manifestation
        conn.exec_driver_sql(
            """
            INSERT INTO items (id, manifestation_id, source, source_id, holding_organization)
            VALUES (1000, 100, 'koha', 'i1', 'National Library')
            ON CONFLICT (source, source_id) DO NOTHING;
            """
        )

        # Insert work vector mapping
        conn.exec_driver_sql(
            """
            INSERT INTO work_vectors (vector_id, work_id) VALUES (500, 1)
            ON CONFLICT (vector_id) DO NOTHING;
            """
        )

        # Verify rows exist
        assert (
            conn.exec_driver_sql("SELECT COUNT(*) FROM expressions WHERE work_id = 1;").scalar()
            == 1
        )
        assert (
            conn.exec_driver_sql(
                "SELECT COUNT(*) FROM manifestation_expressions WHERE manifestation_id = 100;"
            ).scalar()
            == 1
        )
        assert (
            conn.exec_driver_sql(
                "SELECT COUNT(*) FROM items WHERE manifestation_id = 100;"
            ).scalar()
            == 1
        )
        assert (
            conn.exec_driver_sql("SELECT COUNT(*) FROM work_vectors WHERE work_id = 1;").scalar()
            == 1
        )

        # Delete work -> should cascade to expression and work_vectors
        conn.exec_driver_sql("DELETE FROM works WHERE id = 1;")
        assert (
            conn.exec_driver_sql("SELECT COUNT(*) FROM expressions WHERE work_id = 1;").scalar()
            == 0
        )
        assert (
            conn.exec_driver_sql("SELECT COUNT(*) FROM work_vectors WHERE work_id = 1;").scalar()
            == 0
        )
        # manifestation.work_id is ON DELETE SET NULL, so manifestation remains but work_id is NULL
        manif_row = (
            conn.exec_driver_sql("SELECT work_id FROM manifestations WHERE id = 100;")
            .mappings()
            .fetchone()
        )
        assert manif_row["work_id"] is None

        # Delete manifestation -> cascades to items and manifestation_expressions
        conn.exec_driver_sql("DELETE FROM manifestations WHERE id = 100;")
        assert (
            conn.exec_driver_sql(
                "SELECT COUNT(*) FROM items WHERE manifestation_id = 100;"
            ).scalar()
            == 0
        )
        assert (
            conn.exec_driver_sql(
                "SELECT COUNT(*) FROM manifestation_expressions WHERE manifestation_id = 100;"
            ).scalar()
            == 0
        )


def test_postgres_fulltext_and_trigram(tmp_storage: StorageConfig):
    with get_db_connection(tmp_storage.database_url) as conn:
        conn.exec_driver_sql(
            """
            INSERT INTO works (id, source, source_id, title, title_normalized, serialized_text)
            VALUES (1, 'melinda', 's1', 'Sota ja rauha', 'sota ja rauha', 'Leo Tolstoi Sota ja rauha klassikko')
            ON CONFLICT (source, source_id) DO UPDATE SET serialized_text = EXCLUDED.serialized_text;
            """
        )

        # Full-text query on serialized_text
        res = (
            conn.exec_driver_sql(
                """
            SELECT id, serialized_text
            FROM works
            WHERE to_tsvector('simple', coalesce(serialized_text, '')) @@ plainto_tsquery('simple', %s);
            """,
                ("Tolstoi",),
            )
            .mappings()
            .fetchall()
        )
        assert len(res) == 1
        assert res[0]["id"] == 1

        # Trigram query on title_normalized
        tri_res = (
            conn.exec_driver_sql(
                """
            SELECT id, title
            FROM works
            WHERE title_normalized %% %s;
            """,
                ("sota ja rauha",),
            )
            .mappings()
            .fetchall()
        )
        assert len(tri_res) == 1


def test_reset_all_storage_wemi(tmp_storage: StorageConfig):
    w_idx = tmp_storage.data_dir / "work.index"
    m_idx = tmp_storage.data_dir / "manifestation.index"
    a_idx = tmp_storage.data_dir / "agent.index"

    storage_cfg = StorageConfig(
        data_dir=tmp_storage.data_dir,
        database_url=tmp_storage.database_url,
        work_index=w_idx,
        manifestation_index=m_idx,
        agent_index=a_idx,
    )

    # Create dummy files
    for p in [w_idx, m_idx, a_idx]:
        p.write_text("test")
        assert p.exists()

    with get_db_connection(tmp_storage.database_url) as conn:
        conn.exec_driver_sql(
            """
            INSERT INTO works (source, source_id, title, title_normalized)
            VALUES ('s', 'r1', 'T', 't')
            ON CONFLICT (source, source_id) DO NOTHING;
            """
        )
        assert conn.exec_driver_sql("SELECT COUNT(*) FROM works;").scalar() >= 1

    reset_all_storage(storage_config=storage_cfg)

    with get_db_connection(tmp_storage.database_url) as conn:
        assert conn.exec_driver_sql("SELECT COUNT(*) FROM works;").scalar() == 0
        assert conn.exec_driver_sql("SELECT COUNT(*) FROM entity_keys;").scalar() == 0

    # Vector indices are deleted
    for p in [w_idx, m_idx, a_idx]:
        assert not p.exists()


def test_entity_keys_are_not_cascade_deleted_with_their_owner(tmp_storage: StorageConfig):
    """entity_keys is polymorphic, so it carries no foreign key and no cascade."""
    with get_db_connection(tmp_storage.database_url) as conn:
        conn.exec_driver_sql(
            """
            INSERT INTO manifestations (source, source_id, title, title_normalized)
            VALUES ('s', 'r1', 'T', 't')
            ON CONFLICT (source, source_id) DO NOTHING;
            """
        )
        mid = (
            conn.exec_driver_sql("SELECT id FROM manifestations WHERE source_id = 'r1';")
            .mappings()
            .fetchone()["id"]
        )
        for key_type, value in (("patent", "FI129456B"), ("patent", "EP999888"), ("custom", "xyz")):
            conn.exec_driver_sql(
                "INSERT INTO entity_keys (entity_type, entity_id, key_type, key_value) VALUES (%s,%s,%s,%s);",
                ("manifestation", mid, key_type, value),
            )
        assert (
            conn.exec_driver_sql(
                "SELECT COUNT(*) FROM entity_keys WHERE entity_id = %s;", (mid,)
            ).scalar()
            == 3
        )

        conn.exec_driver_sql("DELETE FROM manifestations WHERE id = %s;", (mid,))
        assert (
            conn.exec_driver_sql(
                "SELECT COUNT(*) FROM entity_keys WHERE entity_id = %s;", (mid,)
            ).scalar()
            == 3
        ), "no cascade: cleanup must be explicit"


def test_entity_keys_reject_duplicate_rows(tmp_storage: StorageConfig):
    """The UNIQUE constraint is what makes re-ingesting a record idempotent."""
    with get_db_connection(tmp_storage.database_url) as conn:
        for _ in range(2):
            conn.exec_driver_sql(
                """
                INSERT INTO entity_keys (entity_type, entity_id, key_type, key_value)
                VALUES ('manifestation', 1, 'work', 'abc')
                ON CONFLICT (entity_type, entity_id, key_type, key_value) DO NOTHING;
                """
            )
        assert conn.exec_driver_sql("SELECT COUNT(*) FROM entity_keys;").scalar() == 1


def test_extensible_types_in_authority_db(tmp_storage: StorageConfig):
    """Verify that PostgreSQL schema supports newly added work_type and content_type without CHECK violations."""
    with get_db_connection(tmp_storage.database_url) as conn:
        conn.exec_driver_sql(
            """
            INSERT INTO works (source, source_id, title, title_normalized, work_type)
            VALUES ('custom_src', 'f1', 'Future Simulation', 'future simulation', 'quantum_sim')
            ON CONFLICT (source, source_id) DO UPDATE SET work_type = EXCLUDED.work_type;
            """
        )
        w_id = (
            conn.exec_driver_sql("SELECT id FROM works WHERE source_id = 'f1';")
            .mappings()
            .fetchone()["id"]
        )
        conn.exec_driver_sql(
            """
            INSERT INTO expressions (work_id, source, source_id, language, content_type)
            VALUES (%s, 'custom_src', 'ef1', 'und', 'neural_field_weights')
            ON CONFLICT (source, source_id) DO UPDATE SET content_type = EXCLUDED.content_type;
            """,
            (w_id,),
        )
        assert (
            conn.exec_driver_sql("SELECT work_type FROM works WHERE source_id = 'f1';").scalar()
            == "quantum_sim"
        )
        assert (
            conn.exec_driver_sql(
                "SELECT content_type FROM expressions WHERE source_id = 'ef1';"
            ).scalar()
            == "neural_field_weights"
        )
