"""Schema-driven value truncation and sanitization for OAI-PMH and WEMI batch ingestion."""

from __future__ import annotations

import logging
from typing import Any, Optional

from sqlalchemy import String, Table

from reconciliator.config import OAIIngestConfig
from reconciliator.database.schema import (
    agents_table,
    contributions_table,
    entity_keys_table,
    entity_subjects_table,
    expressions_table,
    get_table_string_limits,
    identifiers_table,
    items_table,
    manifestation_expressions_table,
    manifestations_table,
    work_relationships_table,
    works_table,
)

logger = logging.getLogger(__name__)

# Single source of truth mapping between batch payload entity groups and SQLAlchemy tables
PAYLOAD_TABLE_MAP: dict[str, Table] = {
    "works": works_table,
    "expressions": expressions_table,
    "manifestations": manifestations_table,
    "items": items_table,
    "agents": agents_table,
    "relationships": work_relationships_table,
    "identifiers": identifiers_table,
    "associations": manifestation_expressions_table,
    "contributions": contributions_table,
    "entity_keys": entity_keys_table,
    "entity_subjects": entity_subjects_table,
}


def _resolve_column_limit(
    table: Table,
    col_name: str,
    config: OAIIngestConfig,
    schema_limits: dict[str, int],
) -> Optional[int]:
    """Determine effective character limit for a table column from schema and config overrides."""
    # 1. Qualified override: table_name.column_name
    qual_name = f"{table.name}.{col_name}"
    if qual_name in config.field_length_overrides:
        return config.field_length_overrides[qual_name]

    # 2. Simple column name override
    if col_name in config.field_length_overrides:
        return config.field_length_overrides[col_name]

    # 3. Dynamic schema-defined limit from schema.py
    if col_name in schema_limits:
        return schema_limits[col_name]

    # 4. Optional global default max length for unconstrained String columns
    if config.default_max_field_length is not None and col_name in table.c:
        if isinstance(table.c[col_name].type, String):
            return config.default_max_field_length

    return None


def sanitize_batch_payload(
    payload: dict[str, Any],
    config: OAIIngestConfig,
    task_id: str = "",
    tracker: Optional[Any] = None,
) -> int:
    """Sanitize and truncate overlong string fields in a batch payload based on schema.py definitions.

    For each entity whose string value exceeds the schema limit (or config override),
    the value is truncated in-place, an ERROR is logged connecting the event to task_id,
    and tracker.record_error() is notified to surface the count in telemetry.

    Returns:
        Total number of field truncations performed across the batch.
    """
    if not config.truncate_overlong_values:
        return 0

    record_origin = str(payload.get("record_origin") or "unknown")
    marker = config.truncation_marker or ""
    total_truncations = 0

    for group_key, table in PAYLOAD_TABLE_MAP.items():
        entities = payload.get(group_key)
        if not entities or not isinstance(entities, list):
            continue

        schema_limits = get_table_string_limits(table)
        columns_to_check: dict[str, int] = {}

        # Collect columns with effective limits
        for col in table.columns:
            limit = _resolve_column_limit(table, col.name, config, schema_limits)
            if limit is not None and limit > 0:
                columns_to_check[col.name] = limit

        if not columns_to_check:
            continue

        for entity in entities:
            is_dict = isinstance(entity, dict)
            source = (
                (entity.get("source") if is_dict else getattr(entity, "source", None))
                or record_origin
            )
            source_id = (
                (entity.get("source_id") if is_dict else getattr(entity, "source_id", None))
                or (entity.get("source_record_id") if is_dict else getattr(entity, "source_record_id", None))
                or (entity.get("id") if is_dict else getattr(entity, "id", None))
                or "unknown"
            )

            for col_name, limit in columns_to_check.items():
                if is_dict:
                    val = entity.get(col_name)
                else:
                    val = getattr(entity, col_name, None)

                if isinstance(val, str) and len(val) > limit:
                    orig_len = len(val)
                    if marker:
                        keep_len = max(0, limit - len(marker))
                        truncated_val = val[:keep_len] + marker
                    else:
                        truncated_val = val[:limit]

                    if is_dict:
                        entity[col_name] = truncated_val
                    else:
                        setattr(entity, col_name, truncated_val)

                    total_truncations += 1

                    logger.error(
                        "Task %s: Value too long for %s.%s (source=%s, source_id=%s, len=%d > max=%d). Truncating value.",
                        task_id or "unknown",
                        table.name,
                        col_name,
                        source,
                        source_id,
                        orig_len,
                        limit,
                    )

                    if tracker is not None and hasattr(tracker, "record_error"):
                        tracker.record_error(
                            message=f"Truncated {table.name}.{col_name} (len {orig_len} > {limit}) for task {task_id}",
                            metadata={
                                "last_truncated_field": f"{table.name}.{col_name}",
                                "last_truncated_task_id": task_id,
                            },
                        )

    return total_truncations

