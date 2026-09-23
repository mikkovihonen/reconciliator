"""Tests for PostgreSQL engine configuration and schema definitions."""

from pathlib import Path

from sqlalchemy import text

from reconciliator.config import PostgresConfig
from reconciliator.database import (
    concepts_table,
    entity_subjects_table,
    get_db_connection,
    get_engine,
    get_podman_path,
    is_container_running,
    metadata,
)


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


def test_get_db_connection_context_manager(tmp_path: Path):
    db_file = tmp_path / "test_conn.db"
    with get_db_connection(db_file) as conn:
        res = conn.execute(text("SELECT 1 AS val")).mappings().fetchone()
        assert res is not None
        assert res["val"] == 1


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
