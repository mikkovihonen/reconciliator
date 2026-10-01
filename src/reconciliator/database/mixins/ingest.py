"""Batch WEMI ingestion, entity graph linking, and vector index update mixin."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Optional

import numpy as np
from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert as pg_insert

from reconciliator.database.ops import calculate_batch_size, insert_batch
from reconciliator.database.schema import (
    contributions_table,
    entity_keys_table,
    entity_subjects_table,
    expressions_table,
    identifiers_table,
    items_table,
    manifestation_expressions_table,
    manifestation_vectors_table,
    manifestations_table,
    work_relationships_table,
    work_vectors_table,
    works_table,
)
from reconciliator.models import (
    AgentEntity,
    AgentRelationshipEntity,
    ContributionEntity,
    EntityKey,
    EntitySubject,
    ExpressionEntity,
    IdentifierEntity,
    ItemEntity,
    ManifestationEntity,
    ManifestationExpressionAssociation,
    RecordOrigin,
    WorkEntity,
    WorkRelationship,
)
from reconciliator.utils.curie import parse_curie, to_curie

if TYPE_CHECKING:
    from reconciliator.vector_indices import VectorIndex

logger = logging.getLogger(__name__)


class WEMIIngestionRepositoryMixin:
    """Repository mixin managing high-throughput batched ingestion of WEMI entities and vectors."""

    work_indexer: Optional[VectorIndex]
    manifestation_indexer: Optional[VectorIndex]
    _work_cache: dict[int, WorkEntity]
    _manifestation_cache: dict[int, ManifestationEntity]
    _expressions_for_work_cache: dict[int, list[ExpressionEntity]]
    _identifier_lookup_cache: dict[Any, Any]

    def get_connection(self) -> Any:
        raise NotImplementedError

    def clear_entity_caches(self) -> None:
        raise NotImplementedError
    def insert_works_batch(
        self,
        works: list[WorkEntity],
        vectors: Optional[np.ndarray] = None,
        conn: Optional[Any] = None,
        chunk_size: Optional[int] = None,
    ) -> list[int]:
        """Insert or update WorkEntity records and optional dense vectors in batch."""
        if not works:
            return []

        deduped_rows: dict[tuple[str, str], dict[str, Any]] = {}
        for work in works:
            key = (work.source, work.source_id)
            row = {
                "source": work.source,
                "source_id": work.source_id,
                "record_origin": work.record_origin,
                "title": work.title,
                "title_normalized": work.title_normalized,
                "original_language": work.original_language,
                "primary_agent_id": work.primary_agent_id,
                "classification": work.classification,
                "classification_scheme": work.classification_scheme,
                "work_type": work.work_type,
                "serialized_text": work.serialized_text,
            }
            if key in deduped_rows:
                existing = deduped_rows[key]
                for field, val in row.items():
                    if val is not None:
                        existing[field] = val
            else:
                deduped_rows[key] = row

        rows = list(deduped_rows.values())
        id_map: dict[tuple[str, str], int] = {}

        def _exec(c: Any) -> None:
            effective_chunk_size = chunk_size or calculate_batch_size(
                num_columns=len(works_table.c)
            )
            for i in range(0, len(rows), effective_chunk_size):
                chunk = rows[i : i + effective_chunk_size]
                stmt = pg_insert(works_table).values(chunk)
                stmt = stmt.on_conflict_do_update(
                    index_elements=["source", "source_id"],
                    set_={
                        "record_origin": stmt.excluded.record_origin,
                        "title": stmt.excluded.title,
                        "title_normalized": stmt.excluded.title_normalized,
                        "original_language": func.coalesce(
                            stmt.excluded.original_language, works_table.c.original_language
                        ),
                        "primary_agent_id": func.coalesce(
                            stmt.excluded.primary_agent_id, works_table.c.primary_agent_id
                        ),
                        "classification": func.coalesce(
                            stmt.excluded.classification, works_table.c.classification
                        ),
                        "classification_scheme": func.coalesce(
                            stmt.excluded.classification_scheme, works_table.c.classification_scheme
                        ),
                        "work_type": stmt.excluded.work_type,
                        "serialized_text": stmt.excluded.serialized_text,
                    },
                ).returning(works_table.c.id, works_table.c.source, works_table.c.source_id)
                res = c.execute(stmt).fetchall()
                for r in res:
                    id_map[(r[1], r[2])] = r[0]

        if conn is not None:
            _exec(conn)
        else:
            with self.get_connection() as c:
                _exec(c)

        work_ids = [id_map.get((w.source, w.source_id), 0) for w in works]

        if vectors is not None and self.work_indexer is not None and len(vectors) == len(works):
            vector_ids = self.work_indexer.add_vectors(vectors, ids=work_ids)
            vec_rows = [
                {"vector_id": v_id, "work_id": w_id} for v_id, w_id in zip(vector_ids, work_ids)
            ]

            def _exec_vec(c: Any) -> None:
                effective_vec_chunk_size = chunk_size or calculate_batch_size(
                    num_columns=len(work_vectors_table.c)
                )
                for i in range(0, len(vec_rows), effective_vec_chunk_size):
                    chunk = vec_rows[i : i + effective_vec_chunk_size]
                    v_stmt = pg_insert(work_vectors_table).values(chunk)
                    v_stmt = v_stmt.on_conflict_do_update(
                        index_elements=["vector_id"],
                        set_={"work_id": v_stmt.excluded.work_id},
                    )
                    c.execute(v_stmt)

            if conn is not None:
                _exec_vec(conn)
            else:
                with self.get_connection() as c:
                    _exec_vec(c)

        wikidata_idents: list[IdentifierEntity] = []
        for work, wid in zip(works, work_ids):
            if getattr(work, "wikidata_qid", None) and work.source != "wikidata":
                wikidata_idents.append(
                    IdentifierEntity(
                        entity_type="work",
                        entity_id=wid,
                        source="wikidata",
                        source_id=work.wikidata_qid,
                        value=work.wikidata_qid,
                    )
                )
        if wikidata_idents:
            self.insert_identifiers_batch(wikidata_idents, conn=conn)

        if hasattr(self, "_work_cache") and self._work_cache:
            for wid in work_ids:
                self._work_cache.pop(wid, None)
        if hasattr(self, "_expressions_for_work_cache") and self._expressions_for_work_cache:
            for wid in work_ids:
                self._expressions_for_work_cache.pop(wid, None)

        return work_ids

    def insert_expressions_batch(
        self,
        expressions: list[ExpressionEntity],
        conn: Optional[Any] = None,
        chunk_size: Optional[int] = None,
    ) -> list[int]:
        """Insert or update ExpressionEntity records and optional dense vectors in batch."""
        if not expressions:
            return []

        deduped_rows: dict[tuple[str, str], dict[str, Any]] = {}
        for exp in expressions:
            key = (exp.source, exp.source_id)
            row = {
                "work_id": exp.work_id,
                "source": exp.source,
                "source_id": exp.source_id,
                "record_origin": exp.record_origin,
                "language": exp.language,
                "content_type": exp.content_type,
                "title": exp.title,
                "version_designation": exp.version_designation,
                "arrangement_note": exp.arrangement_note,
                "medium_of_performance": exp.medium_of_performance,
                "serialized_text": exp.serialized_text,
            }
            if key in deduped_rows:
                existing = deduped_rows[key]
                for field, val in row.items():
                    if val is not None:
                        existing[field] = val
            else:
                deduped_rows[key] = row

        rows = list(deduped_rows.values())
        id_map: dict[tuple[str, str], int] = {}

        def _exec(c: Any) -> None:
            effective_chunk_size = chunk_size or calculate_batch_size(
                num_columns=len(expressions_table.c)
            )
            for i in range(0, len(rows), effective_chunk_size):
                chunk = rows[i : i + effective_chunk_size]
                stmt = pg_insert(expressions_table).values(chunk)
                stmt = stmt.on_conflict_do_update(
                    index_elements=["source", "source_id"],
                    set_={
                        "work_id": stmt.excluded.work_id,
                        "record_origin": stmt.excluded.record_origin,
                        "language": stmt.excluded.language,
                        "content_type": stmt.excluded.content_type,
                        "title": stmt.excluded.title,
                        "version_designation": stmt.excluded.version_designation,
                        "arrangement_note": stmt.excluded.arrangement_note,
                        "medium_of_performance": stmt.excluded.medium_of_performance,
                        "serialized_text": stmt.excluded.serialized_text,
                    },
                ).returning(
                    expressions_table.c.id,
                    expressions_table.c.source,
                    expressions_table.c.source_id,
                )
                res = c.execute(stmt).fetchall()
                for r in res:
                    id_map[(r[1], r[2])] = r[0]

        if conn is not None:
            _exec(conn)
        else:
            with self.get_connection() as c:
                _exec(c)

        if hasattr(self, "_expressions_for_work_cache") and self._expressions_for_work_cache:
            self._expressions_for_work_cache.clear()
        return [id_map.get((e.source, e.source_id), 0) for e in expressions]

    def insert_manifestations_batch(
        self,
        manifestations: list[ManifestationEntity],
        vectors: Optional[np.ndarray] = None,
        conn: Optional[Any] = None,
        chunk_size: Optional[int] = None,
    ) -> list[int]:
        """Insert or update ManifestationEntity records and optional dense vectors in batch."""
        if not manifestations:
            return []

        deduped_rows: dict[tuple[str, str], dict[str, Any]] = {}
        for manif in manifestations:
            key = (manif.source, manif.source_id)
            row = {
                "work_id": manif.work_id,
                "container_id": manif.container_id,
                "is_component": bool(manif.is_component),
                "component_type": manif.component_type,
                "source": manif.source,
                "source_id": manif.source_id,
                "record_origin": manif.record_origin,
                "title": manif.title,
                "title_normalized": manif.title_normalized,
                "subtitle": manif.subtitle,
                "publication_year": manif.publication_year,
                "publisher": manif.publisher,
                "publication_place": manif.publication_place,
                "edition_statement": manif.edition_statement,
                "edition_number": manif.edition_number,
                "series_title": manif.series_title,
                "series_numbering": manif.series_numbering,
                "volume_designation": manif.volume_designation,
                "issue_enumeration": manif.issue_enumeration,
                "pagination": manif.pagination,
                "extent": manif.extent,
                "carrier_type": manif.carrier_type,
                "host_title": manif.host_title,
                "serialized_text": manif.serialized_text,
            }
            if key in deduped_rows:
                existing = deduped_rows[key]
                for field, val in row.items():
                    if val is not None:
                        existing[field] = val
            else:
                deduped_rows[key] = row

        rows = list(deduped_rows.values())
        id_map: dict[tuple[str, str], int] = {}

        def _exec(c: Any) -> None:
            effective_chunk_size = chunk_size or calculate_batch_size(
                num_columns=len(manifestations_table.c)
            )
            for i in range(0, len(rows), effective_chunk_size):
                chunk = rows[i : i + effective_chunk_size]
                stmt = pg_insert(manifestations_table).values(chunk)
                stmt = stmt.on_conflict_do_update(
                    index_elements=["source", "source_id"],
                    set_={
                        "work_id": func.coalesce(
                            stmt.excluded.work_id, manifestations_table.c.work_id
                        ),
                        "container_id": func.coalesce(
                            stmt.excluded.container_id, manifestations_table.c.container_id
                        ),
                        "is_component": stmt.excluded.is_component,
                        "component_type": stmt.excluded.component_type,
                        "record_origin": stmt.excluded.record_origin,
                        "title": stmt.excluded.title,
                        "title_normalized": stmt.excluded.title_normalized,
                        "subtitle": stmt.excluded.subtitle,
                        "publication_year": stmt.excluded.publication_year,
                        "publisher": stmt.excluded.publisher,
                        "publication_place": stmt.excluded.publication_place,
                        "edition_statement": stmt.excluded.edition_statement,
                        "edition_number": stmt.excluded.edition_number,
                        "series_title": stmt.excluded.series_title,
                        "series_numbering": stmt.excluded.series_numbering,
                        "volume_designation": stmt.excluded.volume_designation,
                        "issue_enumeration": stmt.excluded.issue_enumeration,
                        "pagination": stmt.excluded.pagination,
                        "extent": stmt.excluded.extent,
                        "carrier_type": stmt.excluded.carrier_type,
                        "host_title": stmt.excluded.host_title,
                        "serialized_text": stmt.excluded.serialized_text,
                    },
                ).returning(
                    manifestations_table.c.id,
                    manifestations_table.c.source,
                    manifestations_table.c.source_id,
                )
                res = c.execute(stmt).fetchall()
                for r in res:
                    id_map[(r[1], r[2])] = r[0]

        if conn is not None:
            _exec(conn)
        else:
            with self.get_connection() as c:
                _exec(c)

        manif_ids = [id_map.get((m.source, m.source_id), 0) for m in manifestations]

        if (
            vectors is not None
            and self.manifestation_indexer is not None
            and len(vectors) == len(manifestations)
        ):
            vector_ids = self.manifestation_indexer.add_vectors(vectors, ids=manif_ids)
            vec_rows = [
                {"vector_id": v_id, "manifestation_id": m_id}
                for v_id, m_id in zip(vector_ids, manif_ids)
            ]

            def _exec_mvec(c: Any) -> None:
                effective_vec_chunk_size = chunk_size or calculate_batch_size(
                    num_columns=len(manifestation_vectors_table.c)
                )
                for i in range(0, len(vec_rows), effective_vec_chunk_size):
                    chunk = vec_rows[i : i + effective_vec_chunk_size]
                    v_stmt = pg_insert(manifestation_vectors_table).values(chunk)
                    v_stmt = v_stmt.on_conflict_do_update(
                        index_elements=["vector_id"],
                        set_={"manifestation_id": v_stmt.excluded.manifestation_id},
                    )
                    c.execute(v_stmt)

            if conn is not None:
                _exec_mvec(conn)
            else:
                with self.get_connection() as c:
                    _exec_mvec(c)

        if hasattr(self, "_manifestation_cache") and self._manifestation_cache:
            for mid in manif_ids:
                self._manifestation_cache.pop(mid, None)

        return manif_ids

    def insert_manifestation_expressions_batch(
        self,
        assocs: list[ManifestationExpressionAssociation],
        conn: Optional[Any] = None,
        chunk_size: Optional[int] = None,
    ) -> None:
        """Insert or replace junction associations between Manifestations and Expressions."""
        if not assocs:
            return
        deduped_rows: dict[tuple[int, int], dict[str, Any]] = {}
        for a in assocs:
            key = (a.manifestation_id, a.expression_id)
            row = {
                "manifestation_id": a.manifestation_id,
                "expression_id": a.expression_id,
                "track_number": a.track_number,
                "disc_number": a.disc_number,
                "duration": a.duration,
                "is_primary": bool(a.is_primary),
            }
            if key in deduped_rows:
                existing = deduped_rows[key]
                for field, val in row.items():
                    if val is not None:
                        existing[field] = val
            else:
                deduped_rows[key] = row
        rows = list(deduped_rows.values())

        def _exec(c: Any) -> None:
            effective_assoc_chunk_size = chunk_size or calculate_batch_size(
                num_columns=len(manifestation_expressions_table.c)
            )
            for i in range(0, len(rows), effective_assoc_chunk_size):
                chunk = rows[i : i + effective_assoc_chunk_size]
                stmt = pg_insert(manifestation_expressions_table).values(chunk)
                stmt = stmt.on_conflict_do_update(
                    index_elements=["manifestation_id", "expression_id"],
                    set_={
                        "track_number": stmt.excluded.track_number,
                        "disc_number": stmt.excluded.disc_number,
                        "duration": stmt.excluded.duration,
                        "is_primary": stmt.excluded.is_primary,
                    },
                )
                c.execute(stmt)

        if conn is not None:
            _exec(conn)
        else:
            with self.get_connection() as c:
                _exec(c)

    def link_manifestation_expression(
        self, manifestation_id: int, expression_id: int, is_primary: bool = True
    ) -> None:
        """Convenience method to link a Manifestation to an Expression."""
        self.insert_manifestation_expressions_batch(
            [
                ManifestationExpressionAssociation(
                    manifestation_id=manifestation_id,
                    expression_id=expression_id,
                    is_primary=is_primary,
                )
            ]
        )

    def insert_work(self, work: WorkEntity, vector: Optional[np.ndarray] = None) -> int:
        """Convenience method to insert a single WorkEntity."""
        vectors = np.expand_dims(vector, axis=0) if vector is not None else None
        return self.insert_works_batch([work], vectors=vectors)[0]

    def insert_expression(self, expression: ExpressionEntity) -> int:
        """Insert a single ExpressionEntity."""
        return self.insert_expressions_batch([expression])[0]

    def insert_manifestation(
        self, manifestation: ManifestationEntity, vector: Optional[np.ndarray] = None
    ) -> int:
        """Convenience method to insert a single ManifestationEntity."""
        vectors = np.expand_dims(vector, axis=0) if vector is not None else None
        return self.insert_manifestations_batch([manifestation], vectors=vectors)[0]

    def _insert_simple_batch(
        self,
        table: Any,
        entities: list[Any],
        conn: Optional[Any] = None,
        chunk_size: Optional[int] = None,
        conflict_index_elements: Optional[list[str]] = None,
    ) -> list[int]:
        """Insert simple entity records in batch and return their assigned IDs."""
        if not entities:
            return []
        target = conn if conn is not None else self.get_connection()
        res = insert_batch(
            bind=target,
            table=table,
            rows=entities,
            on_conflict_do_nothing=bool(conflict_index_elements),
            conflict_index_elements=conflict_index_elements,
            return_ids=True,
            batch_size=chunk_size,
        )
        return res or []

    def insert_items_batch(
        self,
        items: list[ItemEntity],
        conn: Optional[Any] = None,
        chunk_size: Optional[int] = None,
    ) -> list[int]:
        """Insert ItemEntity records in batch with conflict resolution on (source, source_id)."""
        return self._insert_simple_batch(
            items_table,
            items,
            conn=conn,
            chunk_size=chunk_size,
            conflict_index_elements=["source", "source_id"],
        )

    def insert_identifiers_batch(
        self,
        identifiers: list[IdentifierEntity],
        conn: Optional[Any] = None,
        chunk_size: Optional[int] = None,
    ) -> list[int]:
        """Insert polymorphic IdentifierEntity records in batch with conflict resolution."""
        if hasattr(self, "_identifier_lookup_cache") and self._identifier_lookup_cache:
            self._identifier_lookup_cache.clear()
        return self._insert_simple_batch(
            identifiers_table,
            identifiers,
            conn=conn,
            chunk_size=chunk_size,
            conflict_index_elements=["entity_type", "entity_id", "source", "source_id"],
        )

    def insert_entity_keys_batch(
        self,
        keys: list[EntityKey],
        conn: Optional[Any] = None,
        chunk_size: Optional[int] = None,
    ) -> None:
        """Insert derived domain blocking keys in batch.

        Idempotent: the table's UNIQUE constraint makes re-ingesting a record a
        no-op for its keys rather than an accumulation of duplicates.
        """
        if not keys:
            return
        deduped = {
            (k.entity_type, k.entity_id, k.key_type, k.key_value): {
                "entity_type": k.entity_type,
                "entity_id": k.entity_id,
                "key_type": k.key_type,
                "key_value": k.key_value,
            }
            for k in keys
        }
        target = conn if conn is not None else self.get_connection()
        insert_batch(
            bind=target,
            table=entity_keys_table,
            rows=list(deduped.values()),
            on_conflict_do_nothing=True,
            conflict_index_elements=["entity_type", "entity_id", "key_type", "key_value"],
            batch_size=chunk_size,
        )

    def insert_contributions_batch(
        self,
        contributions: list[ContributionEntity],
        conn: Optional[Any] = None,
        chunk_size: Optional[int] = None,
    ) -> list[int]:
        """Insert ContributionEntity records in batch with conflict resolution."""
        return self._insert_simple_batch(
            contributions_table,
            contributions,
            conn=conn,
            chunk_size=chunk_size,
            conflict_index_elements=["entity_type", "entity_id", "agent_id", "role"],
        )

    def insert_work_relationships_batch(
        self,
        rels: list[WorkRelationship],
        conn: Optional[Any] = None,
        chunk_size: Optional[int] = None,
    ) -> list[int]:
        """Insert WorkRelationship records in batch."""
        if not rels:
            return []
        deduped = {
            (rel.source_work_id, rel.target_work_id, rel.relationship_type): {
                "source_work_id": rel.source_work_id,
                "target_work_id": rel.target_work_id,
                "relationship_type": rel.relationship_type,
                "sequence_number": rel.sequence_number,
            }
            for rel in rels
        }
        rows = list(deduped.values())
        id_map: dict[tuple[int, int, str], int] = {}

        def _exec(c: Any) -> None:
            effective_chunk_size = chunk_size or calculate_batch_size(
                num_columns=len(work_relationships_table.c)
            )
            for i in range(0, len(rows), effective_chunk_size):
                chunk = rows[i : i + effective_chunk_size]
                stmt = pg_insert(work_relationships_table).values(chunk)
                stmt = stmt.on_conflict_do_update(
                    index_elements=["source_work_id", "target_work_id", "relationship_type"],
                    set_={
                        "relationship_type": stmt.excluded.relationship_type,
                        "sequence_number": stmt.excluded.sequence_number,
                    },
                ).returning(
                    work_relationships_table.c.id,
                    work_relationships_table.c.source_work_id,
                    work_relationships_table.c.target_work_id,
                    work_relationships_table.c.relationship_type,
                )
                res = c.execute(stmt).fetchall()
                for r in res:
                    id_map[(r[1], r[2], r[3])] = r[0]

        if conn is not None:
            _exec(conn)
        else:
            with self.get_connection() as c:
                _exec(c)
        return [
            id_map.get((r.source_work_id, r.target_work_id, r.relationship_type), 0) for r in rels
        ]

    def insert_entity_subjects_batch(
        self,
        subjects: list[EntitySubject],
        conn: Optional[Any] = None,
        chunk_size: Optional[int] = None,
    ) -> list[int]:
        """Insert polymorphic EntitySubject records in batch with conflict resolution."""
        return self._insert_simple_batch(
            entity_subjects_table,
            subjects,
            conn=conn,
            chunk_size=chunk_size,
            conflict_index_elements=[
                "entity_type",
                "entity_id",
                "vocabulary",
                "concept_id",
                "raw_label",
                "relation_type",
            ],
        )

    def insert_wemi_batch(
        self,
        works: list[WorkEntity],
        expressions: list[ExpressionEntity],
        manifestations: list[ManifestationEntity],
        items: Optional[list[ItemEntity]] = None,
        agents: Optional[list[AgentEntity]] = None,
        relationships: Optional[list[WorkRelationship]] = None,
        agent_relationships: Optional[list[AgentRelationshipEntity]] = None,
        identifiers: Optional[list[IdentifierEntity]] = None,
        manifestation_expressions: Optional[list[ManifestationExpressionAssociation]] = None,
        contributions: Optional[list[ContributionEntity]] = None,
        entity_keys: Optional[list[EntityKey]] = None,
        entity_subjects: Optional[list[EntitySubject]] = None,
        work_vectors: Optional[Any] = None,
        manifestation_vectors: Optional[Any] = None,
        record_origin: Optional[RecordOrigin | str] = None,
        conn: Optional[Any] = None,
        chunk_size: Optional[int] = None,
        use_synchronous_commit_off: bool = False,
    ) -> None:
        """Coordinated batch insertion of WEMI domain entities with foreign key binding in a single transaction."""
        if record_origin:
            for w in works:
                w.record_origin = record_origin
            for exp in expressions:
                exp.record_origin = record_origin
            for m in manifestations:
                m.record_origin = record_origin
            if items:
                for it in items:
                    it.record_origin = record_origin
            if agents:
                for ag in agents:
                    ag.record_origin = record_origin

        def _resolve(key_dict: dict[Any, int], curie_or_uri: Optional[str]) -> Optional[int]:
            if not curie_or_uri:
                return None
            if curie_or_uri in key_dict:
                return key_dict[curie_or_uri]
            src, sid = parse_curie(str(curie_or_uri))
            if (src, sid) in key_dict:
                return key_dict[(src, sid)]
            c = to_curie(src, sid)
            if c in key_dict:
                return key_dict[c]
            if record_origin:
                if (record_origin, sid) in key_dict:
                    return key_dict[(record_origin, sid)]
                if to_curie(record_origin, sid) in key_dict:
                    return key_dict[to_curie(record_origin, sid)]
            if "#" in sid:
                sid_base, _, _ = sid.rpartition("#")
                if (src, sid_base) in key_dict:
                    return key_dict[(src, sid_base)]
                if to_curie(src, sid_base) in key_dict:
                    return key_dict[to_curie(src, sid_base)]
                if sid_base in key_dict:
                    return key_dict[sid_base]
            if ":" in sid:
                sid_base, _, _ = sid.rpartition(":")
                if (src, sid_base) in key_dict:
                    return key_dict[(src, sid_base)]
                if to_curie(src, sid_base) in key_dict:
                    return key_dict[to_curie(src, sid_base)]
                if sid_base in key_dict:
                    return key_dict[sid_base]
            return None

        def _run_in_txn(c: Any) -> None:
            if use_synchronous_commit_off:
                c.exec_driver_sql("SET LOCAL synchronous_commit = 'off'")

            # 1. Agents
            agent_key_to_id: dict[Any, int] = {}
            if agents:
                a_ids = self.insert_agents_batch(agents, conn=c, chunk_size=chunk_size)
                for a, aid in zip(agents, a_ids):
                    a.id = aid
                    agent_key_to_id[a.curie] = aid
                    agent_key_to_id[(a.source, a.source_id)] = aid
                    agent_key_to_id[a.source_id] = aid
                    agent_key_to_id[a.name] = aid
                    agent_key_to_id[a.name_normalized] = aid

            # 2. Works
            work_key_to_id: dict[Any, int] = {}
            if works:
                w_ids = self.insert_works_batch(
                    works, vectors=work_vectors, conn=c, chunk_size=chunk_size
                )
                for w, wid in zip(works, w_ids):
                    w.id = wid
                    work_key_to_id[w.curie] = wid
                    work_key_to_id[(w.source, w.source_id)] = wid
                    work_key_to_id[w.source_id] = wid

            # 3. Expressions
            expr_key_to_id: dict[Any, int] = {}
            if expressions:
                valid_expressions: list[ExpressionEntity] = []
                for exp in expressions:
                    if not exp.work_id or exp.work_id <= 0:
                        cand_id = _resolve(work_key_to_id, exp.work_curie)
                        if cand_id is None:
                            cand_id = work_key_to_id.get((exp.source, exp.source_id))
                        if cand_id is None and len(works) == 1:
                            cand_id = works[0].id
                        if cand_id:
                            exp.work_id = cand_id
                    if exp.work_id and exp.work_id > 0:
                        valid_expressions.append(exp)
                    else:
                        from reconciliator.logging import log_validation

                        log_validation(
                            logger,
                            action="quarantine",
                            entity="expressions.work_id",
                            source_id=exp.curie or f"{exp.source}:{exp.source_id}",
                            reason=f"Expression cannot resolve parent work_id (work_curie={exp.work_curie})",
                        )
                if valid_expressions:
                    e_ids = self.insert_expressions_batch(
                        valid_expressions, conn=c, chunk_size=chunk_size
                    )
                    for exp, eid in zip(valid_expressions, e_ids):
                        exp.id = eid
                        expr_key_to_id[exp.curie] = eid
                        expr_key_to_id[(exp.source, exp.source_id)] = eid
                        expr_key_to_id[exp.source_id] = eid
                        expr_key_to_id[f"{exp.source}:{exp.source_id}:{exp.language}"] = eid
                        expr_key_to_id[(exp.source, f"{exp.source_id}:{exp.language}")] = eid

            # 4. Manifestations, containers before components
            manif_key_to_id: dict[Any, int] = {}
            if manifestations:
                valid_manifestations: list[ManifestationEntity] = []
                for m in manifestations:
                    if not m.work_id or m.work_id <= 0:
                        cand_id = _resolve(work_key_to_id, m.work_curie)
                        if cand_id is None:
                            cand_id = work_key_to_id.get((m.source, m.source_id))
                        if cand_id is None and len(works) == 1:
                            cand_id = works[0].id
                        if cand_id:
                            m.work_id = cand_id
                    if m.work_id is not None and m.work_id <= 0:
                        from reconciliator.logging import log_validation

                        log_validation(
                            logger,
                            action="quarantine",
                            entity="manifestations.work_id",
                            source_id=m.curie or f"{m.source}:{m.source_id}",
                            reason=f"Manifestation has invalid parent work_id ({m.work_id})",
                        )
                    elif m.work_curie and not m.work_id:
                        from reconciliator.logging import log_validation

                        log_validation(
                            logger,
                            action="quarantine",
                            entity="manifestations.work_id",
                            source_id=m.curie or f"{m.source}:{m.source_id}",
                            reason=f"Manifestation cannot resolve parent work_id (work_curie={m.work_curie})",
                        )
                    else:
                        valid_manifestations.append(m)

                def _insert_manifestations(rows: list[ManifestationEntity], idx: list[int]) -> None:
                    if not rows:
                        return
                    vectors = (
                        [manifestation_vectors[i] for i in idx]
                        if manifestation_vectors is not None
                        else None
                    )
                    ids = self.insert_manifestations_batch(
                        rows, vectors=vectors, conn=c, chunk_size=chunk_size
                    )
                    for row, row_id in zip(rows, ids):
                        row.id = row_id
                        manif_key_to_id[row.curie] = row_id
                        manif_key_to_id[(row.source, row.source_id)] = row_id
                        manif_key_to_id[row.source_id] = row_id

                component_idx = [i for i, m in enumerate(valid_manifestations) if m.container_curie]
                if not component_idx:
                    _insert_manifestations(
                        valid_manifestations, list(range(len(valid_manifestations)))
                    )
                else:
                    container_idx = [
                        i for i, m in enumerate(valid_manifestations) if not m.container_curie
                    ]
                    _insert_manifestations(
                        [valid_manifestations[i] for i in container_idx], container_idx
                    )
                    for i in component_idx:
                        component = valid_manifestations[i]
                        resolved = _resolve(manif_key_to_id, component.container_curie)
                        if not resolved and component.container_curie:
                            existing = self.get_manifestation_by_curie(component.container_curie)
                            resolved = existing.id if existing else None
                        if resolved:
                            component.container_id = resolved
                        else:
                            logger.warning(
                                "Component %s/%s names container %s, which is not in "
                                "this batch or the store; inserting it unlinked.",
                                component.source,
                                component.source_id,
                                component.container_curie,
                            )
                    _insert_manifestations(
                        [valid_manifestations[i] for i in component_idx], component_idx
                    )

            # 5. Manifestation-Expression Associations
            if manifestation_expressions:
                bound_assocs: list[ManifestationExpressionAssociation] = []
                for assoc in manifestation_expressions:
                    if (
                        not assoc.manifestation_id or assoc.manifestation_id <= 0
                    ) and assoc.manifestation_curie:
                        assoc.manifestation_id = (
                            _resolve(manif_key_to_id, assoc.manifestation_curie) or 0
                        )
                    if (
                        not assoc.expression_id or assoc.expression_id <= 0
                    ) and assoc.expression_curie:
                        assoc.expression_id = _resolve(expr_key_to_id, assoc.expression_curie) or 0
                    if (
                        not assoc.manifestation_id or assoc.manifestation_id <= 0
                    ) and manifestations:
                        assoc.manifestation_id = manifestations[0].id or 0
                    if (not assoc.expression_id or assoc.expression_id <= 0) and expressions:
                        assoc.expression_id = expressions[0].id or 0
                    if assoc.manifestation_id > 0 and assoc.expression_id > 0:
                        bound_assocs.append(assoc)
                    else:
                        logger.warning(
                            "Dropping manifestation-expression link with an unbound endpoint: "
                            "manifestation=%s expression=%s",
                            assoc.manifestation_curie or assoc.manifestation_id,
                            assoc.expression_curie or assoc.expression_id,
                        )
                if bound_assocs:
                    self.insert_manifestation_expressions_batch(
                        bound_assocs, conn=c, chunk_size=chunk_size
                    )

            # 6. Items
            if items:
                valid_items: list[ItemEntity] = []
                for item in items:
                    if not item.manifestation_id or item.manifestation_id <= 0:
                        cand_id = manif_key_to_id.get((item.source, item.source_id))
                        if cand_id is None and len(manifestations) == 1:
                            cand_id = manifestations[0].id
                        if cand_id:
                            item.manifestation_id = cand_id
                    if item.manifestation_id and item.manifestation_id > 0:
                        valid_items.append(item)
                    else:
                        from reconciliator.logging import log_validation

                        log_validation(
                            logger,
                            action="quarantine",
                            entity="items.manifestation_id",
                            source_id=item.curie or f"{item.source}:{item.source_id}",
                            reason="Item cannot resolve parent manifestation_id",
                        )
                if valid_items:
                    self.insert_items_batch(valid_items, conn=c, chunk_size=chunk_size)

            # 7. Identifiers
            if identifiers:
                bound_identifiers: list[IdentifierEntity] = []
                for ident in identifiers:
                    if not ident.entity_id or ident.entity_id <= 0:
                        if ident.entity_curie:
                            if ident.entity_type == "agent":
                                resolved = _resolve(agent_key_to_id, ident.entity_curie)
                            elif ident.entity_type == "work":
                                resolved = _resolve(work_key_to_id, ident.entity_curie)
                            elif ident.entity_type == "expression":
                                resolved = _resolve(expr_key_to_id, ident.entity_curie)
                            elif ident.entity_type == "manifestation":
                                resolved = _resolve(manif_key_to_id, ident.entity_curie)
                            else:
                                resolved = None
                            if resolved:
                                ident.entity_id = resolved
                    if not ident.entity_id or ident.entity_id <= 0:
                        if ident.entity_type == "manifestation" and manifestations:
                            ident.entity_id = manifestations[0].id
                        elif ident.entity_type == "work" and works:
                            ident.entity_id = works[0].id
                        elif ident.entity_type == "expression" and expressions:
                            ident.entity_id = expressions[0].id
                        elif ident.entity_type == "agent" and agents:
                            ident.entity_id = agents[0].id
                    if ident.entity_id and ident.entity_id > 0:
                        bound_identifiers.append(ident)
                if bound_identifiers:
                    self.insert_identifiers_batch(bound_identifiers, conn=c, chunk_size=chunk_size)

            # 8. Work Relationships
            if relationships:
                bound_rels: list[WorkRelationship] = []
                for rel in relationships:
                    if (
                        not rel.source_work_id or rel.source_work_id <= 0
                    ) and rel.source_work_curie:
                        rel.source_work_id = _resolve(work_key_to_id, rel.source_work_curie) or 0
                    if (
                        not rel.target_work_id or rel.target_work_id <= 0
                    ) and rel.target_work_curie:
                        rel.target_work_id = _resolve(work_key_to_id, rel.target_work_curie) or 0
                    if (not rel.source_work_id or rel.source_work_id <= 0) and works:
                        rel.source_work_id = works[0].id or 0
                    if rel.source_work_id > 0 and rel.target_work_id > 0:
                        bound_rels.append(rel)
                    else:
                        logger.warning(
                            "Dropping %s edge with an unbound endpoint: source=%s target=%s",
                            rel.relationship_type,
                            rel.source_work_curie or rel.source_work_id,
                            rel.target_work_curie or rel.target_work_id,
                        )
                if bound_rels:
                    self.insert_work_relationships_batch(bound_rels, conn=c, chunk_size=chunk_size)

            # 8b. Agent Relationships
            if agent_relationships:
                bound_agent_rels: list[AgentRelationshipEntity] = []
                for a_rel in agent_relationships:
                    if (
                        not a_rel.source_agent_id or a_rel.source_agent_id <= 0
                    ) and a_rel.source_agent_curie:
                        a_rel.source_agent_id = (
                            _resolve(agent_key_to_id, a_rel.source_agent_curie) or 0
                        )
                    if (not a_rel.source_agent_id or a_rel.source_agent_id <= 0) and agents:
                        a_rel.source_agent_id = agents[0].id or 0

                    if (
                        not a_rel.target_agent_id or a_rel.target_agent_id <= 0
                    ) and a_rel.target_agent_curie:
                        a_rel.target_agent_id = _resolve(agent_key_to_id, a_rel.target_agent_curie)

                    if a_rel.source_agent_id > 0:
                        bound_agent_rels.append(a_rel)
                    else:
                        logger.warning(
                            "Dropping %s agent relationship with unbound source: source=%s target=%s",
                            a_rel.relationship_type,
                            a_rel.source_agent_curie or a_rel.source_agent_id,
                            a_rel.target_agent_curie or a_rel.target_agent_id,
                        )
                if bound_agent_rels:
                    self.insert_agent_relationships_batch(
                        bound_agent_rels, conn=c, chunk_size=chunk_size or 1000
                    )

            # 9. Contributions
            if contributions:
                valid_contribs: list[ContributionEntity] = []
                for c_item in contributions:
                    if (not c_item.entity_id or c_item.entity_id <= 0) and c_item.entity_curie:
                        if c_item.entity_type == "work":
                            c_item.entity_id = _resolve(work_key_to_id, c_item.entity_curie) or 0
                        elif c_item.entity_type == "expression":
                            c_item.entity_id = _resolve(expr_key_to_id, c_item.entity_curie) or 0
                        elif c_item.entity_type == "manifestation":
                            c_item.entity_id = _resolve(manif_key_to_id, c_item.entity_curie) or 0
                    if (not c_item.agent_id or c_item.agent_id <= 0) and c_item.agent_curie:
                        c_item.agent_id = _resolve(agent_key_to_id, c_item.agent_curie) or 0
                    if not c_item.entity_id or c_item.entity_id <= 0:
                        if c_item.entity_type == "work" and works:
                            c_item.entity_id = works[0].id
                        elif c_item.entity_type == "expression" and expressions:
                            c_item.entity_id = expressions[0].id
                        elif c_item.entity_type == "manifestation" and manifestations:
                            c_item.entity_id = manifestations[0].id
                    if not c_item.agent_id or c_item.agent_id <= 0:
                        if agents:
                            c_item.agent_id = agents[0].id
                    if (
                        c_item.agent_id
                        and c_item.agent_id > 0
                        and c_item.entity_id
                        and c_item.entity_id > 0
                    ):
                        valid_contribs.append(c_item)
                if valid_contribs:
                    self.insert_contributions_batch(valid_contribs, conn=c, chunk_size=chunk_size)

            # 11. Derived domain keys
            if entity_keys:
                bound_keys: list[EntityKey] = []
                for ek in entity_keys:
                    if (not ek.entity_id or ek.entity_id <= 0) and ek.entity_curie:
                        if ek.entity_type == "work":
                            ek.entity_id = _resolve(work_key_to_id, ek.entity_curie) or 0
                        elif ek.entity_type == "expression":
                            ek.entity_id = _resolve(expr_key_to_id, ek.entity_curie) or 0
                        elif ek.entity_type == "manifestation":
                            ek.entity_id = _resolve(manif_key_to_id, ek.entity_curie) or 0
                        elif ek.entity_type == "agent":
                            ek.entity_id = _resolve(agent_key_to_id, ek.entity_curie) or 0
                    if not ek.entity_id or ek.entity_id <= 0:
                        if ek.entity_type == "manifestation" and manifestations:
                            ek.entity_id = manifestations[0].id or 0
                        elif ek.entity_type == "work" and works:
                            ek.entity_id = works[0].id or 0
                        elif ek.entity_type == "expression" and expressions:
                            ek.entity_id = expressions[0].id or 0
                    if ek.entity_id and ek.entity_id > 0:
                        bound_keys.append(ek)
                if bound_keys:
                    self.insert_entity_keys_batch(bound_keys, conn=c, chunk_size=chunk_size)

            # 12. Entity subjects
            if entity_subjects:
                bound_subjects: list[EntitySubject] = []
                seen_subj_keys: set[
                    tuple[str, int, Optional[str], Optional[str], Optional[str], str]
                ] = set()
                for subj in entity_subjects:
                    if (not subj.entity_id or subj.entity_id <= 0) and subj.entity_curie:
                        if subj.entity_type == "work":
                            subj.entity_id = int(_resolve(work_key_to_id, subj.entity_curie) or 0)
                        elif subj.entity_type == "expression":
                            subj.entity_id = int(_resolve(expr_key_to_id, subj.entity_curie) or 0)
                        elif subj.entity_type == "manifestation":
                            subj.entity_id = int(_resolve(manif_key_to_id, subj.entity_curie) or 0)
                        elif subj.entity_type == "agent":
                            subj.entity_id = int(_resolve(agent_key_to_id, subj.entity_curie) or 0)
                    elif not subj.entity_id or subj.entity_id <= 0:
                        if subj.entity_type == "work" and works and works[0].id:
                            subj.entity_id = int(works[0].id)
                        elif subj.entity_type == "expression" and expressions and expressions[0].id:
                            subj.entity_id = int(expressions[0].id)
                        elif (
                            subj.entity_type == "manifestation"
                            and manifestations
                            and manifestations[0].id
                        ):
                            subj.entity_id = int(manifestations[0].id)
                        elif subj.entity_type == "agent" and agents and agents[0].id:
                            subj.entity_id = int(agents[0].id)
                    if subj.entity_id and subj.entity_id > 0:
                        s_key = (
                            subj.entity_type,
                            subj.entity_id,
                            subj.vocabulary,
                            subj.concept_id,
                            subj.raw_label,
                            subj.relation_type,
                        )
                        if s_key not in seen_subj_keys:
                            seen_subj_keys.add(s_key)
                            bound_subjects.append(subj)
                if bound_subjects:
                    self.insert_entity_subjects_batch(bound_subjects, conn=c, chunk_size=chunk_size)

        if conn is not None:
            _run_in_txn(conn)
        else:
            with self.get_connection() as c:
                _run_in_txn(c)

