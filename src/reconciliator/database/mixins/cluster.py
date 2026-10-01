"""Cluster persistence, retrieval, and lifecycle repository mixin."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from sqlalchemy import and_, func, insert, nulls_last, select, update

from reconciliator.database.ops import upsert_batch
from reconciliator.database.schema import (
    cluster_lifecycle_events_table,
    cluster_redirections_table,
    corpus_agent_clusters_table,
    corpus_constituent_linkages_table,
    corpus_expression_clusters_table,
    corpus_linkages_table,
    corpus_manifestation_clusters_table,
    corpus_work_clusters_table,
    corpus_work_relationships_table,
    entity_keys_table,
    manifestations_table,
    sammelband_bindings_table,
)
from reconciliator.models import (
    ClusterLifecycleEvent,
    ClusterRedirection,
    CorpusConstituentLinkage,
    CorpusLinkage,
    CorpusWorkRelationship,
    ExpressionCluster,
    ManifestationCluster,
    SammelbandBinding,
    WorkCluster,
)


class ClusterRepositoryMixin:
    """Repository mixin for WEMI cluster persistence, retrieval, linkages, and lifecycle."""

    def get_connection(self) -> Any:
        raise NotImplementedError

    def get_corpus_connection(self) -> Any:
        raise NotImplementedError

    def insert_manifestation_clusters_batch(self, clusters: list[ManifestationCluster]) -> None:
        """Batch insert or replace carrier duplicate clusters."""
        if not clusters:
            return
        deduped = {
            c.cluster_id: {
                "cluster_id": c.cluster_id,
                "canonical_record_id": c.canonical_record_id,
                "publisher": c.publisher,
                "publication_year": c.publication_year,
                "edition_statement": c.edition_statement,
                "extent": c.extent,
                "carrier_type": c.carrier_type,
                "member_record_count": c.member_record_count,
            }
            for c in clusters
        }
        rows = list(deduped.values())
        upsert_batch(
            bind=self.get_corpus_connection(),
            table=corpus_manifestation_clusters_table,
            rows=rows,
            conflict_index_elements=["cluster_id"],
        )

    def insert_work_clusters_batch(self, clusters: list[WorkCluster]) -> None:
        """Batch insert or replace work family clusters."""
        if not clusters:
            return
        deduped = {
            c.cluster_id: {
                "cluster_id": c.cluster_id,
                "work_type": c.work_type,
                "canonical_work_source": c.canonical_work_source,
                "canonical_work_source_id": c.canonical_work_source_id,
                "canonical_title": c.canonical_title,
                "canonical_author": c.canonical_author,
            }
            for c in clusters
        }
        upsert_batch(
            bind=self.get_corpus_connection(),
            table=corpus_work_clusters_table,
            rows=list(deduped.values()),
            conflict_index_elements=["cluster_id"],
        )

    def insert_expression_clusters_batch(self, clusters: list[ExpressionCluster]) -> None:
        """Batch insert or replace expression realization clusters."""
        if not clusters:
            return
        deduped = {
            c.cluster_id: {
                "cluster_id": c.cluster_id,
                "work_cluster_id": c.work_cluster_id,
                "canonical_expression_source": c.canonical_expression_source,
                "canonical_expression_source_id": c.canonical_expression_source_id,
                "content_type": c.content_type,
                "language": c.language,
                "version_designation": c.version_designation,
            }
            for c in clusters
        }
        upsert_batch(
            bind=self.get_corpus_connection(),
            table=corpus_expression_clusters_table,
            rows=list(deduped.values()),
            conflict_index_elements=["cluster_id"],
        )

    def insert_corpus_linkages_batch(self, linkages: list[CorpusLinkage]) -> None:
        """Batch insert or replace final record-to-cluster linkages."""
        if not linkages:
            return
        deduped = {
            lnk.record_id: {
                "record_id": lnk.record_id,
                "manifestation_cluster_id": lnk.manifestation_cluster_id,
                "work_cluster_id": lnk.work_cluster_id,
                "expression_cluster_id": lnk.expression_cluster_id,
                "final_work_source": lnk.final_work_source,
                "final_work_source_id": lnk.final_work_source_id,
                "final_expression_source": lnk.final_expression_source,
                "final_expression_source_id": lnk.final_expression_source_id,
            }
            for lnk in linkages
        }
        upsert_batch(
            bind=self.get_corpus_connection(),
            table=corpus_linkages_table,
            rows=list(deduped.values()),
            conflict_index_elements=["record_id"],
        )

    def insert_corpus_constituent_linkages_batch(
        self, linkages: list[CorpusConstituentLinkage]
    ) -> None:
        """Batch insert or replace constituent record-to-cluster linkages."""
        if not linkages:
            return
        deduped = {
            (lnk.record_id, lnk.constituent_ordinal): {
                "record_id": lnk.record_id,
                "constituent_ordinal": lnk.constituent_ordinal,
                "constituent_work_id": lnk.constituent_work_id,
                "constituent_work_cluster_id": lnk.constituent_work_cluster_id,
                "constituent_expression_cluster_id": lnk.constituent_expression_cluster_id,
                "title": lnk.title,
                "creator": lnk.creator,
                "field_tag": lnk.field_tag,
                "final_work_source": lnk.final_work_source,
                "final_work_source_id": lnk.final_work_source_id,
                "final_work_uri": lnk.final_work_uri,
                "confidence": lnk.confidence,
            }
            for lnk in linkages
        }
        upsert_batch(
            bind=self.get_corpus_connection(),
            table=corpus_constituent_linkages_table,
            rows=list(deduped.values()),
            conflict_index_elements=["record_id", "constituent_ordinal"],
        )

    def insert_corpus_work_relationships_batch(
        self, relationships: list[CorpusWorkRelationship]
    ) -> None:
        """Batch insert or replace whole-part relationships between discovered work clusters."""
        if not relationships:
            return
        deduped = {
            (rel.source_cluster_id, rel.target_cluster_id, rel.relationship_type): {
                "source_cluster_id": rel.source_cluster_id,
                "target_cluster_id": rel.target_cluster_id,
                "relationship_type": rel.relationship_type,
                "sequence_number": rel.sequence_number,
                "confidence": rel.confidence,
            }
            for rel in relationships
        }
        upsert_batch(
            bind=self.get_corpus_connection(),
            table=corpus_work_relationships_table,
            rows=list(deduped.values()),
            conflict_index_elements=[
                "source_cluster_id",
                "target_cluster_id",
                "relationship_type",
            ],
        )

    def insert_sammelband_bindings_batch(self, bindings: list[SammelbandBinding]) -> None:
        """Batch insert or replace sammelband item-level composite bindings."""
        if not bindings:
            return
        deduped = {
            (b.binding_id, b.record_id): {
                "binding_id": b.binding_id,
                "record_id": b.record_id,
                "sequence_in_binding": b.sequence_in_binding,
                "binding_notes": b.binding_notes,
            }
            for b in bindings
        }
        upsert_batch(
            bind=self.get_corpus_connection(),
            table=sammelband_bindings_table,
            rows=list(deduped.values()),
            conflict_index_elements=["binding_id", "record_id"],
        )

    def find_records_by_key(self, key_type: str, key_value: str) -> list[str]:
        """Find record ids (manifestation source_ids) carrying a key."""
        stmt = (
            select(manifestations_table.c.source_id)
            .select_from(
                entity_keys_table.join(
                    manifestations_table,
                    manifestations_table.c.id == entity_keys_table.c.entity_id,
                )
            )
            .where(
                entity_keys_table.c.entity_type == "manifestation",
                entity_keys_table.c.key_type == key_type,
                entity_keys_table.c.key_value == key_value,
            )
        )
        with self.get_connection() as conn:
            rows = conn.execute(stmt).fetchall()
            return [str(r[0]) for r in rows if r[0] is not None]

    def get_blocking_groups(
        self, key_types: Optional[list[str]] = None
    ) -> dict[tuple[str, str], list[str]]:
        """Find blocking key collision groups in SQL, over `entity_keys`."""
        cond = entity_keys_table.c.entity_type == "manifestation"
        if key_types:
            cond = and_(cond, entity_keys_table.c.key_type.in_(key_types))

        agg_col = func.string_agg(manifestations_table.c.source_id, ",").label("agg_ids")
        stmt = (
            select(
                entity_keys_table.c.key_type,
                entity_keys_table.c.key_value,
                agg_col,
            )
            .select_from(
                entity_keys_table.join(
                    manifestations_table,
                    manifestations_table.c.id == entity_keys_table.c.entity_id,
                )
            )
            .where(cond)
            .group_by(entity_keys_table.c.key_type, entity_keys_table.c.key_value)
            .having(func.count() > 1)
        )
        with self.get_connection() as conn:
            rows = conn.execute(stmt).fetchall()
            return {(str(r[0]), str(r[1])): str(r[2]).split(",") for r in rows}

    def get_all_work_clusters(self) -> list[WorkCluster]:
        """Fetch all work clusters."""
        stmt = select(corpus_work_clusters_table).order_by(corpus_work_clusters_table.c.cluster_id)
        with self.get_corpus_connection() as conn:
            rows = conn.execute(stmt).mappings().fetchall()
            return [WorkCluster.model_validate(dict(r)) for r in rows]

    def get_expression_clusters_for_work(self, work_cluster_id: str) -> list[ExpressionCluster]:
        """Fetch all expression clusters under a work cluster."""
        stmt = (
            select(corpus_expression_clusters_table)
            .where(corpus_expression_clusters_table.c.work_cluster_id == work_cluster_id)
            .order_by(corpus_expression_clusters_table.c.cluster_id)
        )
        with self.get_corpus_connection() as conn:
            rows = conn.execute(stmt).mappings().fetchall()
            return [ExpressionCluster.model_validate(dict(r)) for r in rows]

    def get_all_expression_clusters(self) -> list[ExpressionCluster]:
        """Fetch all expression clusters."""
        stmt = select(corpus_expression_clusters_table).order_by(
            corpus_expression_clusters_table.c.cluster_id
        )
        with self.get_corpus_connection() as conn:
            rows = conn.execute(stmt).mappings().fetchall()
            return [ExpressionCluster.model_validate(dict(r)) for r in rows]

    def get_sibling_manifestation_record_ids(self, record_id: str) -> list[str]:
        """Return record IDs that share the same manifestation cluster, excluding the given record."""
        sub = (
            select(corpus_linkages_table.c.manifestation_cluster_id)
            .where(corpus_linkages_table.c.record_id == record_id)
            .scalar_subquery()
        )
        stmt = select(corpus_linkages_table.c.record_id).where(
            corpus_linkages_table.c.manifestation_cluster_id == sub,
            corpus_linkages_table.c.record_id != record_id,
        )
        with self.get_corpus_connection() as conn:
            return [str(r[0]) for r in conn.execute(stmt)]

    def get_all_manifestation_clusters(self) -> list[ManifestationCluster]:
        """Fetch all manifestation clusters."""
        stmt = select(corpus_manifestation_clusters_table).order_by(
            corpus_manifestation_clusters_table.c.cluster_id
        )
        with self.get_corpus_connection() as conn:
            rows = conn.execute(stmt).mappings().fetchall()
            return [ManifestationCluster.model_validate(dict(r)) for r in rows]

    def get_corpus_linkage(self, record_id: str) -> Optional[CorpusLinkage]:
        """Fetch linkage record for a staged target record."""
        stmt = select(corpus_linkages_table).where(corpus_linkages_table.c.record_id == record_id)
        with self.get_corpus_connection() as conn:
            row = conn.execute(stmt).mappings().fetchone()
            return CorpusLinkage.model_validate(dict(row)) if row else None

    def get_all_corpus_linkages(self) -> list[CorpusLinkage]:
        """Fetch all corpus linkage records."""
        stmt = select(corpus_linkages_table).order_by(corpus_linkages_table.c.record_id)
        with self.get_corpus_connection() as conn:
            rows = conn.execute(stmt).mappings().fetchall()
            return [CorpusLinkage.model_validate(dict(r)) for r in rows]

    def get_constituent_linkages_for_record(self, record_id: str) -> list[CorpusConstituentLinkage]:
        """Fetch all constituent linkages for a container record, ordered by constituent ordinal."""
        stmt = (
            select(corpus_constituent_linkages_table)
            .where(corpus_constituent_linkages_table.c.record_id == record_id)
            .order_by(corpus_constituent_linkages_table.c.constituent_ordinal)
        )
        with self.get_corpus_connection() as conn:
            rows = conn.execute(stmt).mappings().fetchall()
            return [CorpusConstituentLinkage.model_validate(dict(r)) for r in rows]

    def get_all_constituent_linkages(self) -> list[CorpusConstituentLinkage]:
        """Fetch all constituent linkages ordered by record_id and ordinal."""
        stmt = select(corpus_constituent_linkages_table).order_by(
            corpus_constituent_linkages_table.c.record_id,
            corpus_constituent_linkages_table.c.constituent_ordinal,
        )
        with self.get_corpus_connection() as conn:
            rows = conn.execute(stmt).mappings().fetchall()
            return [CorpusConstituentLinkage.model_validate(dict(r)) for r in rows]

    def get_constituent_clusters_for_container(
        self, container_cluster_id: str
    ) -> list[WorkCluster]:
        """Fetch all constituent work clusters for an aggregate container work cluster, ordered by sequence."""
        stmt = (
            select(corpus_work_clusters_table)
            .select_from(
                corpus_work_relationships_table.join(
                    corpus_work_clusters_table,
                    corpus_work_clusters_table.c.cluster_id
                    == corpus_work_relationships_table.c.target_cluster_id,
                )
            )
            .where(
                corpus_work_relationships_table.c.source_cluster_id == container_cluster_id,
                corpus_work_relationships_table.c.relationship_type == "has_part",
            )
            .order_by(
                nulls_last(corpus_work_relationships_table.c.sequence_number),
                corpus_work_clusters_table.c.cluster_id,
            )
        )
        with self.get_corpus_connection() as conn:
            rows = conn.execute(stmt).mappings().fetchall()
            return [WorkCluster.model_validate(dict(r)) for r in rows]

    def get_constituent_work_relationships_for_container(
        self, container_cluster_id: str
    ) -> list[CorpusWorkRelationship]:
        """Fetch all outgoing work relationships (e.g. has_part) for a container cluster."""
        stmt = (
            select(corpus_work_relationships_table)
            .where(
                corpus_work_relationships_table.c.source_cluster_id == container_cluster_id,
                corpus_work_relationships_table.c.relationship_type == "has_part",
            )
            .order_by(
                nulls_last(corpus_work_relationships_table.c.sequence_number),
                corpus_work_relationships_table.c.target_cluster_id,
            )
        )
        with self.get_corpus_connection() as conn:
            rows = conn.execute(stmt).mappings().fetchall()
            return [CorpusWorkRelationship.model_validate(dict(r)) for r in rows]

    def get_sammelband_bindings(self, binding_id: str) -> list[SammelbandBinding]:
        """Fetch all constituent items in a composite binding."""
        stmt = (
            select(sammelband_bindings_table)
            .where(sammelband_bindings_table.c.binding_id == binding_id)
            .order_by(sammelband_bindings_table.c.sequence_in_binding)
        )
        with self.get_corpus_connection() as conn:
            rows = conn.execute(stmt).mappings().fetchall()
            return [SammelbandBinding.model_validate(dict(r)) for r in rows]

    def insert_cluster_lifecycle_event(self, event: ClusterLifecycleEvent) -> None:
        """Insert an individual authority cluster lifecycle event."""
        self.insert_cluster_lifecycle_events_batch([event])

    def insert_cluster_lifecycle_events_batch(self, events: list[ClusterLifecycleEvent]) -> None:
        """Batch insert authority cluster lifecycle events."""
        if not events:
            return
        dicts = [
            {
                "event_type": e.event_type,
                "cluster_type": e.cluster_type,
                "cluster_id": e.cluster_id,
                "target_cluster_id": e.target_cluster_id,
                "details_json": e.details_json,
            }
            for e in events
        ]
        with self.get_corpus_connection() as conn:
            conn.execute(insert(cluster_lifecycle_events_table), dicts)
            conn.commit()

    def insert_cluster_redirections_batch(self, redirections: list[ClusterRedirection]) -> None:
        """Batch upsert authority cluster redirections."""
        if not redirections:
            return
        dicts = [
            {
                "source_cluster_id": r.source_cluster_id,
                "target_cluster_id": r.target_cluster_id,
                "cluster_type": r.cluster_type,
                "status_code": r.status_code,
                "reason": r.reason,
            }
            for r in redirections
        ]
        with self.get_corpus_connection() as conn:
            for d in dicts:
                stmt = select(cluster_redirections_table.c.source_cluster_id).where(
                    cluster_redirections_table.c.source_cluster_id == d["source_cluster_id"]
                )
                existing = conn.execute(stmt).first()
                if existing:
                    conn.execute(
                        update(cluster_redirections_table)
                        .where(
                            cluster_redirections_table.c.source_cluster_id == d["source_cluster_id"]
                        )
                        .values(
                            target_cluster_id=d["target_cluster_id"],
                            cluster_type=d["cluster_type"],
                            status_code=d["status_code"],
                            reason=d["reason"],
                        )
                    )
                else:
                    conn.execute(insert(cluster_redirections_table), [d])
            conn.commit()

    def get_cluster_redirection(self, source_cluster_id: str) -> Optional[ClusterRedirection]:
        """Fetch redirection target for a merged or tombstoned authority cluster."""
        with self.get_corpus_connection() as conn:
            stmt = select(cluster_redirections_table).where(
                cluster_redirections_table.c.source_cluster_id == source_cluster_id
            )
            row = conn.execute(stmt).mappings().first()
            if not row:
                return None
            return ClusterRedirection.model_validate(dict(row))

    def get_cluster_lifecycle_events(self, cluster_id: str) -> list[ClusterLifecycleEvent]:
        """Fetch all lifecycle events for a cluster ordered by timestamp."""
        with self.get_corpus_connection() as conn:
            stmt = (
                select(cluster_lifecycle_events_table)
                .where(cluster_lifecycle_events_table.c.cluster_id == cluster_id)
                .order_by(cluster_lifecycle_events_table.c.created_at.asc())
            )
            rows = conn.execute(stmt).mappings().fetchall()
            return [ClusterLifecycleEvent.model_validate(dict(r)) for r in rows]

    def get_work_cluster_full(self, cluster_id: str) -> Optional[WorkCluster]:
        """Fetch work cluster by cluster_id."""
        with self.get_corpus_connection() as conn:
            stmt = select(corpus_work_clusters_table).where(
                corpus_work_clusters_table.c.cluster_id == cluster_id
            )
            row = conn.execute(stmt).mappings().first()
            if not row:
                return None
            return WorkCluster.model_validate(dict(row))

    def list_authority_clusters_modified_since(
        self,
        cluster_type: str,
        since: Optional[datetime] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        """Fetch modified authority clusters since a given timestamp for OAI-PMH harvesting."""
        table = (
            corpus_agent_clusters_table if cluster_type == "agent" else corpus_work_clusters_table
        )
        with self.get_corpus_connection() as conn:
            stmt = select(table)
            count_stmt = select(func.count()).select_from(table)
            if since is not None:
                stmt = stmt.where(table.c.created_at >= since)
                count_stmt = count_stmt.where(table.c.created_at >= since)
            stmt = stmt.order_by(table.c.created_at.asc()).limit(limit).offset(offset)
            total = conn.execute(count_stmt).scalar() or 0
            rows = conn.execute(stmt).mappings().fetchall()
            return [dict(r) for r in rows], int(total)
