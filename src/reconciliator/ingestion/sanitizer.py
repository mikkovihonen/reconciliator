"""Schema-driven value truncation and sanitization for all ingestion pipelines and loaders."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Optional

from sqlalchemy import String, Table

from reconciliator.database.schema import (
    agents_table,
    concept_aliases_table,
    concept_hierarchy_table,
    concept_labels_table,
    concept_mapping_labels_table,
    concept_mappings_table,
    concepts_table,
    contributions_table,
    entity_keys_table,
    entity_subjects_table,
    expressions_table,
    get_table_string_limits,
    identifiers_table,
    items_table,
    manifestation_expressions_table,
    manifestations_table,
    relator_terms_table,
    work_relationships_table,
    works_table,
)

if TYPE_CHECKING:
    from reconciliator.models import ConceptMapping, VocabConcept

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
    config: Any,
    schema_limits: dict[str, int],
) -> Optional[int]:
    """Determine effective character limit for a table column from schema and config overrides."""
    overrides = getattr(config, "field_length_overrides", None) or {}

    # 1. Qualified override: table_name.column_name
    qual_name = f"{table.name}.{col_name}"
    if qual_name in overrides:
        return overrides[qual_name]

    # 2. Simple column name override
    if col_name in overrides:
        return overrides[col_name]

    # 3. Dynamic schema-defined limit from schema.py
    if col_name in schema_limits:
        return schema_limits[col_name]

    # 4. Optional global default max length for unconstrained String columns
    default_max = getattr(config, "default_max_field_length", None)
    if default_max is not None and col_name in table.c:
        if isinstance(table.c[col_name].type, String):
            return default_max

    return None


def sanitize_entities(
    entities: list[Any],
    table: Table,
    config: Any,
    task_id: str = "",
    tracker: Optional[Any] = None,
    record_origin: str = "unknown",
) -> int:
    """Sanitize a list of entities or dictionaries against a target SQLAlchemy table definition.

    Returns:
        Number of field truncations performed.
    """
    if not getattr(config, "truncate_overlong_values", True):
        return 0

    marker = getattr(config, "truncation_marker", "") or ""
    schema_limits = get_table_string_limits(table)
    columns_to_check: dict[str, int] = {}

    for col in table.columns:
        limit = _resolve_column_limit(table, col.name, config, schema_limits)
        if limit is not None and limit > 0:
            columns_to_check[col.name] = limit

    if not columns_to_check:
        return 0

    truncations = 0
    for entity in entities:
        is_dict = isinstance(entity, dict)
        source = (
            (entity.get("source") if is_dict else getattr(entity, "source", None))
            or record_origin
        )
        source_id = (
            (entity.get("source_id") if is_dict else getattr(entity, "source_id", None))
            or (entity.get("source_record_id") if is_dict else getattr(entity, "source_record_id", None))
            or (entity.get("qid") if is_dict else getattr(entity, "qid", None))
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

                truncations += 1

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

    return truncations


def sanitize_batch_payload(
    payload: dict[str, Any],
    config: Any,
    task_id: str = "",
    tracker: Optional[Any] = None,
) -> int:
    """Sanitize and truncate overlong string fields in a batch payload based on schema.py definitions.

    Used across OAI-PMH, Corpus Reconciliation loader, and Wikidata Pass 2 batch insertion.

    Returns:
        Total number of field truncations performed across the batch.
    """
    if not getattr(config, "truncate_overlong_values", True):
        return 0

    record_origin = str(payload.get("record_origin") or "unknown")
    total_truncations = 0

    for group_key, table in PAYLOAD_TABLE_MAP.items():
        entities = payload.get(group_key)
        if not entities or not isinstance(entities, list):
            continue
        total_truncations += sanitize_entities(
            entities=entities,
            table=table,
            config=config,
            task_id=task_id,
            tracker=tracker,
            record_origin=record_origin,
        )

    return total_truncations


def sanitize_vocab_concepts(
    concepts: list[VocabConcept],
    config: Any,
    task_id: str = "",
    tracker: Optional[Any] = None,
) -> int:
    """Sanitize VocabConcept objects and their nested pref/alt labels against schema.py tables."""
    if not getattr(config, "truncate_overlong_values", True):
        return 0

    marker = getattr(config, "truncation_marker", "") or ""
    concepts_limits = get_table_string_limits(concepts_table)
    labels_limits = get_table_string_limits(concept_labels_table)

    label_max = _resolve_column_limit(concept_labels_table, "label", config, labels_limits) or 512
    concept_id_max = (
        _resolve_column_limit(concepts_table, "concept_id", config, concepts_limits) or 128
    )
    vocab_max = (
        _resolve_column_limit(concepts_table, "vocabulary", config, concepts_limits) or 64
    )
    target_field_max = (
        _resolve_column_limit(concepts_table, "target_field", config, concepts_limits) or 32
    )

    truncations = 0

    def _truncate_val(table_name: str, col_name: str, val: str, limit: int, source_id: str) -> str:
        nonlocal truncations
        if len(val) <= limit:
            return val
        orig_len = len(val)
        if marker:
            keep_len = max(0, limit - len(marker))
            res = val[:keep_len] + marker
        else:
            res = val[:limit]
        truncations += 1
        logger.error(
            "Task %s: Value too long for %s.%s (source_id=%s, len=%d > max=%d). Truncating value.",
            task_id or "unknown",
            table_name,
            col_name,
            source_id,
            orig_len,
            limit,
        )
        if tracker is not None and hasattr(tracker, "record_error"):
            tracker.record_error(
                message=f"Truncated {table_name}.{col_name} (len {orig_len} > {limit}) for task {task_id}",
                metadata={
                    "last_truncated_field": f"{table_name}.{col_name}",
                    "last_truncated_task_id": task_id,
                },
            )
        return res

    for c in concepts:
        cid = c.concept_id or c.uri
        if c.concept_id and len(c.concept_id) > concept_id_max:
            c.concept_id = _truncate_val("concepts", "concept_id", c.concept_id, concept_id_max, cid)
        if c.vocabulary and len(c.vocabulary) > vocab_max:
            c.vocabulary = _truncate_val("concepts", "vocabulary", c.vocabulary, vocab_max, cid)
        if c.target_field and len(c.target_field) > target_field_max:
            c.target_field = _truncate_val("concepts", "target_field", c.target_field, target_field_max, cid)

        for lang, lbl in list(c.pref_labels.items()):
            if lbl and len(lbl) > label_max:
                c.pref_labels[lang] = _truncate_val(
                    "concept_labels", "label", lbl, label_max, f"{c.vocabulary}:{c.concept_id}"
                )

        for lang, alts in list(c.alt_labels.items()):
            new_alts = []
            for alt in alts:
                if alt and len(alt) > label_max:
                    new_alts.append(
                        _truncate_val(
                            "concept_labels",
                            "label",
                            alt,
                            label_max,
                            f"{c.vocabulary}:{c.concept_id}",
                        )
                    )
                else:
                    new_alts.append(alt)
            c.alt_labels[lang] = new_alts

    return truncations


def sanitize_vocab_mappings(
    mappings: list[ConceptMapping],
    config: Any,
    task_id: str = "",
    tracker: Optional[Any] = None,
) -> int:
    """Sanitize ConceptMapping objects and their nested legacy_labels against schema.py tables."""
    if not getattr(config, "truncate_overlong_values", True):
        return 0

    marker = getattr(config, "truncation_marker", "") or ""
    mapping_limits = get_table_string_limits(concept_mappings_table)
    label_limits = get_table_string_limits(concept_mapping_labels_table)

    label_max = _resolve_column_limit(concept_mapping_labels_table, "label", config, label_limits) or 512
    legacy_id_max = (
        _resolve_column_limit(concept_mappings_table, "legacy_id", config, mapping_limits) or 128
    )
    target_id_max = (
        _resolve_column_limit(concept_mappings_table, "target_id", config, mapping_limits) or 128
    )
    legacy_vocab_max = (
        _resolve_column_limit(concept_mappings_table, "legacy_vocab", config, mapping_limits) or 64
    )
    target_vocab_max = (
        _resolve_column_limit(concept_mappings_table, "target_vocab", config, mapping_limits) or 64
    )
    target_field_max = (
        _resolve_column_limit(concept_mappings_table, "target_field", config, mapping_limits) or 32
    )

    truncations = 0

    def _truncate_val(table_name: str, col_name: str, val: str, limit: int, source_id: str) -> str:
        nonlocal truncations
        if len(val) <= limit:
            return val
        orig_len = len(val)
        if marker:
            keep_len = max(0, limit - len(marker))
            res = val[:keep_len] + marker
        else:
            res = val[:limit]
        truncations += 1
        logger.error(
            "Task %s: Value too long for %s.%s (source_id=%s, len=%d > max=%d). Truncating value.",
            task_id or "unknown",
            table_name,
            col_name,
            source_id,
            orig_len,
            limit,
        )
        if tracker is not None and hasattr(tracker, "record_error"):
            tracker.record_error(
                message=f"Truncated {table_name}.{col_name} (len {orig_len} > {limit}) for task {task_id}",
                metadata={
                    "last_truncated_field": f"{table_name}.{col_name}",
                    "last_truncated_task_id": task_id,
                },
            )
        return res

    for m in mappings:
        mid = f"{m.legacy_vocab}:{m.legacy_id}->{m.target_vocab}:{m.target_id}"
        if m.legacy_id and len(m.legacy_id) > legacy_id_max:
            m.legacy_id = _truncate_val("concept_mappings", "legacy_id", m.legacy_id, legacy_id_max, mid)
        if m.target_id and len(m.target_id) > target_id_max:
            m.target_id = _truncate_val("concept_mappings", "target_id", m.target_id, target_id_max, mid)
        if m.legacy_vocab and len(m.legacy_vocab) > legacy_vocab_max:
            m.legacy_vocab = _truncate_val("concept_mappings", "legacy_vocab", m.legacy_vocab, legacy_vocab_max, mid)
        if m.target_vocab and len(m.target_vocab) > target_vocab_max:
            m.target_vocab = _truncate_val("concept_mappings", "target_vocab", m.target_vocab, target_vocab_max, mid)
        if m.target_field and len(m.target_field) > target_field_max:
            m.target_field = _truncate_val("concept_mappings", "target_field", m.target_field, target_field_max, mid)

        for lang, lbl in list(m.legacy_labels.items()):
            if lbl and len(lbl) > label_max:
                m.legacy_labels[lang] = _truncate_val(
                    "concept_mapping_labels", "label", lbl, label_max, mid
                )

    return truncations

