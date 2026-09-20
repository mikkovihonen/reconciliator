"""Unit tests for schema-driven value sanitization and overlong field truncation."""

import logging
from unittest.mock import MagicMock

import pytest
from sqlalchemy import Table

from reconciliator.config import OAIIngestConfig
from reconciliator.database.schema import (
    entity_subjects_table,
    get_table_string_limits,
    works_table,
)
from reconciliator.ingestion.sanitizer import (
    PAYLOAD_TABLE_MAP,
    _resolve_column_limit,
    sanitize_batch_payload,
)
from reconciliator.models import EntitySubject, WorkEntity


def test_schema_limits_dynamically_derived() -> None:
    """Verify that field limits are derived dynamically from schema.py without hardcoded duplicates."""
    limits = get_table_string_limits(entity_subjects_table)

    # Values must match the Column definitions in schema.py
    assert limits["raw_label"] == 512
    assert limits["concept_id"] == 128
    assert limits["source_record_id"] == 128
    assert limits["vocabulary"] == 64
    assert limits["relation_type"] == 32
    assert limits["entity_type"] == 32

    # Verify all payload tables in PAYLOAD_TABLE_MAP are actual Table instances from schema.py
    for group, table in PAYLOAD_TABLE_MAP.items():
        assert isinstance(table, Table)


def test_resolve_column_limit_priority() -> None:
    """Verify limit resolution priority: qualified override > field override > schema limit > default_max."""
    config = OAIIngestConfig(
        default_max_field_length=300,
        field_length_overrides={
            "entity_subjects.raw_label": 200,
            "concept_id": 50,
        },
    )
    schema_limits = get_table_string_limits(entity_subjects_table)

    # Qualified override wins
    assert _resolve_column_limit(entity_subjects_table, "raw_label", config, schema_limits) == 200
    # Simple column override wins over schema
    assert _resolve_column_limit(entity_subjects_table, "concept_id", config, schema_limits) == 50
    # Unoverridden schema limit falls back to schema.py definition (64)
    assert _resolve_column_limit(entity_subjects_table, "vocabulary", config, schema_limits) == 64
    # Unconstrained String column on works_table falls back to default_max_field_length
    works_schema_limits = get_table_string_limits(works_table)
    assert _resolve_column_limit(works_table, "title", config, works_schema_limits) == 300


def test_sanitize_entity_subject_overlong_label(caplog: pytest.LogCaptureFixture) -> None:
    """Verify overlong field is truncated, error is logged with task_id, and tracker is notified."""
    long_label = "X" * 700
    subject = EntitySubject(
        entity_type="work",
        entity_id=1,
        source="helka",
        source_record_id="rec-999",
        vocabulary="yso",
        concept_id="p1234",
        raw_label=long_label,
    )

    payload = {
        "entity_subjects": [subject],
        "record_origin": "helka",
    }
    config = OAIIngestConfig(truncate_overlong_values=True)
    tracker = MagicMock()

    with caplog.at_level(logging.ERROR):
        count = sanitize_batch_payload(
            payload, config=config, task_id="oai-harvest-task-42", tracker=tracker
        )

    assert count == 1
    assert len(subject.raw_label) == 512
    assert subject.raw_label == "X" * 512

    # Check logger error with task_id and centralized prefix
    assert "[sanitization] truncate entity_subjects.raw_label" in caplog.text
    assert "len=700 > max=512" in caplog.text
    assert any(getattr(r, "task_id", None) == "oai-harvest-task-42" for r in caplog.records)

    # Check tracker was called
    tracker.record_error.assert_called_once()
    call_kwargs = tracker.record_error.call_args[1]
    assert "metadata" in call_kwargs
    assert call_kwargs["metadata"]["last_truncated_task_id"] == "oai-harvest-task-42"


def test_sanitize_with_truncation_marker() -> None:
    """Verify that truncation marker is correctly preserved within the length limit."""
    long_label = "Y" * 600
    subject = EntitySubject(
        entity_type="work",
        entity_id=1,
        source="helka",
        vocabulary="yso",
        concept_id="p1234",
        raw_label=long_label,
    )

    payload = {"entity_subjects": [subject]}
    config = OAIIngestConfig(
        truncate_overlong_values=True,
        truncation_marker="[TRUNCATED]",
    )

    count = sanitize_batch_payload(payload, config=config, task_id="task-marker")
    assert count == 1
    assert len(subject.raw_label) == 512
    assert subject.raw_label.endswith("[TRUNCATED]")
    assert subject.raw_label.startswith("Y" * (512 - len("[TRUNCATED]")))


def test_sanitize_disabled_does_not_mutate() -> None:
    """Verify that when truncate_overlong_values is False, fields are untouched."""
    long_label = "Z" * 800
    subject = EntitySubject(
        entity_type="work",
        entity_id=1,
        source="helka",
        vocabulary="yso",
        concept_id="p1234",
        raw_label=long_label,
    )

    payload = {"entity_subjects": [subject]}
    config = OAIIngestConfig(truncate_overlong_values=False)

    count = sanitize_batch_payload(payload, config=config, task_id="task-disabled")
    assert count == 0
    assert len(subject.raw_label) == 800
    assert subject.raw_label == long_label


def test_sanitize_dict_entity() -> None:
    """Verify sanitizer handles plain dictionaries in payload."""
    long_label = "D" * 600
    payload = {
        "entity_subjects": [
            {
                "entity_type": "work",
                "entity_id": 1,
                "source": "helka",
                "source_record_id": "r-1",
                "raw_label": long_label,
            }
        ]
    }
    config = OAIIngestConfig(truncate_overlong_values=True)
    count = sanitize_batch_payload(payload, config=config, task_id="task-dict")

    assert count == 1
    assert len(payload["entity_subjects"][0]["raw_label"]) == 512

