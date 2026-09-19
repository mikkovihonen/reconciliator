"""SQLAlchemy 2.0 Core database management, engine lifecycle, and schema initialization."""

from __future__ import annotations

import hashlib
import logging
import os
import time
from pathlib import Path
from typing import Any, Optional, Sequence

from sqlalchemy import (
    Engine,
    MetaData,
    Table,
    create_engine,
    func,
    select,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.pool import QueuePool

from reconciliator.config import PostgresConfig, StorageConfig, get_default_database_url
from reconciliator.database.schema import (
    PG_SEARCH_DDL_STATEMENTS,
    corpus_agent_cluster_attributes_table,
    corpus_agent_cluster_members_table,
    corpus_agent_clusters_table,
    corpus_expression_clusters_table,
    corpus_linkages_table,
    corpus_manifestation_clusters_table,
    corpus_work_clusters_table,
    db_metadata_table,
    metadata,
    sammelband_bindings_table,
)

logger = logging.getLogger(__name__)


def get_engine(
    db_target: Path | str | None = None,
    in_memory: bool = False,
    pool_size: Optional[int] = None,
    max_overflow: Optional[int] = None,
    pool_timeout: Optional[float] = None,
    pool_recycle: Optional[int] = None,
    pool_pre_ping: Optional[bool] = None,
    schema: Optional[str] = None,
) -> Engine:
    """Create a configured SQLAlchemy 2.0 Engine for PostgreSQL.

    Default connection strings, pool sizes, and timeouts are resolved from the
    configuration surface (defaults.yaml / PostgresConfig).
    """
    pg_cfg = PostgresConfig()
    effective_pool_size = pool_size if pool_size is not None else pg_cfg.pool_size
    effective_max_overflow = max_overflow if max_overflow is not None else pg_cfg.max_overflow
    effective_pool_timeout = pool_timeout if pool_timeout is not None else pg_cfg.pool_timeout
    effective_pool_recycle = pool_recycle if pool_recycle is not None else pg_cfg.pool_recycle
    effective_pool_pre_ping = pool_pre_ping if pool_pre_ping is not None else pg_cfg.pool_pre_ping

    default_url = get_default_database_url()

    if db_target is None:
        target_str = os.environ.get("RECONCILIATOR_DATABASE_URL", default_url)
    else:
        target_str = str(db_target)
        if target_str.startswith("postgresql+psycopg:/") and not target_str.startswith(
            "postgresql+psycopg://"
        ):
            target_str = target_str.replace("postgresql+psycopg:/", "postgresql+psycopg://", 1)
        elif target_str.startswith("postgresql:/") and not target_str.startswith("postgresql://"):
            target_str = target_str.replace("postgresql:/", "postgresql://", 1)
        elif not (target_str.startswith("postgresql://") or target_str.startswith("postgresql+")):
            if schema is None:
                p = Path(target_str)
                schema_path_obj = p.parent.resolve() if p.suffix == ".db" else p.resolve()
                is_default_data = (
                    (
                        schema_path_obj.name == "data"
                        and schema_path_obj == (Path.cwd() / "data").resolve()
                    )
                    or p == Path("data")
                    or (p.parent == Path("data"))
                )
                if not is_default_data:
                    schema_path = str(schema_path_obj)
                    schema = f"test_{hashlib.md5(schema_path.encode()).hexdigest()[:10]}"
            target_str = os.environ.get("RECONCILIATOR_DATABASE_URL", default_url)

    if target_str.startswith("postgresql://"):
        target_str = target_str.replace("postgresql://", "postgresql+psycopg://", 1)

    connect_args: dict[str, Any] = {}
    if schema:
        try:
            import psycopg

            raw_url = target_str.split("?")[0].replace("postgresql+psycopg://", "postgresql://", 1)
            with psycopg.connect(raw_url) as conn:
                with conn.cursor() as cur:
                    cur.execute(f"CREATE SCHEMA IF NOT EXISTS {schema};")
        except Exception:
            pass
        connect_args["options"] = f"-csearch_path={schema},public"

    engine = create_engine(
        target_str,
        poolclass=QueuePool,
        pool_size=effective_pool_size,
        max_overflow=effective_max_overflow,
        pool_timeout=effective_pool_timeout,
        pool_recycle=effective_pool_recycle,
        pool_pre_ping=effective_pool_pre_ping,
        connect_args=connect_args,
    )
    return engine


def get_db_connection(db_path: Path | str | None = None) -> Any:
    """Create a configured transaction connection context manager backed by SQLAlchemy Engine."""
    engine = get_engine(db_path)
    return engine.begin()


def _resolve_bind(target: Any) -> tuple[Any, Any]:
    """Resolve target into a SQLAlchemy bindable and an executor function."""
    if isinstance(target, Engine):
        return target, lambda sql: target.connect().exec_driver_sql(sql)
    if hasattr(target, "exec_driver_sql"):
        return target, lambda sql: target.exec_driver_sql(sql)
    if hasattr(target, "execute"):
        return target, lambda sql: target.execute(sql)
    raise TypeError(f"Unsupported target type for database initialization: {type(target)}")


def create_schema_tables(conn: Any, tables: Sequence[Table]) -> None:
    """Create the given tables in the connection's current_schema if they do not exist."""
    if isinstance(conn, Engine):
        with conn.begin() as c:
            create_schema_tables(c, tables)
        return
    existing = set(
        conn.exec_driver_sql(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema();"
        ).scalars()
    )
    for table in tables:
        if table.name not in existing:
            table.create(conn)


def init_authority_db(target: Any) -> None:
    """Initialize relational tables, indexes, and PostgreSQL full-text/trigram indexes."""
    if isinstance(target, Engine):
        with target.begin() as conn:
            init_authority_db(conn)
        return

    bind, exec_sql = _resolve_bind(target)
    in_tx = hasattr(bind, "in_transaction") and (
        bind.in_transaction() if callable(bind.in_transaction) else bool(bind.in_transaction)
    )

    if in_tx:
        create_schema_tables(bind, metadata.sorted_tables)
        for stmt in PG_SEARCH_DDL_STATEMENTS:
            try:
                with bind.begin_nested():
                    exec_sql(stmt)
            except Exception as e:
                logger.debug("Execution of DDL statement failed: %s: %s", stmt, e)
        return

    if hasattr(bind, "begin"):
        with bind.begin():
            create_schema_tables(bind, metadata.sorted_tables)
            for stmt in PG_SEARCH_DDL_STATEMENTS:
                try:
                    with bind.begin_nested():
                        exec_sql(stmt)
                except Exception as e:
                    logger.debug("Execution of DDL statement failed: %s: %s", stmt, e)
    else:
        create_schema_tables(bind, metadata.sorted_tables)
        for stmt in PG_SEARCH_DDL_STATEMENTS:
            try:
                exec_sql(stmt)
            except Exception as e:
                logger.debug("Execution of DDL statement failed: %s: %s", stmt, e)
        if hasattr(bind, "commit"):
            bind.commit()


def init_corpus_db(target: Any) -> None:
    """Initialize corpus clustering and linkage tables."""
    tables = [
        corpus_manifestation_clusters_table,
        corpus_work_clusters_table,
        corpus_expression_clusters_table,
        corpus_linkages_table,
        sammelband_bindings_table,
        corpus_agent_clusters_table,
        corpus_agent_cluster_members_table,
        corpus_agent_cluster_attributes_table,
    ]

    if isinstance(target, Engine):
        with target.begin() as conn:
            init_corpus_db(conn)
        return

    bind, exec_sql = _resolve_bind(target)
    in_tx = hasattr(bind, "in_transaction") and (
        bind.in_transaction() if callable(bind.in_transaction) else bool(bind.in_transaction)
    )
    if in_tx:
        create_schema_tables(bind, tables)
        return

    if hasattr(bind, "begin"):
        with bind.begin():
            create_schema_tables(bind, tables)
    else:
        create_schema_tables(bind, tables)
        if hasattr(bind, "commit"):
            bind.commit()


def set_metadata(target: Any, key: str, value: str) -> None:
    """Upsert a key-value pair in db_metadata."""
    bind, _ = _resolve_bind(target)
    if isinstance(bind, Engine):
        with bind.begin() as conn:
            stmt = pg_insert(db_metadata_table).values(key=key, value=value)
            stmt = stmt.on_conflict_do_update(
                index_elements=["key"],
                set_={"value": stmt.excluded.value},
            )
            conn.execute(stmt)
    else:
        stmt = pg_insert(db_metadata_table).values(key=key, value=value)
        stmt = stmt.on_conflict_do_update(
            index_elements=["key"],
            set_={"value": stmt.excluded.value},
        )
        bind.execute(stmt)
        if hasattr(bind, "commit") and getattr(bind, "_trans_context_manager", None) is None:
            bind.commit()


def get_metadata(target: Any, key: str) -> Optional[str]:
    """Get metadata value by key."""
    bind, _ = _resolve_bind(target)
    if isinstance(bind, Engine):
        with bind.connect() as conn:
            stmt = select(db_metadata_table.c.value).where(db_metadata_table.c.key == key)
            row = conn.execute(stmt).fetchone()
            return str(row[0]) if row and row[0] is not None else None
    else:
        stmt = select(db_metadata_table.c.value).where(db_metadata_table.c.key == key)
        row = bind.execute(stmt).fetchone()
        return str(row[0]) if row and row[0] is not None else None


def reset_all_storage(storage_config: StorageConfig) -> None:
    """Delete vector indices and reset all database tables in PostgreSQL."""
    from reconciliator.indexer.ipc.client import DaemonLifecycleManager
    from reconciliator.indexer.ipc.protocol import get_default_socket_path

    # Stop vector daemon first
    try:
        base_dir = storage_config.data_dir if storage_config.data_dir else Path.cwd()
        sock_p = storage_config.vector_engine.daemon.socket_path or get_default_socket_path(
            base_dir
        )
        lifecycle = DaemonLifecycleManager(sock_p)
        if lifecycle.is_running():
            lifecycle.shutdown(save=False)
            time.sleep(0.1)
    except Exception:
        pass

    for index_path in (
        storage_config.work_index,
        storage_config.manifestation_index,
        storage_config.agent_index,
    ):
        if index_path and index_path.exists():
            try:
                index_path.unlink()
            except OSError:
                pass

    db_url = getattr(storage_config, "database_url", None)
    engine = get_engine(db_url)
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql("""
                DO $$ DECLARE
                    r RECORD;
                BEGIN
                    FOR r IN (SELECT tablename FROM pg_tables WHERE schemaname = current_schema()) LOOP
                        EXECUTE 'DROP TABLE IF EXISTS ' || quote_ident(r.tablename) || ' CASCADE';
                    END LOOP;
                END $$;
            """)
            curr_schema = conn.execute(select(func.current_schema())).scalar()
            if curr_schema == "public":
                if storage_config.data_dir:
                    try:
                        data_hash = hashlib.md5(
                            str(storage_config.data_dir.resolve()).encode()
                        ).hexdigest()[:10]
                        conn.exec_driver_sql(f"DROP SCHEMA IF EXISTS test_{data_hash} CASCADE;")
                    except Exception:
                        pass
        init_authority_db(engine)
        init_corpus_db(engine)
        from reconciliator.progress import init_progress_db
        from reconciliator.vocab_store import init_vocab_db

        init_vocab_db(engine)
        init_progress_db(engine)
    finally:
        engine.dispose()


__all__ = [
    "get_engine",
    "get_db_connection",
    "create_schema_tables",
    "init_authority_db",
    "init_corpus_db",
    "set_metadata",
    "get_metadata",
    "reset_all_storage",
]

