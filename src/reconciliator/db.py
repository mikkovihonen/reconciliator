"""SQLAlchemy 2.0 Core database management, schema definitions, and WAL pragmas for Reconciliator."""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Optional

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Engine,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    create_engine,
    event,
    func,
    select,
)
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.pool import NullPool, StaticPool

from reconciliator.config import StorageConfig

logger = logging.getLogger(__name__)

# Shared MetaData for Authority and Corpus Relational Tables
metadata = MetaData()

# ===================================================================
# 1. Authority Core Tables (§4.1)
# ===================================================================

db_metadata_table = Table(
    "db_metadata",
    metadata,
    Column("key", String, primary_key=True),
    Column("value", String, nullable=True),
)

sources_table = Table(
    "sources",
    metadata,
    Column("name", String, primary_key=True),
    Column("is_authoritative", Boolean, nullable=False, default=True),
    Column("first_seen", DateTime, server_default=func.current_timestamp()),
    Column("last_seen", DateTime, server_default=func.current_timestamp()),
    Index("idx_sources_authoritative", "is_authoritative"),
)

agents_table = Table(
    "agents",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("agent_uri", String, nullable=False, unique=True),
    Column("source", String, nullable=False),
    Column("record_origin", String, nullable=False, default="unknown", server_default="unknown"),
    Column("wikidata_qid", String, nullable=True),
    Column("name", String, nullable=False),
    Column("name_normalized", String, nullable=False),
    Column("surname", String, nullable=True),
    Column("qualifier", String, nullable=True),
    Column("dates", String, nullable=True),
    Column("birth_year", Integer, nullable=True),
    Column("death_year", Integer, nullable=True),
    Column("fuller_name", String, nullable=True),
    Column("affiliation", String, nullable=True),
    Column("metadata_json", Text, nullable=True),
    Column("is_personal", Boolean, nullable=False, default=True, server_default="1"),
    Column("is_fictional", Boolean, nullable=False, default=False, server_default="0"),
    Index("idx_agents_uri", "agent_uri"),
    Index("idx_agents_qid", "wikidata_qid"),
    Index("idx_agents_name_norm", "name_normalized"),
    Index("idx_agents_surname", "surname"),
)

works_table = Table(
    "works",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("work_uri", String, nullable=False, unique=True),
    Column("source", String, nullable=False),
    Column("source_id", String, nullable=False),
    Column("record_origin", String, nullable=False, default="unknown", server_default="unknown"),
    Column("wikidata_qid", String, nullable=True),
    Column("title", String, nullable=False),
    Column("title_normalized", String, nullable=False),
    Column("original_language", String, nullable=True),
    Column("primary_agent_id", Integer, ForeignKey("agents.id", ondelete="SET NULL"), nullable=True),
    Column("ykl_class", String, nullable=True),
    Column("work_type", String, nullable=False, default="text", server_default="text"),
    Column("subjects_json", Text, nullable=False, default="[]", server_default="[]"),
    Column("serialized_text", Text, nullable=True),
    Column("canonical_work_id", Integer, ForeignKey("works.id", ondelete="SET NULL"), nullable=True),
    Column("created_at", DateTime, server_default=func.current_timestamp()),
    Index("idx_works_uri", "work_uri"),
    Index("idx_works_canonical", "canonical_work_id"),
    Index("idx_works_qid", "wikidata_qid"),
    Index("idx_works_source_id", "source", "source_id"),
    Index("idx_works_title_norm", "title_normalized"),
    Index("idx_works_primary_agent", "primary_agent_id"),
    Index("idx_works_type", "work_type"),
)

expressions_table = Table(
    "expressions",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("work_id", Integer, ForeignKey("works.id", ondelete="CASCADE"), nullable=False),
    Column("expression_uri", String, nullable=False, unique=True),
    Column("source", String, nullable=False),
    Column("source_id", String, nullable=False),
    Column("record_origin", String, nullable=False, default="unknown", server_default="unknown"),
    Column("wikidata_qid", String, nullable=True),
    Column("language", String, nullable=False),
    Column("content_type", String, nullable=False, default="text", server_default="text"),
    Column("title", String, nullable=True),
    Column("version_designation", String, nullable=True),
    Column("arrangement_note", String, nullable=True),
    Column("medium_of_performance", String, nullable=True),
    Column("serialized_text", Text, nullable=True),
    Column("created_at", DateTime, server_default=func.current_timestamp()),
    Index("idx_expressions_work_id", "work_id"),
    Index("idx_expressions_uri", "expression_uri"),
    Index("idx_expressions_lookup", "work_id", "language", "content_type"),
)

