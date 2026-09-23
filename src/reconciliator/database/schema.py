"""SQLAlchemy 2.0 Core schema and table definitions for Reconciliator."""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    func,
)

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
    Column("dates", String, nullable=True),
    Column("birth_year", Integer, nullable=True),
    Column("death_year", Integer, nullable=True),
    Column("fuller_name", String, nullable=True),
    Column("is_personal", Boolean, nullable=False, default=True, server_default="1"),
    Column("is_fictional", Boolean, nullable=False, default=False, server_default="0"),
    UniqueConstraint("source", "source_id", name="uq_agents_source_source_id"),
    Index("idx_agents_source_id", "source", "source_id"),
    Index("idx_agents_name_norm", "name_normalized"),
    Index("idx_agents_surname", "surname"),
)

agent_attributes_table = Table(
    "agent_attributes",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("agent_id", Integer, ForeignKey("agents.id", ondelete="CASCADE"), nullable=False),
    Column("attribute_type", String, nullable=False),
    Column("value", String, nullable=False),
    UniqueConstraint("agent_id", "attribute_type", "value", name="uq_agent_attributes"),
    Index("idx_agent_attr_agent", "agent_id", "attribute_type"),
    Index("idx_agent_attr_lookup", "attribute_type", "value"),
)

agent_relationships_table = Table(
    "agent_relationships",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("source_agent_id", Integer, ForeignKey("agents.id", ondelete="CASCADE"), nullable=False),
    Column("target_agent_id", Integer, ForeignKey("agents.id", ondelete="SET NULL"), nullable=True),
    Column("target_name", String, nullable=True),
    Column("target_uri", String, nullable=True),
    Column("relationship_type", String, nullable=False),
    Column("created_at", DateTime, server_default=func.current_timestamp()),
    Index("idx_agent_rel_source", "source_agent_id", "relationship_type"),
    Index("idx_agent_rel_target", "target_agent_id", "relationship_type"),
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
    Column("classification", String, nullable=True),
    Column("classification_scheme", String, nullable=True),
    Column("work_type", String, nullable=False, default="text", server_default="text"),
    Column("serialized_text", Text, nullable=True),
    Column("created_at", DateTime, server_default=func.current_timestamp()),
    UniqueConstraint("source", "source_id", name="uq_works_source_source_id"),
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
    UniqueConstraint(
        "entity_type",
        "entity_id",
        "source",
        "source_id",
        name="uq_identifiers_composite",
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
    UniqueConstraint(
        "entity_type",
        "entity_id",
        "agent_id",
        "role",
        name="uq_contributions_composite",
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

work_consolidations_table = Table(
    "work_consolidations",
    metadata,
    Column("work_id", Integer, ForeignKey("works.id", ondelete="CASCADE"), primary_key=True),
    Column(
        "canonical_work_id", Integer, ForeignKey("works.id", ondelete="CASCADE"), nullable=False
    ),
    Column("evidence", String, nullable=True),
    Column("created_at", DateTime, server_default=func.current_timestamp()),
    Index("idx_work_consolidations_canonical", "canonical_work_id"),
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
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("folded_term", String(128), nullable=False),
    Column("raw_term", String(256), nullable=False),
    Column("lang", String(16), nullable=False),
    Column("code", String(32), nullable=False),
    Column("source_uri", String(512), nullable=True),
    Column("bridged_uri", String(512), nullable=True),
    Column("is_preferred", Boolean, default=True, nullable=False),
    UniqueConstraint("folded_term", "code", "lang", name="uq_relator_folded_code_lang"),
    Index("idx_relators_folded", "folded_term"),
    Index("idx_relators_code", "code"),
    Index("idx_relators_code_lang_pref", "code", "lang", "is_preferred"),
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

concept_hierarchy_table = Table(
    "concept_hierarchy",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("vocabulary", String(64), nullable=False),
    Column("concept_id", String(128), nullable=False),
    Column("broader_concept_id", String(128), nullable=False),
    Column(
        "relation_type", String(32), nullable=False, default="broader", server_default="broader"
    ),
    UniqueConstraint(
        "vocabulary", "concept_id", "broader_concept_id", name="uq_concept_hierarchy_edge"
    ),
    Index("idx_concept_hierarchy_child", "vocabulary", "concept_id"),
    Index("idx_concept_hierarchy_parent", "vocabulary", "broader_concept_id"),
    Index("idx_concept_hierarchy_vocab", "vocabulary"),
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
    UniqueConstraint(
        "entity_type",
        "entity_id",
        "vocabulary",
        "concept_id",
        "raw_label",
        "relation_type",
        name="uq_entity_subjects_composite",
        postgresql_nulls_not_distinct=True,
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
    Column("items_processed", BigInteger, default=0, server_default="0"),
    Column("items_total", BigInteger, nullable=True),
    Column("bytes_processed", BigInteger, default=0, server_default="0"),
    Column("bytes_total", BigInteger, nullable=True),
    Column("rate_per_sec", Float, default=0.0, server_default="0.0"),
    Column("percentage", Float, default=0.0, server_default="0.0"),
    Column("start_time", String(64), nullable=False),
    Column("last_heartbeat", String(64), nullable=False),
    Column("error_message", Text, nullable=True),
    Column("error_count", Integer, default=0, server_default="0"),
    Column("metadata_json", Text, nullable=True),
    Index("idx_progress_type", "task_type"),
    Index("idx_progress_status", "status"),
)

# ===================================================================
# 4. Corpus Staging and Clustering Tables (§4.4)
# ===================================================================

corpus_work_clusters_table = Table(
    "corpus_work_clusters",
    metadata,
    Column("cluster_id", String, primary_key=True),
    Column("work_type", String, nullable=False, default="text", server_default="text"),
    Column("canonical_work_source", String, nullable=True),
    Column("canonical_work_source_id", String, nullable=True),
    Column("canonical_title", String, nullable=False),
    Column("canonical_author", String, nullable=True),
    Column(
        "authority_status",
        String(32),
        nullable=False,
        default="provisional",
        server_default="provisional",
    ),
    Column("canonical_access_point", Text, nullable=True),
    Column("created_at", DateTime, server_default=func.current_timestamp()),
    Column(
        "updated_at",
        DateTime,
        server_default=func.current_timestamp(),
        onupdate=func.current_timestamp(),
    ),
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
    Index("idx_exp_cluster_work", "work_cluster_id"),
)

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

corpus_agent_clusters_table = Table(
    "corpus_agent_clusters",
    metadata,
    Column("cluster_id", String(64), primary_key=True),
    Column(
        "canonical_agent_id", Integer, ForeignKey("agents.id", ondelete="SET NULL"), nullable=True
    ),
    Column("canonical_agent_source", String(32), nullable=True),
    Column("canonical_agent_source_id", Text, nullable=True),
    Column("canonical_name", Text, nullable=False),
    Column("canonical_surname", Text, nullable=True),
    Column("canonical_uri", Text, nullable=True),
    Column("birth_year", Integer, nullable=True),
    Column("death_year", Integer, nullable=True),
    Column("member_count", Integer, nullable=False, default=1, server_default="1"),
    Column("work_count", Integer, nullable=False, default=0, server_default="0"),
    Column("manifestation_count", Integer, nullable=False, default=0, server_default="0"),
    Column("confidence_score", Float, nullable=False, default=1.0, server_default="1.0"),
    Column(
        "authority_status",
        String(32),
        nullable=False,
        default="provisional",
        server_default="provisional",
    ),
    Column("canonical_access_point", Text, nullable=True),
    Column("created_at", DateTime, server_default=func.current_timestamp()),
    Column(
        "updated_at",
        DateTime,
        server_default=func.current_timestamp(),
        onupdate=func.current_timestamp(),
    ),
    Index("idx_agent_cluster_canonical_id", "canonical_agent_id"),
    Index("idx_agent_cluster_canonical_uri", "canonical_uri"),
    Index("idx_agent_cluster_authority_status", "authority_status"),
)

corpus_agent_cluster_members_table = Table(
    "corpus_agent_cluster_members",
    metadata,
    Column("agent_id", Integer, ForeignKey("agents.id", ondelete="CASCADE"), primary_key=True),
    Column(
        "cluster_id",
        String(64),
        ForeignKey("corpus_agent_clusters.cluster_id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("source", String(32), nullable=False),
    Column("source_id", Text, nullable=False),
    Column("link_method", String(64), nullable=False),
    Column("confidence", Float, nullable=False, default=1.0, server_default="1.0"),
    Index("idx_agent_cluster_member_cluster", "cluster_id"),
    Index("idx_agent_cluster_member_source_id", "source", "source_id"),
)

corpus_agent_cluster_attributes_table = Table(
    "corpus_agent_cluster_attributes",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column(
        "cluster_id",
        String(64),
        ForeignKey("corpus_agent_clusters.cluster_id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("attribute_type", String(32), nullable=False),
    Column("value", Text, nullable=False),
    Column("concept_id", Text, nullable=True),
    Column("frequency", Integer, nullable=False, default=1, server_default="1"),
    Column("is_canonical", Boolean, nullable=False, default=False, server_default="0"),
    UniqueConstraint("cluster_id", "attribute_type", "value", name="uq_agent_cluster_attr"),
    Index("idx_agent_cluster_attr_cluster", "cluster_id", "attribute_type"),
)

corpus_agent_relationships_table = Table(
    "corpus_agent_relationships",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column(
        "source_cluster_id",
        String(64),
        ForeignKey("corpus_agent_clusters.cluster_id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column(
        "target_cluster_id",
        String(64),
        ForeignKey("corpus_agent_clusters.cluster_id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("relationship_type", String(64), nullable=False),
    Column("confidence", Float, nullable=False, default=1.0, server_default="1.0"),
    Column("created_at", DateTime, server_default=func.current_timestamp()),
    UniqueConstraint(
        "source_cluster_id",
        "target_cluster_id",
        "relationship_type",
        name="uq_corpus_agent_relationships",
    ),
    Index("idx_corpus_agent_rel_source", "source_cluster_id", "relationship_type"),
    Index("idx_corpus_agent_rel_target", "target_cluster_id", "relationship_type"),
)

cluster_lifecycle_events_table = Table(
    "cluster_lifecycle_events",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("event_type", String(32), nullable=False),  # mint, merge, split, deprecate, reinstate
    Column("cluster_type", String(32), nullable=False),  # agent, work, expression
    Column("cluster_id", String(64), nullable=False),
    Column("target_cluster_id", String(64), nullable=True),
    Column("details_json", Text, nullable=False, default="{}", server_default="{}"),
    Column("created_at", DateTime, server_default=func.current_timestamp()),
    Index("idx_lifecycle_cluster", "cluster_id"),
    Index("idx_lifecycle_type_cluster", "cluster_type", "cluster_id"),
    Index("idx_lifecycle_created", "created_at"),
)

cluster_redirections_table = Table(
    "cluster_redirections",
    metadata,
    Column("source_cluster_id", String(64), primary_key=True),
    Column("target_cluster_id", String(64), nullable=True),  # null if tombstone / 410 Gone
    Column("cluster_type", String(32), nullable=False),
    Column("status_code", Integer, nullable=False, default=308, server_default="308"),
    Column("reason", Text, nullable=False, default="merge", server_default="merge"),
    Column("created_at", DateTime, server_default=func.current_timestamp()),
    Index("idx_redirections_target", "target_cluster_id"),
    Index("idx_redirections_type", "cluster_type"),
)

# ===================================================================
# 5. Curation & Review Changeset Tables (§4.5)
# ===================================================================

review_changesets_table = Table(
    "review_changesets",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("title", String, nullable=False),
    Column("description", Text, nullable=True),
    Column("status", String, nullable=False, default="draft", server_default="draft"),
    Column("created_by", String, nullable=False),
    Column("assigned_to", String, nullable=True),
    Column("committed_by", String, nullable=True),
    Column("committed_at", DateTime, nullable=True),
    Column("created_at", DateTime, server_default=func.current_timestamp()),
    Column("updated_at", DateTime, server_default=func.current_timestamp()),
    Index("idx_review_changesets_status", "status"),
    Index("idx_review_changesets_creator", "created_by"),
)

changeset_operations_table = Table(
    "changeset_operations",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column(
        "changeset_id",
        Integer,
        ForeignKey("review_changesets.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("sequence", Integer, nullable=False, default=1, server_default="1"),
    Column("operation_type", String, nullable=False),
    Column("entity_type", String, nullable=False),
    Column("entity_id", String, nullable=False),
    Column("target_id", String, nullable=True),
    Column("parameters_json", Text, nullable=False, default="{}", server_default="{}"),
    Column("pre_image_json", Text, nullable=True),
    Column("notes", Text, nullable=True),
    Column("created_at", DateTime, server_default=func.current_timestamp()),
    Index("idx_changeset_ops_changeset", "changeset_id", "sequence"),
    Index("idx_changeset_ops_entity", "entity_type", "entity_id"),
)

entity_pins_table = Table(
    "entity_pins",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("entity_type", String, nullable=False),
    Column("entity_id", String, nullable=False),
    Column(
        "changeset_id",
        Integer,
        ForeignKey("review_changesets.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    Column("pinned_at", DateTime, server_default=func.current_timestamp()),
    Column("pinned_by", String, nullable=False),
    UniqueConstraint("entity_type", "entity_id", name="uq_entity_pins_composite"),
    Index("idx_entity_pins_lookup", "entity_type", "entity_id"),
    Index("idx_entity_pins_changeset", "changeset_id"),
)

# ===================================================================
# 6. PostgreSQL Full-Text & Trigram Index Statements
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


def get_table_string_limits(table: Table) -> dict[str, int]:
    """Introspect explicit character limits for String columns directly from table schema definition."""
    return {
        col.name: col.type.length
        for col in table.columns
        if isinstance(col.type, String) and col.type.length is not None
    }


__all__ = [
    "metadata",
    "get_table_string_limits",
    "db_metadata_table",
    "sources_table",
    "agents_table",
    "agent_attributes_table",
    "agent_relationships_table",
    "works_table",
    "expressions_table",
    "manifestations_table",
    "manifestation_expressions_table",
    "items_table",
    "identifiers_table",
    "entity_keys_table",
    "contributions_table",
    "work_relationships_table",
    "work_consolidations_table",
    "work_vectors_table",
    "manifestation_vectors_table",
    "harvest_state_table",
    "concepts_table",
    "concept_labels_table",
    "concept_mappings_table",
    "concept_mapping_labels_table",
    "relator_terms_table",
    "concept_aliases_table",
    "entity_subjects_table",
    "task_progress_table",
    "corpus_work_clusters_table",
    "corpus_expression_clusters_table",
    "corpus_manifestation_clusters_table",
    "corpus_linkages_table",
    "sammelband_bindings_table",
    "corpus_agent_clusters_table",
    "corpus_agent_cluster_members_table",
    "corpus_agent_cluster_attributes_table",
    "corpus_agent_relationships_table",
    "review_changesets_table",
    "changeset_operations_table",
    "entity_pins_table",
    "PG_SEARCH_DDL_STATEMENTS",
]
