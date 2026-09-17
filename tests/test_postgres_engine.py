"""Tests for PostgreSQL engine configuration, SQL dialect adapter, and schema definitions."""

from pathlib import Path
from unittest.mock import MagicMock

from sqlalchemy.dialects import postgresql

from reconciliator.config import PostgresConfig
from reconciliator.db import (
    ConnectionAdapter,
    concepts_table,
    entity_subjects_table,
    get_engine,
    metadata,
)
from reconciliator.db_container import get_podman_path, is_container_running


def test_postgres_config_defaults():
    pg = PostgresConfig()
    assert pg.host == "localhost"
    assert pg.port == 5432
    assert pg.database == "reconciliator"
    assert pg.user == "postgres"
    assert pg.password == "postgres"
    assert (
        pg.get_database_url()
        == "postgresql+psycopg://postgres:postgres@localhost:5432/reconciliator"
    )
    assert pg.pool_size == 10
    assert pg.max_overflow == 20


def test_sql_adapter_placeholder_adaptation():
    conn = MagicMock()
    conn.dialect = postgresql.dialect()
    adapter = ConnectionAdapter(conn)

    sql = "SELECT * FROM works WHERE id = ?;"
    adapted = adapter._adapt_sql(sql)
    assert adapted == "SELECT * FROM works WHERE id = %s;"


def test_sql_adapter_postgres_placeholders():
    conn = MagicMock()
    conn.dialect = postgresql.dialect()
    adapter = ConnectionAdapter(conn)

    sql = "SELECT * FROM works WHERE id = ? AND source = ?;"
    adapted = adapter._adapt_sql(sql)
    assert adapted == "SELECT * FROM works WHERE id = %s AND source = %s;"


def test_sql_adapter_postgres_insert_or_ignore():
    conn = MagicMock()
    conn.dialect = postgresql.dialect()
    adapter = ConnectionAdapter(conn)

    sql = "INSERT OR IGNORE INTO entity_keys (entity_type, entity_id) VALUES (?, ?);"
    adapted = adapter._adapt_sql(sql)
    assert "INSERT INTO entity_keys" in adapted
    assert "ON CONFLICT DO NOTHING;" in adapted
    assert "%s" in adapted


def test_sql_adapter_postgres_insert_or_replace():
    conn = MagicMock()
    conn.dialect = postgresql.dialect()
    adapter = ConnectionAdapter(conn)

    sql = "INSERT OR REPLACE INTO work_vectors (vector_id, work_id) VALUES (?, ?);"
    adapted = adapter._adapt_sql(sql)
    assert "INSERT INTO work_vectors" in adapted
    assert "ON CONFLICT (vector_id) DO UPDATE SET work_id = EXCLUDED.work_id;" in adapted


def test_unified_schema_creation_and_constraints(tmp_path: Path):
    db_file = tmp_path / "unified.db"
    engine = get_engine(db_file)
    metadata.create_all(engine)

    # Verify tables exist in metadata
    assert "concepts" in metadata.tables
    assert "entity_subjects" in metadata.tables
    assert "task_progress" in metadata.tables
    assert "works" in metadata.tables
    assert "agents" in metadata.tables

    # Verify entity_subjects constraints
    table = entity_subjects_table
    col_names = {c.name for c in table.columns}
    assert {
        "id",
        "entity_type",
        "entity_id",
        "source",
        "source_record_id",
        "relation_type",
        "vocabulary",
        "concept_id",
        "raw_label",
        "is_resolved",
        "is_primary",
    }.issubset(col_names)

    # Verify concepts constraints
    c_table = concepts_table
    c_cols = {c.name for c in c_table.columns}
    assert {"id", "vocabulary", "concept_id"}.issubset(c_cols)
    assert "uri" not in c_cols


def test_podman_detection():
    # Should not raise exception even if podman binary is not installed or available
    path = get_podman_path()
    assert path is None or isinstance(path, str)
    running = is_container_running("non_existent_container_xyz_123")
    assert running is False