manifestations_table = Table(
    "manifestations",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("work_id", Integer, ForeignKey("works.id", ondelete="SET NULL"), nullable=True),
    Column("container_id", Integer, ForeignKey("manifestations.id", ondelete="CASCADE"), nullable=True),
    Column("is_component", Boolean, nullable=False, default=False, server_default="0"),
    Column("component_type", String, nullable=True),
    Column("manifestation_uri", String, nullable=True),
    Column("source", String, nullable=False),
    Column("source_id", String, nullable=False),
    Column("record_origin", String, nullable=False, default="unknown", server_default="unknown"),
    Column("title", String, nullable=False),
    Column("title_normalized", String, nullable=False),
    Column("subtitle", String, nullable=True),
    Column("publication_year", Integer, nullable=True),
    Column("publisher", String, nullable=True),
    Column("publication_place", String, nullable=True),
    Column("edition_statement", String, nullable=True),
    Column("edition_number", Integer, nullable=True),
    Column("series_title", String, nullable=True),
    Column("series_numbering", String, nullable=True),
    Column("volume_designation", String, nullable=True),
    Column("issue_enumeration", String, nullable=True),
    Column("pagination", String, nullable=True),
    Column("extent", String, nullable=True),
    Column("carrier_type", String, nullable=False, default="volume", server_default="volume"),
    Column("host_title", String, nullable=True),
    Column("serialized_text", Text, nullable=True),
    Column("created_at", DateTime, server_default=func.current_timestamp()),
    UniqueConstraint("source", "source_id", name="uq_manifestations_source_source_id"),
    Index("idx_manifestations_work_id", "work_id"),
    Index("idx_manifestations_container", "container_id"),
    Index("idx_manifestations_component", "is_component", "component_type"),
    Index("idx_manifestations_year", "publication_year"),
    Index("idx_manifestations_series", "series_title"),
    Index("idx_manifestations_source_id", "source", "source_id"),
)

manifestation_expressions_table = Table(
    "manifestation_expressions",
    metadata,
    Column("manifestation_id", Integer, ForeignKey("manifestations.id", ondelete="CASCADE"), primary_key=True),
    Column("expression_id", Integer, ForeignKey("expressions.id", ondelete="CASCADE"), primary_key=True),
    Column("track_number", Integer, nullable=True),
    Column("disc_number", Integer, default=1, server_default="1"),
    Column("duration", String, nullable=True),
    Column("is_primary", Boolean, nullable=False, default=True, server_default="1"),
    Index("idx_manif_exp_track", "manifestation_id", "track_number"),
)

items_table = Table(
    "items",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("manifestation_id", Integer, ForeignKey("manifestations.id", ondelete="CASCADE"), nullable=False),
    Column("item_uri", String, unique=True, nullable=True),
    Column("source", String, nullable=False),
    Column("source_id", String, nullable=False),
    Column("record_origin", String, nullable=False, default="unknown", server_default="unknown"),
    Column("holding_organization", String, nullable=False),
    Column("shelf_mark", String, nullable=True),
    Column("barcode", String, nullable=True),
    Column("custodial_history", String, nullable=True),
    Column("item_condition", String, nullable=True),
    Column("created_at", DateTime, server_default=func.current_timestamp()),
    Index("idx_items_manif_id", "manifestation_id"),
    Index("idx_items_barcode", "barcode"),
    Index("idx_items_org", "holding_organization"),
)

identifiers_table = Table(
    "identifiers",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("entity_type", String, nullable=False),
    Column("entity_id", Integer, nullable=False),
    Column("scheme", String, nullable=False),
    Column("value", String, nullable=False),
    Column("value_normalized", String, nullable=False),
    CheckConstraint(
        "entity_type IN ('work', 'expression', 'manifestation', 'item', 'agent')",
        name="chk_identifiers_entity_type",
    ),
    Index("idx_identifiers_lookup", "scheme", "value_normalized"),
    Index("idx_identifiers_entity", "entity_type", "entity_id"),
)

entity_keys_table = Table(
    "entity_keys",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("entity_type", String, nullable=False),
    Column("entity_id", Integer, nullable=False),
    Column("key_type", String, nullable=False),
    Column("key_value", String, nullable=False),
    CheckConstraint(
        "entity_type IN ('work', 'expression', 'manifestation', 'item', 'agent')",
        name="chk_entity_keys_entity_type",
    ),
    UniqueConstraint(
        "entity_type", "entity_id", "key_type", "key_value",
        name="uq_entity_keys_composite",
    ),
    Index("idx_entity_keys_entity", "entity_type", "entity_id"),
    Index("idx_entity_keys_lookup", "key_type", "key_value"),
    Index("idx_entity_keys_value", "key_value"),
)

