"""Unit tests for PostgreSQL batch operations and dynamic chunk sizing."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from pydantic import BaseModel
from sqlalchemy import (
    Column,
    Integer,
    MetaData,
    String,
    Table,
    text,
)

from reconciliator.config import StorageConfig
from reconciliator.database import (
    calculate_batch_size,
    get_engine,
    insert_batch,
    normalize_row,
    start_postgres_container,
    upsert_batch,
)


class SampleModel(BaseModel):
    id: int | None = None
    code: str
    name: str
    extra: str = "default"


def test_calculate_batch_size():
    # 32,767 max parameters
    # For a 10-column table: 32767 // 10 = 3276
    assert calculate_batch_size(num_columns=10, max_parameters_per_batch=32767) == 3276
    # For a 1-column table: 32767
    assert calculate_batch_size(num_columns=1, max_parameters_per_batch=32767) == 32767
    # For a 100-column table: 32767 // 100 = 327
    assert calculate_batch_size(num_columns=100, max_parameters_per_batch=32767) == 327

    # Explicit batch size smaller than safe limit
    assert (
        calculate_batch_size(
            num_columns=10, max_parameters_per_batch=32767, explicit_batch_size=500
        )
        == 500
    )
    # Explicit batch size exceeding safe limit is clamped
    assert (
        calculate_batch_size(
            num_columns=10, max_parameters_per_batch=32767, explicit_batch_size=5000
        )
        == 3276
    )


def test_normalize_row():
    meta = MetaData()
    test_table = Table(
        "test_norm",
        meta,
        Column("code", String),
        Column("name", String),
    )

    # 1. Pydantic model
    m = SampleModel(code="C1", name="N1", extra="ignored")
    d1 = normalize_row(m, test_table)
    assert d1 == {"code": "C1", "name": "N1"}

    # 2. Plain dictionary
    d2 = normalize_row({"code": "C2", "name": "N2", "extra": "ignored"}, test_table)
    assert d2 == {"code": "C2", "name": "N2"}

    # 3. Without table filter (None id is stripped so sequences fire)
    d3 = normalize_row(m)
    assert d3 == {"code": "C1", "name": "N1", "extra": "ignored"}


def test_upsert_batch_and_insert_batch_empty():
    meta = MetaData()
    tbl = Table("dummy", meta, Column("id", Integer, primary_key=True))
    mock_bind = MagicMock()

    assert upsert_batch(mock_bind, tbl, [], return_ids=True) == []
    assert upsert_batch(mock_bind, tbl, [], return_ids=False) is None
    assert insert_batch(mock_bind, tbl, [], return_ids=True) == []
    assert insert_batch(mock_bind, tbl, [], return_ids=False) is None
    assert mock_bind.execute.call_count == 0


def test_batch_operations_with_postgres(tmp_storage: StorageConfig):
    engine = get_engine(tmp_storage.database_url)
    meta = MetaData()
    test_items = Table(
        "test_batch_items",
        meta,
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("sku", String(64), unique=True, nullable=False),
        Column("title", String(255), nullable=False),
        Column("price", Integer, nullable=False, default=0),
    )

    with engine.begin() as conn:
        conn.exec_driver_sql("DROP TABLE IF EXISTS test_batch_items CASCADE;")
        test_items.create(conn)

    try:
        # 1. Test insert_batch with returning IDs
        records = [
            {"sku": f"SKU-{i:03d}", "title": f"Item {i}", "price": i * 10} for i in range(1, 11)
        ]
        ids = insert_batch(
            bind=engine,
            table=test_items,
            rows=records,
            return_ids=True,
            batch_size=3,  # Force multiple chunks
        )
        assert ids is not None
        assert len(ids) == 10
        assert sorted(ids) == ids

        # 2. Test upsert_batch updating existing rows and inserting new ones
        upsert_records = [
            # Existing SKU with updated title and price
            {"sku": "SKU-001", "title": "Updated Item 1", "price": 999},
            # New SKU
            {"sku": "SKU-011", "title": "Item 11", "price": 110},
        ]
        upsert_ids = upsert_batch(
            bind=engine,
            table=test_items,
            rows=upsert_records,
            conflict_index_elements=["sku"],
            return_ids=True,
        )
        assert upsert_ids is not None
        assert len(upsert_ids) == 2
        assert upsert_ids[0] == ids[0]  # Same ID preserved on update

        with engine.connect() as conn:
            row1 = conn.execute(
                text("SELECT title, price FROM test_batch_items WHERE sku = 'SKU-001'")
            ).fetchone()
            assert row1 is not None
            assert row1[0] == "Updated Item 1"
            assert row1[1] == 999

            row11 = conn.execute(
                text("SELECT title, price FROM test_batch_items WHERE sku = 'SKU-011'")
            ).fetchone()
            assert row11 is not None
            assert row11[0] == "Item 11"
            assert row11[1] == 110

        # 3. Test upsert_batch with update_columns restriction
        restrict_records = [
            {"sku": "SKU-001", "title": "Title Modified", "price": 12345},
        ]
        upsert_batch(
            bind=engine,
            table=test_items,
            rows=restrict_records,
            conflict_index_elements=["sku"],
            update_columns=["title"],  # only title should change, price should stay 999
        )
        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT title, price FROM test_batch_items WHERE sku = 'SKU-001'")
            ).fetchone()
            assert row is not None
            assert row[0] == "Title Modified"
            assert row[1] == 999  # Preserved

        # 4. Test insert_batch with on_conflict_do_nothing
        duplicate_records = [
            {"sku": "SKU-001", "title": "Ignored", "price": 0},
            {"sku": "SKU-012", "title": "Item 12", "price": 120},
        ]
        new_ids = insert_batch(
            bind=engine,
            table=test_items,
            rows=duplicate_records,
            on_conflict_do_nothing=True,
            conflict_index_elements=["sku"],
            return_ids=True,
        )
        assert new_ids is not None
        assert len(new_ids) == 1  # Only SKU-012 inserted

    finally:
        with engine.begin() as conn:
            conn.exec_driver_sql("DROP TABLE IF EXISTS test_batch_items CASCADE;")
        engine.dispose()


def test_container_locale_configuration():
    """Verify that locale and initdb options in PostgresConfig are correctly passed to podman run."""
    with (
        patch("reconciliator.database.container.get_podman_path", return_value="/usr/bin/podman"),
        patch("reconciliator.database.container.is_container_running", return_value=False),
        patch("reconciliator.database.container.is_container_present", return_value=False),
        patch("subprocess.run") as mock_run,
    ):
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        start_postgres_container(
            host="localhost",
            port=5432,
            database="reconciliator",
            user="postgres",
            password="postgres",
            name="test-pg",
            image="docker.io/library/postgres:16-alpine",
            wait_ready=False,
            locale="fi_FI.UTF-8",
            lc_collate="fi_FI.UTF-8",
            lc_ctype="fi_FI.UTF-8",
            initdb_args="--locale=fi_FI.UTF-8 --lc-collate=fi_FI.UTF-8",
        )

        assert mock_run.called
        cmd_args = mock_run.call_args[0][0]
        assert "-e" in cmd_args
        assert "LANG=fi_FI.UTF-8" in cmd_args
        assert "LC_COLLATE=fi_FI.UTF-8" in cmd_args
        assert "LC_CTYPE=fi_FI.UTF-8" in cmd_args
        assert "POSTGRES_INITDB_ARGS=--locale=fi_FI.UTF-8 --lc-collate=fi_FI.UTF-8" in cmd_args
