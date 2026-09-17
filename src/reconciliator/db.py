"""SQLAlchemy 2.0 Core database management, schema definitions, and WAL pragmas for Reconciliator."""

from __future__ import annotations

import logging
import re
import time
from pathlib import Path
from typing import Any, Optional, Sequence

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Engine,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    create_engine,
    func,
    select,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.pool import QueuePool

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
    Column("source", String, nullable=False),
    Column("source_id", String, nullable=False),
    Column("record_origin", String, nullable=False, default="unknown", server_default="unknown"),
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
    UniqueConstraint("source", "source_id", name="uq_agents_source_source_id"),
    Index("idx_agents_source_id", "source", "source_id"),
    Index("idx_agents_name_norm", "name_normalized"),
    Index("idx_agents_surname", "surname"),
)

works_table = Table(
    "works",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("source", String, nullable=False),
    Column("source_id", String, nullable=False),
    Column("record_origin", String, nullable=False, default="unknown", server_default="unknown"),
    Column("title", String, nullable=False),
    Column("title_normalized", String, nullable=False),
    Column("original_language", String, nullable=True),
    Column(
        "primary_agent_id", Integer, ForeignKey("agents.id", ondelete="SET NULL"), nullable=True
    ),
    Column("ykl_class", String, nullable=True),
    Column("work_type", String, nullable=False, default="text", server_default="text"),
    Column("subjects_json", Text, nullable=False, default="[]", server_default="[]"),
    Column("serialized_text", Text, nullable=True),
    Column(
        "canonical_work_id", Integer, ForeignKey("works.id", ondelete="SET NULL"), nullable=True
    ),
    Column("created_at", DateTime, server_default=func.current_timestamp()),
    UniqueConstraint("source", "source_id", name="uq_works_source_source_id"),
    Index("idx_works_canonical", "canonical_work_id"),
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
    Column("source", String, nullable=False),
    Column("source_id", String, nullable=False),
    Column("record_origin", String, nullable=False, default="unknown", server_default="unknown"),
    Column("language", String, nullable=False),
    Column("content_type", String, nullable=False, default="text", server_default="text"),
    Column("title", String, nullable=True),
    Column("version_designation", String, nullable=True),
    Column("arrangement_note", String, nullable=True),
    Column("medium_of_performance", String, nullable=True),
    Column("serialized_text", Text, nullable=True),
    Column("created_at", DateTime, server_default=func.current_timestamp()),
    UniqueConstraint("source", "source_id", name="uq_expressions_source_source_id"),
    Index("idx_expressions_work_id", "work_id"),
    Index("idx_expressions_source_id", "source", "source_id"),
    Index("idx_expressions_lookup", "work_id", "language", "content_type"),
)