contributions_table = Table(
    "contributions",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("entity_type", String, nullable=False),
    Column("entity_id", Integer, nullable=False),
    Column("agent_id", Integer, ForeignKey("agents.id", ondelete="CASCADE"), nullable=False),
    Column("role", String, nullable=False),
    Column("is_primary", Boolean, nullable=False, default=False, server_default="0"),
    Column("field_tag", String, nullable=True),
    CheckConstraint(
        "entity_type IN ('work', 'expression', 'manifestation', 'item')",
        name="chk_contributions_entity_type",
    ),
    Index("idx_contributions_entity", "entity_type", "entity_id"),
    Index("idx_contributions_agent", "agent_id", "role"),
)

work_relationships_table = Table(
    "work_relationships",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("source_work_id", Integer, ForeignKey("works.id", ondelete="CASCADE"), nullable=False),
    Column("target_work_id", Integer, ForeignKey("works.id", ondelete="CASCADE"), nullable=False),
    Column("relationship_type", String, nullable=False),
    Column("created_at", DateTime, server_default=func.current_timestamp()),
    UniqueConstraint(
        "source_work_id", "target_work_id", "relationship_type",
        name="uq_work_relationships_edge",
    ),
    Index("idx_work_rel_source", "source_work_id", "relationship_type"),
    Index("idx_work_rel_target", "target_work_id", "relationship_type"),
)

work_medium_of_performance_table = Table(
    "work_medium_of_performance",
    metadata,
    Column("work_id", Integer, ForeignKey("works.id", ondelete="CASCADE"), primary_key=True),
    Column("medium_uri", String, primary_key=True),
    Column("label", String, nullable=False),
    Column("player_count", Integer, nullable=True),
    Column("is_soloist", Boolean, nullable=False, default=False, server_default="0"),
)

work_vectors_table = Table(
    "work_vectors",
    metadata,
    Column("vector_id", Integer, primary_key=True),
    Column("work_id", Integer, ForeignKey("works.id", ondelete="CASCADE"), nullable=False),
    Index("idx_work_vectors_work_id", "work_id"),
)

manifestation_vectors_table = Table(
    "manifestation_vectors",
    metadata,
    Column("vector_id", Integer, primary_key=True),
    Column("manifestation_id", Integer, ForeignKey("manifestations.id", ondelete="CASCADE"), nullable=False),
    Index("idx_manifestation_vectors_manif_id", "manifestation_id"),
)

harvest_state_table = Table(
    "harvest_state",
    metadata,
    Column("source_id", String, primary_key=True),
    Column("source_type", String, nullable=False),
    Column("metadata_prefix", String, nullable=False),
    Column("set_spec", String, nullable=True),
    Column("last_harvest_from", String, nullable=True),
    Column("last_harvest_until", String, nullable=True),
    Column("resumption_token", String, nullable=True),
    Column("records_harvested", Integer, nullable=False, default=0, server_default="0"),
    Column("status", String, nullable=False, default="in_progress", server_default="in_progress"),
    Column("completed_slices", Text, nullable=True),
    Column("updated_at", DateTime, server_default=func.current_timestamp()),
)

# ===================================================================
# 2. Corpus Staging and Clustering Tables (§4.2)
# ===================================================================

corpus_manifestation_clusters_table = Table(
    "corpus_manifestation_clusters",
    metadata,
    Column("cluster_id", String, primary_key=True),
    Column("canonical_record_id", String, nullable=False),
    Column("publisher", String, nullable=True),
    Column("publication_year", Integer, nullable=True),
    Column("edition_statement", String, nullable=True),
    Column("extent", String, nullable=True),
    Column("carrier_type", String, nullable=True),
    Column("member_record_count", Integer, nullable=False, default=1, server_default="1"),
)

corpus_work_clusters_table = Table(
    "corpus_work_clusters",
    metadata,
    Column("cluster_id", String, primary_key=True),
    Column("work_type", String, nullable=False, default="text", server_default="text"),
    Column("canonical_work_uri", String, nullable=True),
    Column("wikidata_qid", String, nullable=True),
    Column("canonical_title", String, nullable=False),
    Column("canonical_author", String, nullable=True),
)

