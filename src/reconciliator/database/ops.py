"""PostgreSQL-native batch operations engine with parameter-safe dynamic chunking."""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Optional, TypeVar

from sqlalchemy import Engine, Table
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Connection

from reconciliator.config import PostgresConfig

logger = logging.getLogger(__name__)

T = TypeVar("T")


def calculate_batch_size(
    num_columns: int,
    max_parameters_per_batch: Optional[int] = None,
    explicit_batch_size: Optional[int] = None,
) -> int:
    """Calculate parameter-safe batch size for PostgreSQL asyncpg / psycopg protocol limits.

    PostgreSQL has a protocol parameter limit of 32,767 ($1..$32767).
    This computes a chunk size that strictly avoids parameter limit overflow.
    """
    if max_parameters_per_batch is None:
        cfg = PostgresConfig()
        max_parameters_per_batch = cfg.max_parameters_per_batch

    cols = max(1, num_columns)
    safe_max_size = max(1, max_parameters_per_batch // cols)

    if explicit_batch_size is not None and explicit_batch_size > 0:
        return min(explicit_batch_size, safe_max_size)

    return safe_max_size


def normalize_row(row: Any, table: Optional[Table] = None) -> dict[str, Any]:
    """Convert an entity (Pydantic model, Mapping, or dict) into a plain dictionary."""
    if hasattr(row, "model_dump"):
        d = row.model_dump(mode="python")
    elif isinstance(row, Mapping):
        d = dict(row)
    elif hasattr(row, "__dict__"):
        d = dict(row.__dict__)
    else:
        d = dict(row)

    if table is not None:
        table_cols = {c.name for c in table.c}
        filtered: dict[str, Any] = {}
        for k, v in d.items():
            if k in table_cols:
                # Omit primary key or server_default column if None so Postgres sequence / default fires
                if (k == "id" or k == "created_at") and v is None:
                    continue
                filtered[k] = bool(v) if isinstance(v, bool) else v
        return filtered

    # If no table supplied, still strip None id / created_at
    return {k: v for k, v in d.items() if not ((k == "id" or k == "created_at") and v is None)}


def _execute_with_bind(bind: Any, fn: Callable[[Any], T]) -> T:
    """Execute callback in a connection transaction if bind is an Engine or ContextManager, or directly if Connection."""
    if isinstance(bind, Connection):
        return fn(bind)
    if isinstance(bind, Engine):
        with bind.begin() as conn:
            return fn(conn)
    if hasattr(bind, "__enter__") and hasattr(bind, "__exit__"):
        with bind as conn:
            return fn(conn)
    return fn(bind)


def upsert_batch(
    bind: Any,
    table: Table,
    rows: Sequence[Any],
    conflict_index_elements: Optional[Sequence[str]] = None,
    update_columns: Optional[Sequence[str]] = None,
    custom_set: Optional[dict[str, Any] | Callable[[Any], dict[str, Any]]] = None,
    exclude_columns: Optional[Sequence[str]] = None,
    return_ids: bool = False,
    id_column_name: str = "id",
    batch_size: Optional[int] = None,
    max_parameters_per_batch: Optional[int] = None,
) -> list[int] | None:
    """Perform bulk upsert into PostgreSQL table using ON CONFLICT DO UPDATE.

    - Dynamically chunks records to prevent bind parameter overflow.
    - Automatically builds update clause unless custom_set or update_columns is provided.
    - Falls back to ON CONFLICT DO NOTHING if update column set is empty.
    - Optionally returns generated/existing IDs via RETURNING.
    """
    if not rows:
        return [] if return_ids else None

    # Normalize rows against table columns
    table_cols = {c.name for c in table.c}
    normalized_rows: list[dict[str, Any]] = [normalize_row(r, table) for r in rows]

    # Inspect present keys
    col_names = set()
    for r in normalized_rows:
        col_names.update(r.keys())
    if not col_names:
        col_names = table_cols - {"id", "created_at"}

    effective_batch_size = calculate_batch_size(
        num_columns=len(col_names),
        max_parameters_per_batch=max_parameters_per_batch,
        explicit_batch_size=batch_size,
    )

    returned_ids: list[int] = []

    def _exec(conn: Any) -> None:
        for i in range(0, len(normalized_rows), effective_batch_size):
            chunk = normalized_rows[i : i + effective_batch_size]
            stmt = pg_insert(table).values(chunk)

            if conflict_index_elements:
                if custom_set is not None:
                    set_clause = custom_set(stmt) if callable(custom_set) else custom_set
                elif update_columns is not None:
                    set_clause = {c: stmt.excluded[c] for c in update_columns if c in table_cols}
                else:
                    conflict_set = set(conflict_index_elements)
                    excl_set = set(exclude_columns or ()) | conflict_set | {"id", "created_at"}
                    set_clause = {c: stmt.excluded[c] for c in col_names if c not in excl_set}

                if set_clause:
                    stmt = stmt.on_conflict_do_update(
                        index_elements=list(conflict_index_elements),
                        set_=set_clause,
                    )
                else:
                    stmt = stmt.on_conflict_do_nothing(index_elements=list(conflict_index_elements))

            if return_ids:
                stmt = stmt.returning(table.c[id_column_name])
                res = conn.execute(stmt).fetchall()
                returned_ids.extend(r[0] for r in res)
            else:
                conn.execute(stmt)

    _execute_with_bind(bind, _exec)
    return returned_ids if return_ids else None


def insert_batch(
    bind: Any,
    table: Table,
    rows: Sequence[Any],
    on_conflict_do_nothing: bool = False,
    conflict_index_elements: Optional[Sequence[str]] = None,
    return_ids: bool = False,
    id_column_name: str = "id",
    batch_size: Optional[int] = None,
    max_parameters_per_batch: Optional[int] = None,
) -> list[int] | None:
    """Perform bulk insert into PostgreSQL table with parameter-safe chunking.

    - Automatically calculates batch size based on table width.
    - Supports optional ON CONFLICT DO NOTHING.
    - Supports RETURNING id for bulk entity persistence.
    """
    if not rows:
        return [] if return_ids else None

    table_cols = {c.name for c in table.c}
    normalized_rows: list[dict[str, Any]] = [normalize_row(r, table) for r in rows]

    col_names = set()
    for r in normalized_rows:
        col_names.update(r.keys())
    if not col_names:
        col_names = table_cols - {"id", "created_at"}

    effective_batch_size = calculate_batch_size(
        num_columns=len(col_names),
        max_parameters_per_batch=max_parameters_per_batch,
        explicit_batch_size=batch_size,
    )

    returned_ids: list[int] = []

    def _exec(conn: Any) -> None:
        for i in range(0, len(normalized_rows), effective_batch_size):
            chunk = normalized_rows[i : i + effective_batch_size]
            stmt = pg_insert(table).values(chunk)

            if on_conflict_do_nothing:
                if conflict_index_elements:
                    stmt = stmt.on_conflict_do_nothing(index_elements=list(conflict_index_elements))
                else:
                    stmt = stmt.on_conflict_do_nothing()

            if return_ids:
                stmt = stmt.returning(table.c[id_column_name])
                res = conn.execute(stmt).fetchall()
                returned_ids.extend(r[0] for r in res)
            else:
                conn.execute(stmt)

    _execute_with_bind(bind, _exec)
    return returned_ids if return_ids else None


__all__ = [
    "calculate_batch_size",
    "normalize_row",
    "upsert_batch",
    "insert_batch",
]