manifestations_table = Table(
    "manifestations",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("work_id", Integer, ForeignKey("works.id", ondelete="SET NULL"), nullable=True),
    Column(
        "container_id", Integer, ForeignKey("manifestations.id", ondelete="CASCADE"), nullable=True
    ),
    Column("is_component", Boolean, nullable=False, default=False, server_default="0"),
    Column("component_type", String, nullable=True),
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
    Column(
        "manifestation_id",
        Integer,
        ForeignKey("manifestations.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "expression_id", Integer, ForeignKey("expressions.id", ondelete="CASCADE"), primary_key=True
    ),
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
    Column(
        "manifestation_id",
        Integer,
        ForeignKey("manifestations.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("source", String, nullable=False),
    Column("source_id", String, nullable=False),
    Column("record_origin", String, nullable=False, default="unknown", server_default="unknown"),
    Column("holding_organization", String, nullable=False),
    Column("shelf_mark", String, nullable=True),
    Column("barcode", String, nullable=True),
    Column("custodial_history", String, nullable=True),
    Column("item_condition", String, nullable=True),
    Column("created_at", DateTime, server_default=func.current_timestamp()),
    UniqueConstraint("source", "source_id", name="uq_items_source_source_id"),
    Index("idx_items_manif_id", "manifestation_id"),
    Index("idx_items_source_id", "source", "source_id"),
    Index("idx_items_barcode", "barcode"),
    Index("idx_items_org", "holding_organization"),
)

identifiers_table = Table(
    "identifiers",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("entity_type", String, nullable=False),
    Column("entity_id", Integer, nullable=False),
    Column("source", String, nullable=False),
    Column("source_id", String, nullable=False),
    Column("source_id_normalized", String, nullable=False),
    CheckConstraint(
        "entity_type IN ('work', 'expression', 'manifestation', 'item', 'agent')",
        name="chk_identifiers_entity_type",
    ),
    Index("idx_identifiers_lookup", "source", "source_id_normalized"),
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
        "entity_type",
        "entity_id",
        "key_type",
        "key_value",
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
        "source_work_id",
        "target_work_id",
        "relationship_type",
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
    Column(
        "manifestation_id",
        Integer,
        ForeignKey("manifestations.id", ondelete="CASCADE"),
        nullable=False,
    ),
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
# 2. Controlled Vocabularies & Subject Link Tables
# ===================================================================

concepts_table = Table(
    "concepts",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("vocabulary", String(64), nullable=False),
    Column("concept_id", String(128), nullable=False),
    Column("is_deprecated", Boolean, nullable=False, default=False, server_default="0"),
    Column("target_field", String(32), nullable=True),
    UniqueConstraint("vocabulary", "concept_id", name="uq_concepts_vocab_id"),
    Index("idx_concepts_vocab", "vocabulary"),
    Index("idx_concepts_lookup", "vocabulary", "concept_id"),
)

concept_labels_table = Table(
    "concept_labels",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column(
        "concept_row_id", Integer, ForeignKey("concepts.id", ondelete="CASCADE"), nullable=False
    ),
    Column("lang", String(16), nullable=False),
    Column("label_type", String(32), nullable=False),
    Column("label", String(512), nullable=False),
    Index("idx_concept_labels_concept_id", "concept_row_id"),
    Index("idx_concept_labels_text", "lang", "label_type", "label"),
)

concept_mappings_table = Table(
    "concept_mappings",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("legacy_vocab", String(64), nullable=False),
    Column("legacy_id", String(128), nullable=False),
    Column("target_vocab", String(64), nullable=False),
    Column("target_id", String(128), nullable=False),
    Column("mapping_type", String(32), nullable=False),
    Column("target_field", String(32), nullable=False),
    Index("idx_mappings_legacy_lookup", "legacy_vocab", "legacy_id"),
    Index("idx_mappings_target_lookup", "target_vocab", "target_id"),
    Index("idx_mappings_vocab", "legacy_vocab"),
)

concept_mapping_labels_table = Table(
    "concept_mapping_labels",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column(
        "mapping_id", Integer, ForeignKey("concept_mappings.id", ondelete="CASCADE"), nullable=False
    ),
    Column("lang", String(16), nullable=False),
    Column("label", String(512), nullable=False),
    Index("idx_concept_mapping_labels_mapping_id", "mapping_id"),
)

relator_terms_table = Table(
    "relator_terms",
    metadata,
    Column("folded_term", String(128), primary_key=True),
    Column("raw_term", String(256), nullable=False),
    Column("lang", String(16), nullable=False),
    Column("code", String(32), nullable=True),
    Column("uri", String(512), nullable=True),
    Index("idx_relators_lang", "lang"),
)

concept_aliases_table = Table(
    "concept_aliases",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("folded_label", String(512), nullable=False),
    Column("raw_label", String(512), nullable=False),
    Column("lang", String(16), nullable=False),
    Column("concept_id", String(512), nullable=True),
    Column("vocabulary", String(64), nullable=False),
    Column("target_field", String(32), nullable=False),
    Column("is_preferred", Integer, nullable=False),
    Column("source", String(64), nullable=False),
    Index("idx_concept_aliases_lookup", "folded_label", "target_field"),
    Index("idx_concept_aliases_concept", "vocabulary", "concept_id"),
)

entity_subjects_table = Table(
    "entity_subjects",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column(
        "entity_type", String(32), nullable=False
    ),  # 'work', 'expression', 'manifestation', 'agent'
    Column("entity_id", Integer, nullable=False),
    Column("source", String(64), nullable=False, default="unknown", server_default="unknown"),
    Column("source_record_id", String(128), nullable=True),
    Column("relation_type", String(32), nullable=False, default="topic", server_default="topic"),
    Column("vocabulary", String(64), nullable=True),
    Column("concept_id", String(128), nullable=True),
    Column("raw_label", String(512), nullable=True),
    Column("is_resolved", Boolean, nullable=False, default=False, server_default="0"),
    Column("is_primary", Boolean, nullable=False, default=False, server_default="0"),
    Column("created_at", DateTime, server_default=func.current_timestamp()),
    CheckConstraint(
        "entity_type IN ('work', 'expression', 'manifestation', 'agent')",
        name="chk_entity_subjects_entity_type",
    ),
    Index("idx_entity_subjects_entity", "entity_type", "entity_id"),
    Index("idx_entity_subjects_concept", "vocabulary", "concept_id"),
    Index("idx_entity_subjects_unresolved", "vocabulary", "is_resolved"),
    Index("idx_entity_subjects_source", "source", "source_record_id"),
)

# ===================================================================
# 3. Telemetry and Progress Tracking Tables (§4.3)
# ===================================================================

task_progress_table = Table(
    "task_progress",
    metadata,
    Column("task_id", String(128), primary_key=True),
    Column("task_type", String(64), nullable=False),
    Column("status", String(32), nullable=False),
    Column("current_stage", String(64), nullable=False),
    Column("items_processed", Integer, default=0, server_default="0"),
    Column("items_total", Integer, nullable=True),
    Column("bytes_processed", Integer, default=0, server_default="0"),
    Column("bytes_total", Integer, nullable=True),
    Column("rate_per_sec", Float, default=0.0, server_default="0.0"),
    Column("percentage", Float, default=0.0, server_default="0.0"),
    Column("start_time", String(64), nullable=False),
    Column("last_heartbeat", String(64), nullable=False),
    Column("error_message", Text, nullable=True),
    Column("metadata_json", Text, nullable=True),
    Index("idx_progress_type", "task_type"),
    Index("idx_progress_status", "status"),
)

# ===================================================================
# 4. Corpus Staging and Clustering Tables (§4.4)
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
    Column("canonical_work_source", String, nullable=True),
    Column("canonical_work_source_id", String, nullable=True),
    Column("canonical_title", String, nullable=False),
    Column("canonical_author", String, nullable=True),
)

corpus_expression_clusters_table = Table(
    "corpus_expression_clusters",
    metadata,
    Column("cluster_id", String, primary_key=True),
    Column(
        "work_cluster_id", String, ForeignKey("corpus_work_clusters.cluster_id"), nullable=False
    ),
    Column("canonical_expression_source", String, nullable=True),
    Column("canonical_expression_source_id", String, nullable=True),
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
    Column(
        "manifestation_cluster_id",
        String,
        ForeignKey("corpus_manifestation_clusters.cluster_id"),
        nullable=True,
    ),
    Column(
        "work_cluster_id", String, ForeignKey("corpus_work_clusters.cluster_id"), nullable=False
    ),
    Column(
        "expression_cluster_id",
        String,
        ForeignKey("corpus_expression_clusters.cluster_id"),
        nullable=False,
    ),
    Column("final_work_source", String, nullable=False, default="", server_default=""),
    Column("final_work_source_id", String, nullable=False, default="", server_default=""),
    Column("final_expression_source", String, nullable=False, default="", server_default=""),
    Column("final_expression_source_id", String, nullable=False, default="", server_default=""),
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
# 3. PostgreSQL Full-Text & Trigram Index Statements
# ===================================================================

PG_SEARCH_DDL_STATEMENTS = [
    "CREATE EXTENSION IF NOT EXISTS pg_trgm;",
    "CREATE EXTENSION IF NOT EXISTS unaccent;",
    "CREATE INDEX IF NOT EXISTS idx_works_title_trgm ON works USING gin (title_normalized gin_trgm_ops);",
    "CREATE INDEX IF NOT EXISTS idx_agents_name_trgm ON agents USING gin (name_normalized gin_trgm_ops);",
    "CREATE INDEX IF NOT EXISTS idx_manifestations_title_trgm ON manifestations USING gin (title_normalized gin_trgm_ops);",
    "CREATE INDEX IF NOT EXISTS idx_works_fts ON works USING gin (to_tsvector('simple', coalesce(serialized_text, '')));",
    "CREATE INDEX IF NOT EXISTS idx_expressions_fts ON expressions USING gin (to_tsvector('simple', coalesce(serialized_text, '')));",
    "CREATE INDEX IF NOT EXISTS idx_manifestations_fts ON manifestations USING gin (to_tsvector('simple', coalesce(serialized_text, '')));",
]


# ===================================================================
# 4. Result & Connection Adapters
# ===================================================================


class RowAdapter:
    """Mapping & index-accessible row adapter for SQLAlchemy Row."""

    __slots__ = ("_row",)

    def __init__(self, row: Any) -> None:
        self._row = row

    @staticmethod
    def _coerce_val(val: Any) -> Any:
        import datetime

        if isinstance(val, (datetime.date, datetime.datetime)):
            return val.isoformat()
        return val

    def __getitem__(self, key: Any) -> Any:
        if isinstance(key, (int, slice)):
            val = self._row[key]
        else:
            val = self._row._mapping[key]
        return self._coerce_val(val)

    def get(self, key: Any, default: Any = None) -> Any:
        val = self._row._mapping.get(key, default)
        return self._coerce_val(val)

    def keys(self) -> Any:
        return self._row._mapping.keys()

    def values(self) -> Any:
        return [self._coerce_val(v) for v in self._row._mapping.values()]

    def items(self) -> Any:
        return [(k, self._coerce_val(v)) for k, v in self._row._mapping.items()]

    def __iter__(self) -> Any:
        return iter(self._row._mapping)

    def __len__(self) -> int:
        return len(self._row)

    def __getattr__(self, name: str) -> Any:
        try:
            return self._coerce_val(getattr(self._row, name))
        except AttributeError:
            if hasattr(self._row, "_mapping") and name in self._row._mapping:
                return self._coerce_val(self._row._mapping[name])
            raise


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

    def scalar(self) -> Any:
        return self._result.scalar()

    def scalar_one(self) -> Any:
        return self._result.scalar_one()

    def scalar_one_or_none(self) -> Any:
        return self._result.scalar_one_or_none()

    def scalars(self) -> Any:
        return self._result.scalars()

    def mappings(self) -> Any:
        return self._result.mappings()

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
        self._is_postgres = True

    def _adapt_sql(self, statement: str) -> str:
        if not self._is_postgres:
            return statement
        stmt = statement.replace("?", "%s")
        if "INSERT OR IGNORE INTO" in stmt:
            stmt = stmt.replace("INSERT OR IGNORE INTO", "INSERT INTO")
            if "ON CONFLICT" not in stmt:
                stmt = stmt.rstrip().rstrip(";") + " ON CONFLICT DO NOTHING;"
        elif "INSERT OR REPLACE INTO" in stmt:
            m = re.search(
                r"INSERT\s+OR\s+REPLACE\s+INTO\s+([a-zA-Z0-9_]+)\s*\(([^\)]+)\)",
                stmt,
                re.IGNORECASE,
            )
            if m:
                tbl = m.group(1).lower()
                cols_str = m.group(2)
                cols_in_insert = [c.strip() for c in cols_str.split(",")]
                stmt = re.sub(
                    r"INSERT\s+OR\s+REPLACE\s+INTO", "INSERT INTO", stmt, flags=re.IGNORECASE
                )
                if "ON CONFLICT" not in stmt:
                    table_obj = metadata.tables.get(tbl)
                    if table_obj is not None and table_obj.primary_key.columns:
                        pk_cols = [c.name for c in table_obj.primary_key.columns]
                    else:
                        pk_cols = ["id"]

                    conflict_target = ", ".join(pk_cols)
                    update_cols = [c for c in cols_in_insert if c not in pk_cols]
                    if update_cols:
                        updates = ", ".join(f"{col} = EXCLUDED.{col}" for col in update_cols)
                        stmt = (
                            stmt.rstrip().rstrip(";")
                            + f" ON CONFLICT ({conflict_target}) DO UPDATE SET {updates};"
                        )
                    else:
                        stmt = (
                            stmt.rstrip().rstrip(";")
                            + f" ON CONFLICT ({conflict_target}) DO NOTHING;"
                        )
        if "GROUP_CONCAT" in stmt.upper():
            stmt = re.sub(
                r"GROUP_CONCAT\s*\(\s*([^,\)]+)\s*,\s*([^)]+)\s*\)",
                r"STRING_AGG(\1, \2)",
                stmt,
                flags=re.IGNORECASE,
            )
            stmt = re.sub(
                r"GROUP_CONCAT\s*\(\s*([^,\)]+)\s*\)",
                r"STRING_AGG(\1, ',')",
                stmt,
                flags=re.IGNORECASE,
            )
        return stmt

    def execute(self, statement: Any, parameters: Any = None) -> ResultAdapter:
        if isinstance(statement, str):
            statement = self._adapt_sql(statement)
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
            statement = self._adapt_sql(statement)
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


DEFAULT_PG_URL = "postgresql+psycopg://postgres:postgres@localhost:5432/reconciliator"


def get_engine(
    db_target: Path | str | None = None,
    in_memory: bool = False,
    pool_size: int = 10,
    max_overflow: int = 20,
    pool_timeout: float = 30.0,
    pool_recycle: int = 1800,
    schema: Optional[str] = None,
) -> Engine:
    """Create a configured SQLAlchemy 2.0 Engine for PostgreSQL."""
    import hashlib
    import os

    if db_target is None:
        target_str = os.environ.get("RECONCILIATOR_DATABASE_URL", DEFAULT_PG_URL)
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
                schema_path = str(p.parent.resolve()) if p.suffix == ".db" else str(p.resolve())
                schema = f"test_{hashlib.md5(schema_path.encode()).hexdigest()[:10]}"
            target_str = os.environ.get("RECONCILIATOR_DATABASE_URL", DEFAULT_PG_URL)

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
        pool_size=pool_size,
        max_overflow=max_overflow,
        pool_timeout=pool_timeout,
        pool_recycle=pool_recycle,
        pool_pre_ping=True,
        connect_args=connect_args,
    )
    return engine


def get_db_connection(db_path: Path | str | None = None) -> ConnectionAdapter:
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
        engine = get_engine()
        return engine, lambda sql: target.execute(sql)
    raise TypeError(f"Unsupported target type for database initialization: {type(target)}")


def create_schema_tables(conn: Any, tables: Sequence[Table]) -> None:
    """Create the given tables in the connection's current_schema if they do not exist."""
    if isinstance(conn, Engine):
        with conn.begin() as c:
            create_schema_tables(c, tables)
        return
    raw_conn = getattr(conn, "_conn", conn)
    existing = set(
        raw_conn.exec_driver_sql(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema();"
        ).scalars()
    )
    for table in tables:
        if table.name not in existing:
            table.create(raw_conn)


def init_authority_db(target: Any) -> None:
    """Initialize relational tables, indexes, and PostgreSQL full-text/trigram indexes."""
    if isinstance(target, Engine):
        with target.begin() as conn:
            create_schema_tables(conn, metadata.sorted_tables)
            for stmt in PG_SEARCH_DDL_STATEMENTS:
                try:
                    with conn.begin_nested():
                        conn.exec_driver_sql(stmt)
                except Exception as e:
                    logger.debug("Execution of DDL statement failed: %s: %s", stmt, e)
        return

    bind, exec_sql = _resolve_bind(target)
    if hasattr(bind, "begin"):
        with bind.begin():
            create_schema_tables(bind, metadata.sorted_tables)
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
    ]
    if isinstance(target, Engine):
        with target.begin() as conn:
            create_schema_tables(conn, tables)
        return

    bind, _ = _resolve_bind(target)
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
        init_authority_db(engine)
        init_corpus_db(engine)
        from reconciliator.progress import init_progress_db
        from reconciliator.vocab_store import init_vocab_db

        init_vocab_db(engine)
        init_progress_db(engine)
    finally:
        engine.dispose()