corpus_expression_clusters_table = Table(
    "corpus_expression_clusters",
    metadata,
    Column("cluster_id", String, primary_key=True),
    Column("work_cluster_id", String, ForeignKey("corpus_work_clusters.cluster_id"), nullable=False),
    Column("canonical_expression_uri", String, nullable=True),
    Column("content_type", String, nullable=False, default="text", server_default="text"),
    Column("language", String, nullable=False),
    Column("version_designation", String, nullable=True),
    Column("translators_json", Text, nullable=False, default="[]", server_default="[]"),
    Column("performers_json", Text, nullable=False, default="[]", server_default="[]"),
    Index("idx_exp_cluster_work", "work_cluster_id"),
)

corpus_linkages_table = Table(
    "corpus_linkages",
    metadata,
    Column("record_id", String, primary_key=True),
    Column("manifestation_cluster_id", String, ForeignKey("corpus_manifestation_clusters.cluster_id"), nullable=True),
    Column("work_cluster_id", String, ForeignKey("corpus_work_clusters.cluster_id"), nullable=False),
    Column("expression_cluster_id", String, ForeignKey("corpus_expression_clusters.cluster_id"), nullable=False),
    Column("final_work_uri", String, nullable=False, default="", server_default=""),
    Column("final_expression_uri", String, nullable=False, default="", server_default=""),
    Column("sibling_manifestation_ids_json", Text, nullable=True),
    Index("idx_linkage_manifestation", "manifestation_cluster_id"),
    Index("idx_linkage_work", "work_cluster_id"),
)

sammelband_bindings_table = Table(
    "sammelband_bindings",
    metadata,
    Column("binding_id", String, primary_key=True),
    Column("record_id", String, primary_key=True),
    Column("sequence_in_binding", Integer, nullable=False, default=1, server_default="1"),
    Column("binding_notes", String, nullable=True),
    Index("idx_sammelband_binding", "binding_id"),
    Index("idx_sammelband_record", "record_id"),
)

# ===================================================================
# 3. FTS5 Virtual Tables & Triggers DDL Statements
# ===================================================================

FTS5_DDL_STATEMENTS = [
    """
    CREATE VIRTUAL TABLE IF NOT EXISTS works_fts USING fts5(
        serialized_text,
        content='works',
        content_rowid='id',
        tokenize='unicode61 remove_diacritics 2'
    );
    """,
    """
    CREATE VIRTUAL TABLE IF NOT EXISTS expressions_fts USING fts5(
        serialized_text,
        content='expressions',
        content_rowid='id',
        tokenize='unicode61 remove_diacritics 2'
    );
    """,
    """
    CREATE VIRTUAL TABLE IF NOT EXISTS manifestations_fts USING fts5(
        serialized_text,
        content='manifestations',
        content_rowid='id',
        tokenize='unicode61 remove_diacritics 2'
    );
    """,
    # Works Triggers
    """
    CREATE TRIGGER IF NOT EXISTS works_ai AFTER INSERT ON works BEGIN
        INSERT INTO works_fts(rowid, serialized_text) VALUES (new.id, new.serialized_text);
    END;
    """,
    """
    CREATE TRIGGER IF NOT EXISTS works_ad AFTER DELETE ON works BEGIN
        INSERT INTO works_fts(works_fts, rowid, serialized_text) VALUES('delete', old.id, old.serialized_text);
    END;
    """,
    """
    CREATE TRIGGER IF NOT EXISTS works_au AFTER UPDATE ON works BEGIN
        INSERT INTO works_fts(works_fts, rowid, serialized_text) VALUES('delete', old.id, old.serialized_text);
        INSERT INTO works_fts(rowid, serialized_text) VALUES (new.id, new.serialized_text);
    END;
    """,
    # Expressions Triggers
    """
    CREATE TRIGGER IF NOT EXISTS expressions_ai AFTER INSERT ON expressions BEGIN
        INSERT INTO expressions_fts(rowid, serialized_text) VALUES (new.id, new.serialized_text);
    END;
    """,
    """
    CREATE TRIGGER IF NOT EXISTS expressions_ad AFTER DELETE ON expressions BEGIN
        INSERT INTO expressions_fts(expressions_fts, rowid, serialized_text) VALUES('delete', old.id, old.serialized_text);
    END;
    """,
    """
    CREATE TRIGGER IF NOT EXISTS expressions_au AFTER UPDATE ON expressions BEGIN
        INSERT INTO expressions_fts(expressions_fts, rowid, serialized_text) VALUES('delete', old.id, old.serialized_text);
        INSERT INTO expressions_fts(rowid, serialized_text) VALUES (new.id, new.serialized_text);
    END;
    """,
    # Manifestations Triggers
    """
    CREATE TRIGGER IF NOT EXISTS manifestations_ai AFTER INSERT ON manifestations BEGIN
        INSERT INTO manifestations_fts(rowid, serialized_text) VALUES (new.id, new.serialized_text);
    END;
    """,
    """
    CREATE TRIGGER IF NOT EXISTS manifestations_ad AFTER DELETE ON manifestations BEGIN
        INSERT INTO manifestations_fts(manifestations_fts, rowid, serialized_text) VALUES('delete', old.id, old.serialized_text);
    END;
    """,
    """
    CREATE TRIGGER IF NOT EXISTS manifestations_au AFTER UPDATE ON manifestations BEGIN
        INSERT INTO manifestations_fts(manifestations_fts, rowid, serialized_text) VALUES('delete', old.id, old.serialized_text);
        INSERT INTO manifestations_fts(rowid, serialized_text) VALUES (new.id, new.serialized_text);
    END;
    """,
]


