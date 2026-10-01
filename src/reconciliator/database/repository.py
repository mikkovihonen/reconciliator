"""WEMIRepository mediating access to PostgreSQL database and vector index."""

from __future__ import annotations

import logging
import re
from collections import OrderedDict, defaultdict
from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

import numpy as np
from sqlalchemy import (
    Table,
    and_,
    delete,
    distinct,
    func,
    nulls_last,
    or_,
    select,
    union,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert

from reconciliator.database.engine import get_engine, init_authority_db
from reconciliator.database.mixins import (
    AuthorityAgentRepositoryMixin,
    ChangesetRepositoryMixin,
    ClusterRepositoryMixin,
    ConsolidationRepositoryMixin,
    EntityLookupRepositoryMixin,
    HarvestStateRepositoryMixin,
    MatchRecordRepositoryMixin,
    PaginationRepositoryMixin,
    SearchRepositoryMixin,
    SourceManagementMixin,
    VectorCacheRepositoryMixin,
)
from reconciliator.database.ops import calculate_batch_size, insert_batch
from reconciliator.database.schema import (
    agent_attributes_table,
    agents_table,
    contributions_table,
    entity_keys_table,
    entity_subjects_table,
    expressions_table,
    identifiers_table,
    items_table,
    manifestation_expressions_table,
    manifestation_vectors_table,
    manifestations_table,
    sources_table,
    work_consolidations_table,
    work_relationships_table,
    work_vectors_table,
    works_table,
)
from reconciliator.indexer import VectorIndex, create_vector_engine
from reconciliator.models import (
    AgentEntity,
    AgentRelationshipEntity,
    ContributionEntity,
    EntityKey,
    EntitySubject,
    EntityType,
    ExpressionEntity,
    HarvestState,
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
    from sqlalchemy.engine import Engine

    from reconciliator.config import RepositoryFTSConfig, VectorEngineConfig

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# WEMI Entity Core Repository Helpers
# ---------------------------------------------------------------------------


def _row_to_dict(row: Any) -> dict[str, Any]:
    if hasattr(row, "_mapping"):
        d = dict(row._mapping)
    elif isinstance(row, dict):
        d = dict(row)
    else:
        d = dict(row)
    for k, v in d.items():
        if isinstance(v, (datetime, date)):
            d[k] = v.isoformat()
    return d


def _row_to_agent(row: Any) -> AgentEntity:
    return AgentEntity.model_validate(_row_to_dict(row))


def _row_to_work(row: Any) -> WorkEntity:
    return WorkEntity.model_validate(_row_to_dict(row))


def _row_to_expression(row: Any) -> ExpressionEntity:
    return ExpressionEntity.model_validate(_row_to_dict(row))


def _row_to_manifestation(row: Any) -> ManifestationEntity:
    return ManifestationEntity.model_validate(_row_to_dict(row))


def _row_to_item(row: Any) -> ItemEntity:
    return ItemEntity.model_validate(_row_to_dict(row))


def _row_to_identifier(row: Any) -> IdentifierEntity:
    return IdentifierEntity.model_validate(_row_to_dict(row))


def _row_to_contribution(row: Any) -> ContributionEntity:
    return ContributionEntity.model_validate(_row_to_dict(row))


def _row_to_work_relationship(row: Any) -> WorkRelationship:
    return WorkRelationship.model_validate(_row_to_dict(row))


def _row_to_harvest_state(row: Any) -> HarvestState:
    return HarvestState.model_validate(_row_to_dict(row))


class WEMIRepository(
    VectorCacheRepositoryMixin,
    MatchRecordRepositoryMixin,
    HarvestStateRepositoryMixin,
    ClusterRepositoryMixin,
    AuthorityAgentRepositoryMixin,
    ChangesetRepositoryMixin,
    SearchRepositoryMixin,
    PaginationRepositoryMixin,
    SourceManagementMixin,
    ConsolidationRepositoryMixin,
    EntityLookupRepositoryMixin,
):
    """Unified repository managing WEMI tables, corpus staging/clustering, harvest state,
    FTS5 lexical search, and tripartite vector indices in authority.db and its corpus database.
    """

    def __init__(
        self,
        db_path: Path | str | None = None,
        work_index_path: Optional[Path | str] = None,
        manifestation_index_path: Optional[Path | str] = None,
        dimension: int = 768,
        vector_engine_config: Optional[VectorEngineConfig] = None,
        fts_config: Optional["RepositoryFTSConfig"] = None,
        work_indexer: Optional[VectorIndex] = None,
        manifestation_indexer: Optional[VectorIndex] = None,
        engine: Optional[Engine] = None,
        entity_cache_size: Optional[int] = None,
    ) -> None:
        if engine is not None:
            self.engine = engine
            self.db_path = engine.url.render_as_string(hide_password=False)
        else:
            self.engine = get_engine(db_path)
            self.db_path = self.engine.url.render_as_string(hide_password=False)
        self.dimension = dimension
        self.vector_engine_config = vector_engine_config
        if fts_config is not None:
            self.fts_config = fts_config
        else:
            from reconciliator.config import RepositoryFTSConfig

            self.fts_config = RepositoryFTSConfig()

        if entity_cache_size is not None:
            self.entity_cache_size = entity_cache_size
        else:
            from reconciliator.config.defaults import get_bundled_reconcile_defaults

            self.entity_cache_size = int(
                get_bundled_reconcile_defaults().get("entity_cache_size", 50000)
            )

        self._work_cache: OrderedDict[int, WorkEntity] = OrderedDict()
        self._manifestation_cache: OrderedDict[int, ManifestationEntity] = OrderedDict()
        self._expressions_for_work_cache: OrderedDict[int, list[ExpressionEntity]] = OrderedDict()
        self._identifier_lookup_cache: OrderedDict[
            tuple[str, str, bool], list[tuple[EntityType, int]]
        ] = OrderedDict()

        init_authority_db(self.engine)

        # Work indexer
        if work_indexer is not None:
            self.work_indexer = work_indexer
        elif work_index_path is not None:
            self.work_indexer = create_vector_engine(
                Path(work_index_path), dimension=self.dimension, config=vector_engine_config
            )
        else:
            self.work_indexer = None

        # Expression indexer

        # Manifestation indexer
        if manifestation_indexer is not None:
            self.manifestation_indexer = manifestation_indexer
        elif manifestation_index_path is not None:
            self.manifestation_indexer = create_vector_engine(
                Path(manifestation_index_path),
                dimension=self.dimension,
                config=vector_engine_config,
            )
        else:
            self.manifestation_indexer = None

        # Agent indexer
        self.agent_indexer = None

    def get_connection(self) -> Any:
        """Obtain connection with an active transaction."""
        return self.engine.begin()

    def get_read_connection(self) -> Any:
        """Obtain lightweight read connection without transaction wrapping."""
        return self.engine.connect()

    def clear_entity_caches(self) -> None:
        """Clear all in-memory LRU entity and lookup caches."""
        self._work_cache.clear()
        self._manifestation_cache.clear()
        self._expressions_for_work_cache.clear()
        self._identifier_lookup_cache.clear()

    def get_corpus_connection(self) -> Any:
        """Corpus staging shares the authority database.

        Retained as a named entry point because the corpus tables are a distinct
        concern, but they are no longer a distinct file: a separate database made
        joins between a cluster and the works it resolves to impossible, and let
        reset clear one database while the repository wrote to another.
        """
        return self.get_connection()

    def close(self) -> None:
        """Dispose SQLAlchemy engine and release connection pool."""
        self.engine.dispose()

    # -----------------------------------------------------------------------
    # Batch Insertions
    # -----------------------------------------------------------------------

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

    def get_entity_keys(self, entity_type: str, entity_id: int) -> dict[str, list[str]]:
        """Return the key bag for one entity, shaped as the funnels consume it."""
        bag: dict[str, list[str]] = {}
        stmt = (
            select(entity_keys_table.c.key_type, entity_keys_table.c.key_value)
            .where(
                entity_keys_table.c.entity_type == entity_type,
                entity_keys_table.c.entity_id == entity_id,
            )
            .order_by(entity_keys_table.c.id)
        )
        with self.get_connection() as conn:
            rows = conn.execute(stmt).mappings().fetchall()
        for row in rows:
            bag.setdefault(row["key_type"], []).append(row["key_value"])
        return bag

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

    # -----------------------------------------------------------------------
    # Lookups & Retrievals
    # -----------------------------------------------------------------------

    def get_subjects_for_entity(self, entity_type: str, entity_id: int) -> list[EntitySubject]:
        """Fetch all EntitySubject records linked to a specific entity."""
        stmt = (
            select(entity_subjects_table)
            .where(
                entity_subjects_table.c.entity_type == entity_type,
                entity_subjects_table.c.entity_id == entity_id,
            )
            .order_by(entity_subjects_table.c.is_primary.desc(), entity_subjects_table.c.id.asc())
        )
        with self.get_connection() as conn:
            rows = conn.execute(stmt).mappings().fetchall()
            return [EntitySubject.model_validate(dict(r)) for r in rows]

    def get_subjects_for_agent(self, agent_id: int) -> list[EntitySubject]:
        """Fetch all EntitySubject records linked to an agent (e.g. occupation, field_of_activity)."""
        return self.get_subjects_for_entity("agent", agent_id)

    def get_entities_by_concept(
        self,
        vocabulary: str,
        concept_id: str,
        entity_type: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """Fetch entity subject rows linked to a specific vocabulary concept."""
        stmt = select(entity_subjects_table).where(
            entity_subjects_table.c.vocabulary == vocabulary,
            entity_subjects_table.c.concept_id == concept_id,
        )
        if entity_type:
            stmt = stmt.where(entity_subjects_table.c.entity_type == entity_type)
        stmt = stmt.order_by(entity_subjects_table.c.id.asc())
        with self.get_connection() as conn:
            rows = conn.execute(stmt).mappings().fetchall()
            return [dict(r) for r in rows]

    def _get_entity_by_id(
        self, table: Any, row_converter: Callable[[Any], Any], entity_id: int
    ) -> Any:
        stmt = select(table).where(table.c.id == entity_id)
        with self.get_connection() as conn:
            row = conn.execute(stmt).fetchone()
            return row_converter(row) if row else None

    def _get_entity_by_source(
        self,
        table: Any,
        row_converter: Callable[[Any], Any],
        source: str,
        source_id: str,
        case_insensitive: bool = False,
    ) -> Any:
        source_cond = (
            func.lower(table.c.source) == source.lower()
            if case_insensitive
            else table.c.source == source
        )
        stmt = select(table).where(source_cond, table.c.source_id == source_id)
        with self.get_connection() as conn:
            row = conn.execute(stmt).fetchone()
            return row_converter(row) if row else None

    def get_agent(self, agent_id: int) -> Optional[AgentEntity]:
        """Fetch AgentEntity by ID with relational attributes and identifiers."""
        agents = self.get_agents_by_ids([agent_id])
        if agent_id in agents:
            return AgentEntity.model_validate(agents[agent_id])
        return None

    def get_agents_by_ids(self, agent_ids: list[int]) -> dict[int, dict[str, Any]]:
        """Fetch agent profiles with relational attributes for a list of IDs."""
        if not agent_ids:
            return {}
        result: dict[int, dict[str, Any]] = {}
        with self.get_connection() as conn:
            # 1. Fetch agents
            agents_stmt = select(agents_table).where(agents_table.c.id.in_(agent_ids))
            agent_rows = conn.execute(agents_stmt).mappings().fetchall()
            for row in agent_rows:
                agent_id = row["id"]
                agent_dict = dict(row)
                agent_dict["qualifiers"] = []
                agent_dict["variant_names"] = []
                agent_dict["identifiers"] = []
                result[agent_id] = agent_dict

            # 2. Batch fetch attributes in a single query
            attr_stmt = select(
                agent_attributes_table.c.agent_id,
                agent_attributes_table.c.attribute_type,
                agent_attributes_table.c.value,
            ).where(agent_attributes_table.c.agent_id.in_(agent_ids))
            for r in conn.execute(attr_stmt):
                aid = r[0]
                atype = r[1]
                val = r[2]
                if aid in result:
                    if atype == "qualifier":
                        result[aid]["qualifiers"].append(val)
                    elif atype == "variant_name":
                        result[aid]["variant_names"].append(val)

            # 3. Batch fetch identifiers in a single query
            ident_stmt = select(identifiers_table).where(
                identifiers_table.c.entity_type == "agent",
                identifiers_table.c.entity_id.in_(agent_ids),
            )
            for r in conn.execute(ident_stmt):
                aid = r.entity_id if hasattr(r, "entity_id") else r[2]
                if aid in result:
                    result[aid]["identifiers"].append(_row_to_identifier(r))

        return result

    def get_agent_by_curie(self, curie_or_uri: str) -> Optional[AgentEntity]:
        """Fetch AgentEntity by CURIE or URI with relational attributes and identifiers."""
        source, source_id = parse_curie(curie_or_uri)
        if not source_id:
            return None
        with self.get_connection() as conn:
            stmt = select(agents_table.c.id).where(
                agents_table.c.source == source,
                agents_table.c.source_id == source_id,
            )
            row = conn.execute(stmt).fetchone()
            if not row:
                from reconciliator.config import get_source_aliases

                aliases = get_source_aliases(source)
                if aliases:
                    fallback_stmt = select(agents_table.c.id).where(
                        agents_table.c.source.in_([source, *aliases]),
                        agents_table.c.source_id == source_id,
                    )
                    row = conn.execute(fallback_stmt).fetchone()
        if row:
            return self.get_agent(row[0])
        return None

    def _enrich_work_identifiers(
        self, conn: Any, work: Optional[WorkEntity]
    ) -> Optional[WorkEntity]:
        if work and not work.wikidata_qid and work.id:
            stmt = (
                select(identifiers_table.c.source_id)
                .where(
                    identifiers_table.c.entity_type == "work",
                    identifiers_table.c.entity_id == work.id,
                    func.lower(identifiers_table.c.source) == "wikidata",
                )
                .limit(1)
            )
            qid = conn.execute(stmt).scalar()
            if qid:
                work.wikidata_qid = qid
        return work

    def get_work(self, work_id: int) -> Optional[WorkEntity]:
        """Fetch WorkEntity by ID with in-memory LRU caching."""
        if work_id in self._work_cache:
            self._work_cache.move_to_end(work_id)
            return self._work_cache[work_id]

        stmt = select(works_table).where(works_table.c.id == work_id)
        with self.get_read_connection() as conn:
            row = conn.execute(stmt).fetchone()
            work = self._enrich_work_identifiers(conn, _row_to_work(row)) if row else None

        if work is not None:
            self._work_cache[work_id] = work
            if len(self._work_cache) > self.entity_cache_size:
                self._work_cache.popitem(last=False)
        return work

    def get_works_by_ids(self, work_ids: list[int]) -> dict[int, WorkEntity]:
        """Fetch multiple WorkEntity instances by IDs, leveraging LRU cache."""
        if not work_ids:
            return {}
        result: dict[int, WorkEntity] = {}
        missing_ids: list[int] = []
        for wid in work_ids:
            if wid in self._work_cache:
                self._work_cache.move_to_end(wid)
                result[wid] = self._work_cache[wid]
            else:
                missing_ids.append(wid)

        if missing_ids:
            stmt = select(works_table).where(works_table.c.id.in_(missing_ids))
            with self.get_read_connection() as conn:
                for row in conn.execute(stmt):
                    w = self._enrich_work_identifiers(conn, _row_to_work(row))
                    if w and w.id:
                        result[w.id] = w
                        self._work_cache[w.id] = w
                        if len(self._work_cache) > self.entity_cache_size:
                            self._work_cache.popitem(last=False)
        return result

    def get_work_by_curie(self, curie_or_uri: str) -> Optional[WorkEntity]:
        """Fetch WorkEntity by CURIE or URI."""
        source, source_id = parse_curie(curie_or_uri)
        if not source_id:
            return None
        return self.get_work_by_source(source, source_id)

    def get_work_by_source(self, source: str, source_id: str) -> Optional[WorkEntity]:
        """Fetch WorkEntity by source and source_id."""
        stmt = select(works_table).where(
            works_table.c.source == source,
            works_table.c.source_id == source_id,
        )
        with self.get_read_connection() as conn:
            row = conn.execute(stmt).fetchone()
            return self._enrich_work_identifiers(conn, _row_to_work(row)) if row else None

    def get_expression(self, expression_id: int) -> Optional[ExpressionEntity]:
        """Fetch ExpressionEntity by ID."""
        return self._get_entity_by_id(expressions_table, _row_to_expression, expression_id)

    def get_expression_by_curie(self, curie_or_uri: str) -> Optional[ExpressionEntity]:
        """Fetch ExpressionEntity by CURIE or URI."""
        source, source_id = parse_curie(curie_or_uri)
        if not source_id:
            return None
        return self._get_entity_by_source(
            expressions_table, _row_to_expression, source, source_id, case_insensitive=True
        )

    def get_manifestation(self, manifestation_id: int) -> Optional[ManifestationEntity]:
        """Fetch ManifestationEntity by ID with in-memory LRU caching."""
        if manifestation_id in self._manifestation_cache:
            self._manifestation_cache.move_to_end(manifestation_id)
            return self._manifestation_cache[manifestation_id]

        manif = self._get_entity_by_id(
            manifestations_table, _row_to_manifestation, manifestation_id
        )
        if manif is not None:
            self._manifestation_cache[manifestation_id] = manif
            if len(self._manifestation_cache) > self.entity_cache_size:
                self._manifestation_cache.popitem(last=False)
        return manif

    def get_manifestations_by_ids(
        self, manifestation_ids: list[int]
    ) -> dict[int, ManifestationEntity]:
        """Fetch multiple ManifestationEntity instances by IDs, leveraging LRU cache."""
        if not manifestation_ids:
            return {}
        result: dict[int, ManifestationEntity] = {}
        missing_ids: list[int] = []
        for mid in manifestation_ids:
            if mid in self._manifestation_cache:
                self._manifestation_cache.move_to_end(mid)
                result[mid] = self._manifestation_cache[mid]
            else:
                missing_ids.append(mid)

        if missing_ids:
            stmt = select(manifestations_table).where(manifestations_table.c.id.in_(missing_ids))
            with self.get_read_connection() as conn:
                for row in conn.execute(stmt):
                    m = _row_to_manifestation(row)
                    if m and m.id:
                        result[m.id] = m
                        self._manifestation_cache[m.id] = m
                        if len(self._manifestation_cache) > self.entity_cache_size:
                            self._manifestation_cache.popitem(last=False)
        return result

    def get_manifestation_by_source(
        self, source: str, source_id: str
    ) -> Optional[ManifestationEntity]:
        """Fetch ManifestationEntity by source and source_id."""
        return self._get_entity_by_source(
            manifestations_table, _row_to_manifestation, source, source_id
        )

    def get_expressions_for_work(
        self, work_id: int, record_origin: Optional[RecordOrigin | str] = None
    ) -> list[ExpressionEntity]:
        """Fetch all Expression entities linked to a given work_id, with optional record_origin filter."""
        if record_origin is None and work_id in self._expressions_for_work_cache:
            self._expressions_for_work_cache.move_to_end(work_id)
            return self._expressions_for_work_cache[work_id]

        stmt = select(expressions_table).where(expressions_table.c.work_id == work_id)
        if record_origin is not None:
            stmt = stmt.where(expressions_table.c.record_origin == record_origin)
        stmt = stmt.order_by(expressions_table.c.id)
        with self.get_read_connection() as conn:
            rows = conn.execute(stmt).fetchall()
            exps = [_row_to_expression(r) for r in rows]

        if record_origin is None:
            self._expressions_for_work_cache[work_id] = exps
            if len(self._expressions_for_work_cache) > self.entity_cache_size:
                self._expressions_for_work_cache.popitem(last=False)
        return exps

    def get_constituent_works(self, work_id: int) -> list[WorkEntity]:
        """The works this one contains, by stored whole-part edges."""
        stmt = (
            select(
                works_table,
                func.min(work_relationships_table.c.sequence_number).label("seq"),
                func.min(manifestation_expressions_table.c.disc_number).label("d"),
                func.min(manifestation_expressions_table.c.track_number).label("t"),
            )
            .select_from(
                work_relationships_table.join(
                    works_table, works_table.c.id == work_relationships_table.c.target_work_id
                )
                .outerjoin(expressions_table, expressions_table.c.work_id == works_table.c.id)
                .outerjoin(
                    manifestation_expressions_table,
                    manifestation_expressions_table.c.expression_id == expressions_table.c.id,
                )
            )
            .where(
                work_relationships_table.c.source_work_id == work_id,
                work_relationships_table.c.relationship_type.in_(("has_part", "has_constituent")),
            )
            .group_by(*works_table.c)
            .order_by(
                nulls_last(func.min(work_relationships_table.c.sequence_number)),
                nulls_last(func.min(manifestation_expressions_table.c.disc_number)),
                nulls_last(func.min(manifestation_expressions_table.c.track_number)),
                works_table.c.id,
            )
        )
        with self.get_connection() as conn:
            rows = conn.execute(stmt).fetchall()
            return [_row_to_work(r) for r in rows]

    def get_manifestation_by_curie(self, curie_or_uri: str) -> Optional[ManifestationEntity]:
        """Fetch ManifestationEntity by CURIE or URI."""
        source, source_id = parse_curie(curie_or_uri)
        if not source_id:
            return None
        return self.get_manifestation_by_source(source, source_id)

    def get_manifestations_for_work(
        self, work_id: int, record_origin: Optional[RecordOrigin | str] = None
    ) -> list[ManifestationEntity]:
        """Fetch all Manifestation entities linked directly to a given work_id, with optional record_origin filter."""
        stmt = select(manifestations_table).where(manifestations_table.c.work_id == work_id)
        if record_origin is not None:
            stmt = stmt.where(manifestations_table.c.record_origin == record_origin)
        stmt = stmt.order_by(
            manifestations_table.c.publication_year.asc(), manifestations_table.c.id.asc()
        )
        with self.get_connection() as conn:
            rows = conn.execute(stmt).fetchall()
            return [_row_to_manifestation(r) for r in rows]

    def get_manifestations_for_works(
        self, work_ids: list[int], record_origin: Optional[RecordOrigin | str] = None
    ) -> dict[int, list[ManifestationEntity]]:
        """Fetch all Manifestation entities linked to given work_ids, grouped by work_id."""
        if not work_ids:
            return {}
        result: dict[int, list[ManifestationEntity]] = {wid: [] for wid in work_ids}
        chunk_size = 1000
        with self.get_connection() as conn:
            for i in range(0, len(work_ids), chunk_size):
                chunk = work_ids[i : i + chunk_size]
                stmt = select(manifestations_table).where(manifestations_table.c.work_id.in_(chunk))
                if record_origin is not None:
                    stmt = stmt.where(manifestations_table.c.record_origin == record_origin)
                stmt = stmt.order_by(
                    manifestations_table.c.publication_year.asc(), manifestations_table.c.id.asc()
                )
                rows = conn.execute(stmt).fetchall()
                for r in rows:
                    m = _row_to_manifestation(r)
                    if m.work_id in result:
                        result[m.work_id].append(m)
        return result

    def get_expressions_for_manifestation(
        self, manifestation_id: int, record_origin: Optional[RecordOrigin | str] = None
    ) -> list[ExpressionEntity]:
        """Fetch Expression entities associated with a manifestation via manifestation_expressions."""
        stmt = (
            select(expressions_table)
            .select_from(
                manifestation_expressions_table.join(
                    expressions_table,
                    manifestation_expressions_table.c.expression_id == expressions_table.c.id,
                )
            )
            .where(manifestation_expressions_table.c.manifestation_id == manifestation_id)
        )
        if record_origin is not None:
            stmt = stmt.where(expressions_table.c.record_origin == record_origin)
        stmt = stmt.order_by(
            manifestation_expressions_table.c.disc_number.asc(),
            manifestation_expressions_table.c.track_number.asc(),
            expressions_table.c.id.asc(),
        )
        with self.get_connection() as conn:
            rows = conn.execute(stmt).fetchall()
            return [_row_to_expression(r) for r in rows]

    def get_manifestations_for_expression(
        self, expression_id: int, record_origin: Optional[RecordOrigin | str] = None
    ) -> list[ManifestationEntity]:
        """Fetch Manifestation entities associated with an expression via manifestation_expressions."""
        stmt = (
            select(manifestations_table)
            .select_from(
                manifestation_expressions_table.join(
                    manifestations_table,
                    manifestation_expressions_table.c.manifestation_id == manifestations_table.c.id,
                )
            )
            .where(manifestation_expressions_table.c.expression_id == expression_id)
        )
        if record_origin is not None:
            stmt = stmt.where(manifestations_table.c.record_origin == record_origin)
        stmt = stmt.order_by(
            manifestations_table.c.publication_year.asc(), manifestations_table.c.id.asc()
        )
        with self.get_connection() as conn:
            rows = conn.execute(stmt).fetchall()
            return [_row_to_manifestation(r) for r in rows]

    def get_items_for_manifestation(
        self, manifestation_id: int, record_origin: Optional[RecordOrigin | str] = None
    ) -> list[ItemEntity]:
        """Fetch all Item entities held under a given manifestation."""
        stmt = select(items_table).where(items_table.c.manifestation_id == manifestation_id)
        if record_origin is not None:
            stmt = stmt.where(items_table.c.record_origin == record_origin)
        stmt = stmt.order_by(items_table.c.id)
        with self.get_connection() as conn:
            rows = conn.execute(stmt).fetchall()
            return [_row_to_item(r) for r in rows]

    def get_work_relationships(self, work_id: int) -> list[WorkRelationship]:
        """Fetch all relationships where work_id is either source or target."""
        stmt = (
            select(work_relationships_table)
            .where(
                or_(
                    work_relationships_table.c.source_work_id == work_id,
                    work_relationships_table.c.target_work_id == work_id,
                )
            )
            .order_by(work_relationships_table.c.id)
        )
        with self.get_connection() as conn:
            rows = conn.execute(stmt).fetchall()
            return [_row_to_work_relationship(r) for r in rows]

    def register_source(self, name: str, is_authoritative: bool = True) -> None:
        """Record an ingest source and whether its records carry authority."""
        stmt = pg_insert(sources_table).values(
            name=name,
            is_authoritative=bool(is_authoritative),
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["name"],
            set_={
                "is_authoritative": stmt.excluded.is_authoritative,
                "last_seen": func.current_timestamp(),
            },
        )
        with self.get_connection() as conn:
            conn.execute(stmt)

    def get_sources(self) -> dict[str, bool]:
        """Map every known ingest source to whether it is authoritative."""
        stmt = select(sources_table.c.name, sources_table.c.is_authoritative)
        with self.get_connection() as conn:
            return {r["name"]: bool(r["is_authoritative"]) for r in conn.execute(stmt).mappings()}

    def authoritative_sources(self) -> list[str]:
        """Names of the sources whose records may be used as authority."""
        stmt = (
            select(sources_table.c.name)
            .where(sources_table.c.is_authoritative == True)  # noqa: E712
            .order_by(sources_table.c.name)
        )
        with self.get_connection() as conn:
            return list(conn.execute(stmt).scalars().all())

    def non_authoritative_sources(self) -> list[str]:
        """Source names registered as non-authoritative, i.e. corpora to cluster."""
        return sorted(name for name, auth in self.get_sources().items() if not auth)

    def apply_work_consolidations(self, group_map: dict[str, list[str]]) -> int:
        """Apply work-level cluster groupings to work_consolidations_table."""
        if not group_map:
            return 0
        updates = 0
        mapping: dict[int, int] = {}
        for canon_source_id, member_ids in group_map.items():
            w_canon = self.get_work_by_source_id(canon_source_id)
            if not w_canon or not w_canon.id:
                continue
            canon_pk = w_canon.id

            for mid in member_ids:
                w = self.get_work_by_source_id(mid)
                if w and w.id:
                    mapping[w.id] = canon_pk
                    updates += 1
        if mapping:
            self.set_canonical_works(mapping)
        return updates

    def set_canonical_works(
        self, mapping: dict[int, int], evidence_map: Optional[dict[int, str]] = None
    ) -> None:
        """Record the elected canonical work for each consolidated work id."""
        if not mapping:
            return
        rows = [
            {
                "work_id": work_id,
                "canonical_work_id": canonical,
                "evidence": (evidence_map or {}).get(work_id),
            }
            for work_id, canonical in mapping.items()
        ]
        with self.get_connection() as conn:
            stmt = pg_insert(work_consolidations_table).values(rows)
            stmt = stmt.on_conflict_do_update(
                index_elements=["work_id"],
                set_={
                    "canonical_work_id": stmt.excluded.canonical_work_id,
                    "evidence": stmt.excluded.evidence,
                },
            )
            conn.execute(stmt)

    def clear_canonical_works(self) -> None:
        """Undo consolidation, leaving every work its own canonical."""
        self.reset_work_consolidations()

    def reset_work_consolidations(self) -> None:
        """Clear work_consolidations and same_work_as relationships."""
        with self.get_connection() as conn:
            conn.execute(delete(work_consolidations_table))
            conn.execute(
                delete(work_relationships_table).where(
                    work_relationships_table.c.relationship_type == "same_work_as"
                )
            )

    def get_work_equivalence_map(self) -> dict[int, int]:
        """Map each consolidated work id to its group's canonical work id."""
        stmt = select(
            work_consolidations_table.c.work_id,
            work_consolidations_table.c.canonical_work_id,
        ).where(
            work_consolidations_table.c.canonical_work_id != work_consolidations_table.c.work_id,
        )
        with self.get_connection() as conn:
            return {r[0]: r[1] for r in conn.execute(stmt)}

    def get_identifiers_for_entity(
        self, entity_type: EntityType, entity_id: int
    ) -> list[IdentifierEntity]:
        """Fetch all polymorphic identifiers for a specific entity."""
        stmt = (
            select(identifiers_table)
            .where(
                identifiers_table.c.entity_type == entity_type,
                identifiers_table.c.entity_id == entity_id,
            )
            .order_by(identifiers_table.c.id)
        )
        with self.get_connection() as conn:
            rows = conn.execute(stmt).fetchall()
            return [_row_to_identifier(r) for r in rows]

    def get_identifiers_for_entities(
        self, entity_type: EntityType | str, entity_ids: list[int]
    ) -> dict[int, list[IdentifierEntity]]:
        """Fetch all polymorphic identifiers for multiple entities of a given type in batch."""
        if not entity_ids:
            return {}
        result: dict[int, list[IdentifierEntity]] = {eid: [] for eid in entity_ids}
        chunk_size = 1000
        with self.get_connection() as conn:
            for i in range(0, len(entity_ids), chunk_size):
                chunk = entity_ids[i : i + chunk_size]
                stmt = (
                    select(identifiers_table)
                    .where(
                        identifiers_table.c.entity_type == entity_type,
                        identifiers_table.c.entity_id.in_(chunk),
                    )
                    .order_by(identifiers_table.c.id)
                )
                rows = conn.execute(stmt).fetchall()
                for r in rows:
                    ident = _row_to_identifier(r)
                    if ident.entity_id in result:
                        result[ident.entity_id].append(ident)
        return result

    def get_contributions_for_entity(
        self, entity_type: str, entity_id: int
    ) -> list[ContributionEntity]:
        """Fetch all contributions for a specific entity."""
        stmt = (
            select(contributions_table)
            .where(
                contributions_table.c.entity_type == entity_type,
                contributions_table.c.entity_id == entity_id,
            )
            .order_by(contributions_table.c.is_primary.desc(), contributions_table.c.id.asc())
        )
        with self.get_connection() as conn:
            rows = conn.execute(stmt).fetchall()
            return [_row_to_contribution(r) for r in rows]

    def get_contributions_for_entities(
        self, entity_specs: list[tuple[str, int]]
    ) -> dict[tuple[str, int], list[ContributionEntity]]:
        """Fetch all contributions for multiple (entity_type, entity_id) pairs in batch."""
        if not entity_specs:
            return {}
        result: dict[tuple[str, int], list[ContributionEntity]] = {
            spec: [] for spec in entity_specs
        }
        by_type: dict[str, list[int]] = {}
        for etype, eid in entity_specs:
            by_type.setdefault(etype, []).append(eid)

        with self.get_connection() as conn:
            for etype, eids in by_type.items():
                chunk_size = 1000
                for i in range(0, len(eids), chunk_size):
                    chunk = eids[i : i + chunk_size]
                    stmt = (
                        select(contributions_table)
                        .where(
                            contributions_table.c.entity_type == etype,
                            contributions_table.c.entity_id.in_(chunk),
                        )
                        .order_by(
                            contributions_table.c.is_primary.desc(),
                            contributions_table.c.id.asc(),
                        )
                    )
                    rows = conn.execute(stmt).fetchall()
                    for r in rows:
                        c = _row_to_contribution(r)
                        key = (c.entity_type, c.entity_id)
                        if key in result:
                            result[key].append(c)
        return result

    def lookup_by_identifier(
        self, scheme: str, value: str, authoritative_only: bool = False
    ) -> list[tuple[EntityType, int]]:
        """Look up entity types and IDs carrying an identifier, optionally authoritative only."""
        cache_key = (scheme.lower().strip(), value.strip().lower(), authoritative_only)
        if cache_key in self._identifier_lookup_cache:
            self._identifier_lookup_cache.move_to_end(cache_key)
            return self._identifier_lookup_cache[cache_key]

        res = self.lookup_by_identifiers_batch(
            [(scheme, value)], authoritative_only=authoritative_only
        )
        ans = res.get((scheme, value), [])
        return ans

    def lookup_by_identifiers_batch(
        self, pairs: list[tuple[str, str]], authoritative_only: bool = False
    ) -> dict[tuple[str, str], list[tuple[EntityType, int]]]:
        """Look up entity types and IDs for multiple (scheme, value) pairs in bulk."""
        if not pairs:
            return {}

        results: dict[tuple[str, str], list[tuple[EntityType, int]]] = {}
        needed_pairs: list[tuple[str, str]] = []

        for pair in pairs:
            scheme, val = pair
            cache_key = (scheme.lower().strip(), val.strip().lower(), authoritative_only)
            if cache_key in self._identifier_lookup_cache:
                self._identifier_lookup_cache.move_to_end(cache_key)
                results[pair] = self._identifier_lookup_cache[cache_key]
            else:
                needed_pairs.append(pair)
                results[pair] = []

        if not needed_pairs:
            return results

        conds = []
        source_map: dict[str, list[tuple[str, str, str, tuple[str, str]]]] = defaultdict(list)
        for scheme, val in needed_pairs:
            src = scheme.lower().strip()
            if src == "wikidata_qid":
                src = "wikidata"
            val_clean = val.strip()
            norm_val = val_clean.lower()
            alnum_val = re.sub(r"[^0-9a-zA-Z]", "", val_clean).lower()
            source_map[src].append((val_clean, norm_val, alnum_val, (scheme, val)))

        for src, item_list in source_map.items():
            norm_vals = {norm_val for _, norm_val, _, _ in item_list}
            alnum_vals = {alnum_val for _, _, alnum_val, _ in item_list if alnum_val}
            clean_vals = {clean_val for clean_val, _, _, _ in item_list}
            combined_norms = list(norm_vals | alnum_vals)
            conds.append(
                and_(
                    identifiers_table.c.source == src,
                    or_(
                        identifiers_table.c.source_id_normalized.in_(combined_norms),
                        identifiers_table.c.source_id.in_(list(clean_vals)),
                    ),
                )
            )

        if not conds:
            return results

        stmt = select(
            identifiers_table.c.source,
            identifiers_table.c.source_id,
            identifiers_table.c.source_id_normalized,
            identifiers_table.c.entity_type,
            identifiers_table.c.entity_id,
        ).where(or_(*conds))

        allowed = set(self.authoritative_sources()) if authoritative_only else None
        if not allowed:
            allowed = None

        with self.get_read_connection() as conn:
            rows = conn.execute(stmt).mappings().fetchall()

            valid_candidates: set[tuple[str, int]] = set()
            if allowed is not None and rows:
                table_map = {
                    "work": works_table,
                    "works": works_table,
                    "expression": expressions_table,
                    "expressions": expressions_table,
                    "manifestation": manifestations_table,
                    "manifestations": manifestations_table,
                    "item": items_table,
                    "items": items_table,
                    "agent": agents_table,
                    "agents": agents_table,
                }
                for etype, tbl in table_map.items():
                    eids = [r["entity_id"] for r in rows if r["entity_type"] == etype]
                    if eids:
                        check_stmt = select(tbl.c.id, tbl.c.record_origin).where(tbl.c.id.in_(eids))
                        for sr in conn.execute(check_stmt).fetchall():
                            if sr[1] in allowed:
                                valid_candidates.add((etype, sr[0]))

        for r in rows:
            etype = r["entity_type"]
            eid = r["entity_id"]
            if allowed is not None and (etype, eid) not in valid_candidates:
                continue

            src = r["source"]
            s_id = r["source_id"]
            s_norm = r["source_id_normalized"]

            candidates_for_src = source_map.get(src, [])
            for val_clean, norm_val, alnum_val, orig_pair in candidates_for_src:
                if s_norm in (norm_val, alnum_val) or s_id == val_clean:
                    if (etype, eid) not in results[orig_pair]:
                        results[orig_pair].append((etype, eid))

        for pair in needed_pairs:
            scheme, val = pair
            cache_key = (scheme.lower().strip(), val.strip().lower(), authoritative_only)
            self._identifier_lookup_cache[cache_key] = results[pair]
            if len(self._identifier_lookup_cache) > self.entity_cache_size:
                self._identifier_lookup_cache.popitem(last=False)

        return results

    def get_mediums_of_performance(self, work_id: int) -> list[EntitySubject]:
        """Fetch medium_of_performance EntitySubject records for a given work."""
        stmt = select(entity_subjects_table).where(
            entity_subjects_table.c.entity_type == "work",
            entity_subjects_table.c.entity_id == work_id,
            entity_subjects_table.c.relation_type == "medium_of_performance",
        )
        with self.get_connection() as conn:
            rows = conn.execute(stmt).mappings().fetchall()
            return [EntitySubject.model_validate(dict(r)) for r in rows]

    def find_works_by_author_and_title(
        self,
        author_key: Optional[str],
        title_stem: str,
        work_type: Optional[str] = None,
        authoritative_only: bool = True,
    ) -> list[WorkEntity]:
        """Look up works by title stem and optional author key, from authoritative sources."""
        clean_title = title_stem.strip().lower()
        if not clean_title:
            return []

        auth_sources = self.authoritative_sources() if authoritative_only else []

        def attempt(match_type: str, pattern: str) -> list[WorkEntity]:
            with self.get_connection() as conn:
                stmt = select(works_table).select_from(
                    works_table.outerjoin(
                        agents_table, works_table.c.primary_agent_id == agents_table.c.id
                    )
                )
                if match_type == "eq":
                    stmt = stmt.where(works_table.c.title_normalized == pattern)
                elif match_type == "prefix":
                    stmt = stmt.where(works_table.c.title_normalized.like(f"{pattern}%"))
                else:
                    stmt = stmt.where(works_table.c.title_normalized.like(f"%{pattern}%"))

                if author_key:
                    clean_author = author_key.strip().lower()
                    stmt = stmt.where(
                        or_(
                            works_table.c.primary_agent_id.is_(None),
                            agents_table.c.surname.like(f"%{clean_author}%"),
                            agents_table.c.name_normalized.like(f"%{clean_author}%"),
                        )
                    )
                if work_type:
                    stmt = stmt.where(works_table.c.work_type == work_type)
                if authoritative_only and auth_sources:
                    stmt = stmt.where(works_table.c.record_origin.in_(auth_sources))
                stmt = stmt.limit(20)
                return [_row_to_work(r) for r in conn.execute(stmt).fetchall()]

        for mtype in ("eq", "prefix", "substr"):
            found = attempt(mtype, clean_title)
            if found:
                return found
        return []

    def find_works_by_type(
        self,
        work_type: str,
        limit: int = 50,
        authoritative_only: bool = True,
    ) -> list[WorkEntity]:
        """Fetch works of a given work_type from authoritative sources."""
        auth_sources = self.authoritative_sources() if authoritative_only else []
        stmt = select(works_table).where(works_table.c.work_type == work_type)
        if authoritative_only and auth_sources:
            stmt = stmt.where(works_table.c.record_origin.in_(auth_sources))
        stmt = stmt.limit(limit)
        with self.get_connection() as conn:
            rows = conn.execute(stmt).fetchall()
            return [_row_to_work(r) for r in rows]

    def get_indexed_source_ids(self, source: str) -> set[str]:
        """Return set of manifestation source_ids stored and indexed for a given source."""
        stmt = select(manifestations_table.c.source_id).where(
            manifestations_table.c.source == source
        )
        with self.get_connection() as conn:
            return {row for row in conn.execute(stmt).scalars() if row}

    # -----------------------------------------------------------------------
    # Database-Level PostgreSQL Full-Text Search (tsvector/tsquery Ranking)
    #
    # NOTE ON ARCHITECTURAL DISTINCTION:
    # This module provides database-level full-text search against PostgreSQL tables
    def count_records_by_origin(self) -> dict[str, dict[str, int]]:
        """Return count breakdown of authority vs target records across WEMI tiers."""
        tier_tables = {
            "works": works_table,
            "expressions": expressions_table,
            "manifestations": manifestations_table,
            "items": items_table,
            "agents": agents_table,
        }
        counts: dict[str, dict[str, int]] = {}
        with self.get_connection() as conn:
            for tier, tbl in tier_tables.items():
                counts[tier] = {"authority": 0, "target": 0}
                stmt = select(tbl.c.record_origin, func.count().label("cnt")).group_by(
                    tbl.c.record_origin
                )
                for orig, cnt in conn.execute(stmt):
                    if orig in counts[tier]:
                        counts[tier][orig] = cnt
        return counts

    def check_source_registry(self) -> list[str]:
        """Report mismatches between the `sources` registry and the stored rows.

        With one database, "which rows may be matched against" is answered
        entirely by `record_origin` plus this registry. Both halves have to
        agree, and when they do not the failure is silent and total: an empty
        allow-list filters every candidate out, so matching returns nothing
        rather than erroring. Two ways to get there:

        - a source registered authoritative whose rows carry a different origin
          (or which has no rows at all), so it contributes nothing to match
          against;
        - rows whose `record_origin` is in no registry entry, which are
          therefore invisible to any authoritative-only search.

        Returns human-readable findings, empty when the two agree.
        """
        findings: list[str] = []
        registered = self.get_sources()
        with self.get_connection() as conn:
            stmt = union(
                select(distinct(works_table.c.record_origin)),
                select(distinct(manifestations_table.c.record_origin)),
                select(distinct(agents_table.c.record_origin)),
            )
            origins = {str(r[0]) for r in conn.execute(stmt) if r[0]}

        for name, is_authoritative in sorted(registered.items()):
            if is_authoritative and name not in origins:
                findings.append(
                    f"source '{name}' is registered authoritative but no rows carry that "
                    f"record_origin, so it cannot be matched against"
                )

        # An unregistered origin is only hidden when *some* source is registered
        # authoritative: `_origin_clause` applies no filter at all when the
        # allow-list would be empty, so with nothing authoritative every row
        # stays visible. Reporting invisibility then would be crying wolf.
        if any(registered.values()):
            for origin in sorted(o for o in origins if o not in registered and o != "unknown"):
                findings.append(
                    f"rows carry record_origin '{origin}', which is in no sources entry, so "
                    f"they are invisible to authoritative-only matching"
                )
        elif origins:
            findings.append(
                "no source is registered authoritative, so authoritative-only matching has no "
                "allow-list to apply and searches every source, corpora included"
            )

        return findings

    def delete_source(self, source_name: str) -> dict[str, int]:
        """Remove every row a source contributed, leaving other sources untouched.

        Backs `ingest_mode: replace`. Five tables carry `record_origin` - works,
        expressions, manifestations, items, agents - and everything else hangs off
        them by id. The polymorphic tables (`identifiers`, `entity_keys`,
        `contributions`) carry no foreign key, because `entity_type`/`entity_id`
        can name any tier and cannot be expressed as a single foreign key constraint, so
        their rows have to be deleted explicitly or they outlive their owners.

        Returns per-table counts so a caller can report what it removed rather
        than claiming a clean slate it did not verify.
        """
        removed: dict[str, int] = {}
        with self.get_connection() as conn:

            def ids_for(table_obj: Table) -> list[int]:
                stmt = select(table_obj.c.id).where(table_obj.c.record_origin == source_name)
                return [int(r[0]) for r in conn.execute(stmt)]

            work_ids = ids_for(works_table)
            expression_ids = ids_for(expressions_table)
            manifestation_ids = ids_for(manifestations_table)
            item_ids = ids_for(items_table)
            agent_ids = ids_for(agents_table)

            def delete_in(table_obj: Table, column_name: str, values: list[int]) -> int:
                if not values:
                    return 0
                total = 0
                # Chunked to stay under variable limits on large sources.
                for start in range(0, len(values), 500):
                    chunk = values[start : start + 500]
                    col = getattr(table_obj.c, column_name)
                    stmt = delete(table_obj).where(col.in_(chunk))
                    cur = conn.execute(stmt)
                    total += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
                return total

            # Polymorphic children first, keyed by (entity_type, entity_id).
            for table_name, table_obj in (
                ("identifiers", identifiers_table),
                ("entity_keys", entity_keys_table),
                ("contributions", contributions_table),
            ):
                count = 0
                for entity_type, entity_ids in (
                    ("work", work_ids),
                    ("expression", expression_ids),
                    ("manifestation", manifestation_ids),
                    ("item", item_ids),
                    ("agent", agent_ids),
                ):
                    if not entity_ids:
                        continue
                    for start in range(0, len(entity_ids), 500):
                        chunk = entity_ids[start : start + 500]
                        stmt = delete(table_obj).where(
                            table_obj.c.entity_type == entity_type,
                            table_obj.c.entity_id.in_(chunk),
                        )
                        cur = conn.execute(stmt)
                        count += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
                removed[table_name] = count

            # Contributions also reference agents by agent_id, not entity_id.
            removed["contributions"] += delete_in(contributions_table, "agent_id", agent_ids)

            removed["work_vectors"] = delete_in(work_vectors_table, "work_id", work_ids)
            removed["manifestation_vectors"] = delete_in(
                manifestation_vectors_table, "manifestation_id", manifestation_ids
            )
            removed["manifestation_expressions"] = delete_in(
                manifestation_expressions_table, "manifestation_id", manifestation_ids
            )
            removed["work_relationships"] = delete_in(
                work_relationships_table, "source_work_id", work_ids
            ) + delete_in(work_relationships_table, "target_work_id", work_ids)

            # Then the entities themselves, children before parents.
            for table_name, table_obj in (
                ("items", items_table),
                ("manifestations", manifestations_table),
                ("expressions", expressions_table),
                ("works", works_table),
                ("agents", agents_table),
            ):
                stmt = delete(table_obj).where(table_obj.c.record_origin == source_name)
                cur = conn.execute(stmt)
                removed[table_name] = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0

        return removed