# ===================================================================
# 4. Result & Connection Adapters
# ===================================================================

class RowAdapter:
    """Mapping & index-accessible row adapter for SQLAlchemy Row."""

    __slots__ = ("_row",)

    def __init__(self, row: Any) -> None:
        self._row = row

    def __getitem__(self, key: Any) -> Any:
        if isinstance(key, (int, slice)):
            return self._row[key]
        return self._row._mapping[key]

    def get(self, key: Any, default: Any = None) -> Any:
        return self._row._mapping.get(key, default)

    def keys(self) -> Any:
        return self._row._mapping.keys()

    def values(self) -> Any:
        return self._row._mapping.values()

    def items(self) -> Any:
        return self._row._mapping.items()

    def __iter__(self) -> Any:
        return iter(self._row._mapping)

    def __len__(self) -> int:
        return len(self._row)


class ResultAdapter:
    """Cursor/Result adapter for SQLAlchemy CursorResult."""

    __slots__ = ("_result",)

    def __init__(self, result: Any) -> None:
        self._result = result

    def fetchone(self) -> Optional[RowAdapter]:
        row = self._result.fetchone()
        return RowAdapter(row) if row is not None else None

    def fetchmany(self, size: Optional[int] = None) -> list[RowAdapter]:
        if size is None:
            rows = self._result.fetchmany()
        else:
            rows = self._result.fetchmany(size)
        return [RowAdapter(r) for r in rows]

    def fetchall(self) -> list[RowAdapter]:
        return [RowAdapter(r) for r in self._result.fetchall()]

    def __iter__(self) -> Any:
        for r in self._result:
            yield RowAdapter(r)

    @property
    def rowcount(self) -> int:
        rc = self._result.rowcount
        return int(rc) if rc is not None else -1

    @property
    def lastrowid(self) -> Any:
        return getattr(self._result, "lastrowid", None)


class ConnectionAdapter:
    """Connection adapter supporting both raw SQL statements and SQLAlchemy Core statements."""

    def __init__(self, conn: Any, auto_close: bool = False) -> None:
        self._conn = conn
        self._auto_close = auto_close

    def execute(self, statement: Any, parameters: Any = None) -> ResultAdapter:
        if isinstance(statement, str):
            if parameters is None:
                res = self._conn.exec_driver_sql(statement)
            else:
                params = tuple(parameters) if isinstance(parameters, (list, set)) else parameters
                res = self._conn.exec_driver_sql(statement, params)
        elif parameters is not None:
            res = self._conn.execute(statement, parameters)
        else:
            res = self._conn.execute(statement)
        return ResultAdapter(res)

    def executemany(self, statement: Any, parameters: Any) -> ResultAdapter:
        if isinstance(statement, str):
            res = self._conn.exec_driver_sql(statement, list(parameters))
        else:
            res = self._conn.execute(statement, parameters)
        return ResultAdapter(res)

    def commit(self) -> None:
        if hasattr(self._conn, "commit"):
            self._conn.commit()

    def rollback(self) -> None:
        if hasattr(self._conn, "rollback"):
            self._conn.rollback()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> ConnectionAdapter:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        if hasattr(self._conn, "commit") and exc_type is None:
            try:
                self._conn.commit()
            except Exception:
                pass
        if self._auto_close:
            self._conn.close()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._conn, name)


# ===================================================================
# 5. Engine Lifecycle & Pragmas
# ===================================================================

def get_engine(db_path: Path | str, in_memory: bool = False) -> Engine:
    """Create a configured SQLAlchemy 2.0 Engine with SQLite WAL pragmas."""
    is_mem = in_memory or str(db_path) == ":memory:"
    if is_mem:
        engine = create_engine(
            "sqlite:///:memory:",
            poolclass=StaticPool,
            connect_args={"check_same_thread": False},
        )
    else:
        path = Path(db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        engine = create_engine(
            f"sqlite:///{path}",
            poolclass=NullPool,
            connect_args={"timeout": 30.0},
        )

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragmas(dbapi_connection: Any, connection_record: Any) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode = WAL;")
        cursor.execute("PRAGMA busy_timeout = 30000;")
        cursor.execute("PRAGMA synchronous = NORMAL;")
        cursor.execute("PRAGMA foreign_keys = ON;")
        cursor.close()

    return engine


def get_db_connection(db_path: Path | str) -> ConnectionAdapter:
    """Create a configured connection adapter backed by SQLAlchemy Engine."""
    engine = get_engine(db_path)
    conn = engine.connect()
    return ConnectionAdapter(conn, auto_close=True)


def _resolve_bind(target: Any) -> tuple[Any, Any]:
    """Resolve target into a SQLAlchemy bindable and an executor function."""
    if isinstance(target, Engine):
        return target, lambda sql: target.connect().exec_driver_sql(sql)
    if hasattr(target, "_conn"):
        return target._conn, lambda sql: target._conn.exec_driver_sql(sql)
    if hasattr(target, "exec_driver_sql"):
        return target, lambda sql: target.exec_driver_sql(sql)
    if hasattr(target, "execute"):
        engine = create_engine("sqlite://", creator=lambda: target)
        return engine, lambda sql: target.execute(sql)
    raise TypeError(f"Unsupported target type for database initialization: {type(target)}")


def init_authority_db(target: Any) -> None:
    """Initialize relational tables, indexes, and FTS5 virtual tables in authority.db."""
    bind, exec_sql = _resolve_bind(target)
    metadata.create_all(bind)
    for stmt in FTS5_DDL_STATEMENTS:
        exec_sql(stmt)
    if hasattr(bind, "commit"):
        bind.commit()


def init_corpus_db(target: Any) -> None:
    """Initialize corpus clustering and linkage tables."""
    bind, _ = _resolve_bind(target)
    metadata.create_all(
        bind,
        tables=[
            corpus_manifestation_clusters_table,
            corpus_work_clusters_table,
            corpus_expression_clusters_table,
            corpus_linkages_table,
            sammelband_bindings_table,
        ],
    )
    if hasattr(bind, "commit"):
        bind.commit()


def set_metadata(target: Any, key: str, value: str) -> None:
    """Upsert a key-value pair in db_metadata."""
    bind, _ = _resolve_bind(target)
    if isinstance(bind, Engine):
        with bind.begin() as conn:
            stmt = sqlite_insert(db_metadata_table).values(key=key, value=value)
            stmt = stmt.on_conflict_do_update(
                index_elements=["key"],
                set_={"value": stmt.excluded.value},
            )
            conn.execute(stmt)
    else:
        stmt = sqlite_insert(db_metadata_table).values(key=key, value=value)
        stmt = stmt.on_conflict_do_update(
            index_elements=["key"],
            set_={"value": stmt.excluded.value},
        )
        bind.execute(stmt)
        if hasattr(bind, "commit"):
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


def reset_all_storage(storage_config: StorageConfig) -> None:
    """Delete every database and vector index, then reinitialize clean schemas."""
    from reconciliator.indexer.ipc.client import DaemonLifecycleManager
    from reconciliator.indexer.ipc.protocol import get_default_socket_path
    from reconciliator.progress import init_progress_db
    from reconciliator.vocab_store import init_vocab_db

    # Stop vector daemon first
    try:
        base_dir = storage_config.authority_db.parent if storage_config.authority_db else Path.cwd()
        sock_p = storage_config.vector_engine.daemon.socket_path or get_default_socket_path(base_dir)
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

    for db_path, initializer in (
        (storage_config.authority_db, init_authority_db),
        (storage_config.vocab_db, init_vocab_db),
        (storage_config.progress_db, init_progress_db),
    ):
        if db_path:
            reset_database_file(db_path)
            engine = get_engine(db_path)
            try:
                initializer(engine)
            finally:
                engine.dispose()
