"""WEMIRepository mediating access to PostgreSQL database and vector index."""

from __future__ import annotations

import json
import logging
import re
from collections import OrderedDict, defaultdict
from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

import numpy as np
from rapidfuzz import fuzz
from sqlalchemy import (
    Table,
    and_,
    delete,
    distinct,
    func,
    insert,
    nulls_last,
    or_,
    select,
    union,
    update,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert

from reconciliator.database.engine import get_engine, init_authority_db
from reconciliator.database.ops import calculate_batch_size, insert_batch, upsert_batch
from reconciliator.database.schema import (
    agent_attributes_table,
    agent_relationships_table,
    agents_table,
    changeset_operations_table,
    cluster_lifecycle_events_table,
    cluster_redirections_table,
    concept_mappings_table,
    concepts_table,
    contributions_table,
    corpus_agent_cluster_attributes_table,
    corpus_agent_cluster_members_table,
    corpus_agent_clusters_table,
    corpus_agent_relationships_table,
    corpus_constituent_linkages_table,
    corpus_expression_clusters_table,
    corpus_linkages_table,
    corpus_manifestation_clusters_table,
    corpus_work_clusters_table,
    corpus_work_relationships_table,
    entity_keys_table,
    entity_pins_table,
    entity_subjects_table,
    expressions_table,
    harvest_state_table,
    identifiers_table,
    items_table,
    manifestation_expressions_table,
    manifestation_vectors_table,
    manifestations_table,
    review_changesets_table,
    sammelband_bindings_table,
    sources_table,
    work_consolidations_table,
    work_relationships_table,
    work_vectors_table,
    works_table,
)
from reconciliator.indexer import VectorIndex, create_vector_engine
from reconciliator.models import (
    AgentCluster,
    AgentClusterAttribute,
    AgentClusterMember,
    AgentEntity,
    AgentRelationshipEntity,
    ChangesetOperation,
    ClusterLifecycleEvent,
    ClusterRedirection,
    ContributionEntity,
    CorpusConstituentLinkage,
    CorpusLinkage,
    CorpusWorkRelationship,
    DataReadinessSummary,
    EntityKey,
    EntitySubject,
    EntityType,
    ExpressionCluster,
    ExpressionEntity,
    HarvestState,
    IdentifierEntity,
    ItemEntity,
    ManifestationCluster,
    ManifestationEntity,
    ManifestationExpressionAssociation,
    MatchRecord,
    RecordOrigin,
    ReviewChangeset,
    SammelbandBinding,
    WorkCluster,
    WorkEntity,
    WorkRelationship,
)
from reconciliator.normalizers.linguistic import fold_label
from reconciliator.utils.curie import parse_curie, to_curie

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

    from reconciliator.config import RepositoryFTSConfig, VectorEngineConfig
    from reconciliator.embedding.embedder import SentenceEmbedder

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


class WEMIRepository:
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

    def insert_agents_batch(
        self,
        agents: list[AgentEntity],
        conn: Optional[Any] = None,
        chunk_size: Optional[int] = None,
    ) -> list[int]:
        """Insert or update AgentEntity records in batch with relational attribute upserts."""
        if not agents:
            return []

        # In-memory deduplication by (source, source_id)
        deduped_rows: dict[tuple[str, str], dict[str, Any]] = {}
        # Attribute accumulation: ((source, source_id), type, value)
        attr_accum: set[tuple[tuple[str, str], str, str]] = set()
        # Identifiers per agent: (source, source_id) -> list[IdentifierEntity]
        ident_accum: dict[tuple[str, str], list[IdentifierEntity]] = {}

        for agent in agents:
            key = (agent.source, agent.source_id)
            row = {
                "source": agent.source,
                "source_id": agent.source_id,
                "record_origin": agent.record_origin,
                "name": agent.name,
                "name_normalized": agent.name_normalized,
                "surname": agent.surname,
                "dates": agent.dates,
                "birth_year": agent.birth_year,
                "death_year": agent.death_year,
                "fuller_name": agent.fuller_name,
                "is_personal": agent.is_personal,
                "is_fictional": agent.is_fictional,
            }
            if key in deduped_rows:
                existing = deduped_rows[key]
                for field, val in row.items():
                    if val is not None:
                        existing[field] = val
            else:
                deduped_rows[key] = row

            # Accumulate qualifiers
            if agent.qualifiers:
                for q in agent.qualifiers:
                    if q:
                        attr_accum.add((key, "qualifier", q))
            # Accumulate variant names
            if agent.variant_names:
                for vn in agent.variant_names:
                    if vn:
                        attr_accum.add((key, "variant_name", vn))
            # Accumulate affiliations
            if agent.affiliations:
                for aff in agent.affiliations:
                    if aff:
                        attr_accum.add((key, "affiliation", aff))
            # Accumulate identifiers
            if agent.identifiers:
                ident_accum.setdefault(key, []).extend(agent.identifiers)

        rows = list(deduped_rows.values())
        id_map: dict[tuple[str, str], int] = {}

        def _exec(c: Any) -> None:
            # 1. Batch UPSERT into agents_table
            effective_chunk_size = chunk_size or calculate_batch_size(
                num_columns=len(agents_table.c)
            )
            for i in range(0, len(rows), effective_chunk_size):
                chunk = rows[i : i + effective_chunk_size]
                stmt = pg_insert(agents_table).values(chunk)
                stmt = stmt.on_conflict_do_update(
                    index_elements=["source", "source_id"],
                    set_={
                        "record_origin": stmt.excluded.record_origin,
                        "name": stmt.excluded.name,
                        "name_normalized": stmt.excluded.name_normalized,
                        "surname": func.coalesce(stmt.excluded.surname, agents_table.c.surname),
                        "dates": func.coalesce(stmt.excluded.dates, agents_table.c.dates),
                        "birth_year": func.coalesce(
                            stmt.excluded.birth_year, agents_table.c.birth_year
                        ),
                        "death_year": func.coalesce(
                            stmt.excluded.death_year, agents_table.c.death_year
                        ),
                        "fuller_name": func.coalesce(
                            stmt.excluded.fuller_name, agents_table.c.fuller_name
                        ),
                        "is_personal": stmt.excluded.is_personal,
                        "is_fictional": stmt.excluded.is_fictional,
                    },
                ).returning(agents_table.c.id, agents_table.c.source, agents_table.c.source_id)
                res = c.execute(stmt).fetchall()
                for r in res:
                    id_map[(r[1], r[2])] = r[0]

            # 2. Batch idempotent insert into agent_attributes_table
            if attr_accum:
                attr_rows = []
                for (src, sid), atype, val in attr_accum:
                    agent_id = id_map.get((src, sid))
                    if agent_id:
                        attr_rows.append(
                            {
                                "agent_id": agent_id,
                                "attribute_type": atype,
                                "value": val,
                            }
                        )
                effective_attr_chunk_size = chunk_size or calculate_batch_size(
                    num_columns=len(agent_attributes_table.c)
                )
                for i in range(0, len(attr_rows), effective_attr_chunk_size):
                    chunk = attr_rows[i : i + effective_attr_chunk_size]
                    stmt = pg_insert(agent_attributes_table).values(chunk).on_conflict_do_nothing()
                    c.execute(stmt)

            # 3. Batch insert identifiers
            to_insert_idents: list[IdentifierEntity] = []
            for key, idents in ident_accum.items():
                agent_id = id_map.get(key)
                if agent_id:
                    for ident in idents:
                        ident_copy = ident.model_copy() if hasattr(ident, "model_copy") else ident
                        ident_copy.entity_id = agent_id
                        ident_copy.entity_type = "agent"
                        to_insert_idents.append(ident_copy)
            if to_insert_idents:
                self.insert_identifiers_batch(to_insert_idents, conn=c, chunk_size=chunk_size)

        if conn is not None:
            _exec(conn)
        else:
            with self.get_connection() as c:
                _exec(c)

        # Resolve IDs for return
        return [id_map.get((a.source, a.source_id), 0) for a in agents]

    def insert_agent_attributes_batch(
        self,
        agent_id: int,
        attributes: list[tuple[str, str]],
        conn: Optional[Any] = None,
    ) -> None:
        """Batch insert agent attributes idempotently.

        Args:
            agent_id: The agent row ID.
            attributes: List of (attribute_type, value) tuples.
        """
        if not attributes:
            return
        rows = [{"agent_id": agent_id, "attribute_type": t, "value": v} for t, v in attributes]
        stmt = pg_insert(agent_attributes_table).values(rows).on_conflict_do_nothing()
        if conn is not None:
            conn.execute(stmt)
        else:
            with self.get_connection() as c:
                c.execute(stmt)

    def insert_agent_relationships_batch(
        self,
        relationships: list[AgentRelationshipEntity] | list[dict[str, Any]],
        conn: Optional[Any] = None,
        chunk_size: int = 1000,
    ) -> None:
        """Batch insert agent relationships (affiliations, pseudonyms, corporate hierarchy)."""
        if not relationships:
            return
        rows = [
            r.model_dump(exclude={"id"}, exclude_none=True) if hasattr(r, "model_dump") else r
            for r in relationships
        ]

        def _exec(c: Any) -> None:
            for i in range(0, len(rows), chunk_size):
                chunk = rows[i : i + chunk_size]
                stmt = pg_insert(agent_relationships_table).values(chunk)
                c.execute(stmt)

        if conn is not None:
            _exec(conn)
        else:
            with self.get_connection() as c:
                _exec(c)

    def insert_corpus_agent_relationships_batch(
        self,
        relationships: list[dict[str, Any]],
        conn: Optional[Any] = None,
        chunk_size: int = 1000,
    ) -> None:
        """Batch insert or ignore cluster-level agent relationships into corpus_agent_relationships_table."""
        if not relationships:
            return

        def _exec(c: Any) -> None:
            for i in range(0, len(relationships), chunk_size):
                chunk = relationships[i : i + chunk_size]
                stmt = (
                    pg_insert(corpus_agent_relationships_table)
                    .values(chunk)
                    .on_conflict_do_nothing()
                )
                c.execute(stmt)

        if conn is not None:
            _exec(conn)
        else:
            with self.get_corpus_connection() as c:
                _exec(c)

    def get_agent_relationships_map(
        self, agent_ids: list[int], conn: Optional[Any] = None
    ) -> dict[int, list[dict[str, Any]]]:
        """Retrieve all relationships involving the given agent IDs, indexed by agent ID."""
        if not agent_ids:
            return {}
        out: dict[int, list[dict[str, Any]]] = defaultdict(list)
        chunk_size = 1000
        target = conn if conn is not None else self.get_connection()
        for i in range(0, len(agent_ids), chunk_size):
            chunk = agent_ids[i : i + chunk_size]
            stmt = select(
                agent_relationships_table.c.source_agent_id,
                agent_relationships_table.c.target_agent_id,
                agent_relationships_table.c.target_name,
                agent_relationships_table.c.target_uri,
                agent_relationships_table.c.relationship_type,
            ).where(
                (agent_relationships_table.c.source_agent_id.in_(chunk))
                | (agent_relationships_table.c.target_agent_id.in_(chunk))
            )
            for r in target.execute(stmt).mappings():
                d = dict(r)
                src = d["source_agent_id"]
                tgt = d["target_agent_id"]
                if src in agent_ids:
                    out[src].append(d)
                if tgt and tgt in agent_ids:
                    out[tgt].append(d)
        return out

    def insert_agent_identifiers_batch(
        self,
        agent_id: int,
        identifiers: list[IdentifierEntity],
        conn: Optional[Any] = None,
    ) -> None:
        """Batch insert identifiers for an agent."""
        if not identifiers:
            return
        bound: list[IdentifierEntity] = []
        for ident in identifiers:
            ident.entity_id = agent_id
            ident.entity_type = "agent"
            bound.append(ident)
        self.insert_identifiers_batch(bound, conn=conn)

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

    def get_all_entity_keys(
        self, entity_type: str = "manifestation"
    ) -> dict[int, dict[str, list[str]]]:
        """Return key bags for every entity of a type, keyed by entity_id.

        Bulk form for the clusterer, which needs keys for the whole corpus to
        build its blocking index and would otherwise issue one query per record.
        """
        out: dict[int, dict[str, list[str]]] = {}
        stmt = (
            select(
                entity_keys_table.c.entity_id,
                entity_keys_table.c.key_type,
                entity_keys_table.c.key_value,
            )
            .where(entity_keys_table.c.entity_type == entity_type)
            .order_by(entity_keys_table.c.entity_id, entity_keys_table.c.id)
        )
        with self.get_connection() as conn:
            rows = conn.execute(stmt).mappings().fetchall()
        for row in rows:
            out.setdefault(row["entity_id"], {}).setdefault(row["key_type"], []).append(
                row["key_value"]
            )
        return out

    def get_all_match_records(self, origins: Optional[list[str]] = None) -> list[MatchRecord]:
        """Project every stored record back out of WEMI rows as MatchRecords."""
        stmt = (
            select(
                manifestations_table.c.id.label("manif_id"),
                manifestations_table.c.source.label("m_source"),
                manifestations_table.c.source_id,
                manifestations_table.c.title.label("m_title"),
                manifestations_table.c.publication_year,
                manifestations_table.c.volume_designation,
                manifestations_table.c.is_component,
                manifestations_table.c.serialized_text.label("m_serialized"),
                manifestations_table.c.record_origin,
                works_table.c.id.label("work_id"),
                works_table.c.source.label("w_source"),
                works_table.c.source_id.label("w_source_id"),
                works_table.c.title.label("w_title"),
                works_table.c.work_type,
                works_table.c.serialized_text.label("w_serialized"),
                expressions_table.c.source.label("e_source"),
                expressions_table.c.source_id.label("e_source_id"),
                expressions_table.c.language,
                expressions_table.c.content_type,
                expressions_table.c.serialized_text.label("e_serialized"),
            )
            .distinct(manifestations_table.c.id)
            .select_from(
                manifestations_table.outerjoin(
                    works_table, works_table.c.id == manifestations_table.c.work_id
                ).outerjoin(expressions_table, expressions_table.c.work_id == works_table.c.id)
            )
        )
        if origins:
            stmt = stmt.where(manifestations_table.c.record_origin.in_(origins))
        stmt = stmt.order_by(manifestations_table.c.id)

        with self.get_connection() as conn:
            rows = conn.execute(stmt).mappings().fetchall()

            key_bags = self.get_all_entity_keys("manifestation")

            ident_stmt = select(
                identifiers_table.c.entity_type,
                identifiers_table.c.entity_id,
                identifiers_table.c.source.label("scheme"),
                identifiers_table.c.source_id.label("value"),
            ).where(identifiers_table.c.entity_type.in_(("manifestation", "work", "expression")))
            ident_rows = conn.execute(ident_stmt).mappings().fetchall()

        ident_by_entity: dict[tuple[str, int], dict[str, list[str]]] = {}
        for r in ident_rows:
            bag = ident_by_entity.setdefault((r["entity_type"], r["entity_id"]), {})
            bucket = bag.setdefault(r["scheme"].lower(), [])
            if r["value"] not in bucket:
                bucket.append(r["value"])

        records: list[MatchRecord] = []
        for row in rows:
            keys = key_bags.get(row["manif_id"], {})
            identifiers: dict[str, list[str]] = {}
            for et, eid in (
                ("manifestation", row["manif_id"]),
                ("work", row["work_id"]),
                ("expression", None),
            ):
                if eid is None:
                    continue
                for scheme, values in ident_by_entity.get((et, eid), {}).items():
                    bucket = identifiers.setdefault(scheme, [])
                    for v in values:
                        if v not in bucket:
                            bucket.append(v)

            if row["source_id"]:
                bucket = identifiers.setdefault("control_number", [])
                if row["source_id"] not in bucket:
                    bucket.append(row["source_id"])

            title = row["m_title"] or row["w_title"] or ""
            records.append(
                MatchRecord(
                    record_id=row["source_id"],
                    format="wemi",
                    work_type=row["work_type"] or "text",
                    title=title,
                    uniform_title=(keys.get("uniform_title") or [None])[0],
                    language=(
                        None if (row["language"] or "").lower() == "und" else row["language"]
                    ),
                    author_key=(keys.get("author") or [None])[0],
                    title_stem=(keys.get("title_stem") or [None])[0] or title,
                    keys=keys,
                    identifiers=identifiers,
                    content_type=row["content_type"],
                    work_curie=(
                        to_curie(row["w_source"], row["w_source_id"]) if row["w_source"] else None
                    ),
                    expression_curie=(
                        to_curie(row["e_source"], row["e_source_id"]) if row["e_source"] else None
                    ),
                    manifestation_curie=(
                        to_curie(row["m_source"], row["source_id"]) if row["m_source"] else None
                    ),
                    year=row["publication_year"],
                    volume_designation=row["volume_designation"],
                    is_component=bool(row["is_component"]),
                    serialized_work=row["w_serialized"],
                    serialized_expression=row["e_serialized"],
                    serialized_manifestation=row["m_serialized"],
                )
            )
        return records

    def find_entities_by_key(
        self, key_type: str, key_value: str, entity_type: str = "manifestation"
    ) -> list[int]:
        """Return entity ids carrying a given key, for blocking lookups."""
        stmt = select(entity_keys_table.c.entity_id).where(
            entity_keys_table.c.entity_type == entity_type,
            entity_keys_table.c.key_type == key_type,
            entity_keys_table.c.key_value == key_value,
        )
        with self.get_connection() as conn:
            return list(conn.execute(stmt).scalars().all())

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
    # using tsvector, tsquery, and GIN indexes. Ranking is computed directly inside
    # PostgreSQL using ts_rank_cd (or configured ranking expression/weights from
    # RepositoryFTSConfig in storage.fts).
    #
    # This is distinct from:
    # 1) ReconcileFTSFallbackConfig (reconcile.fts_fallback in matcher.py):
    #    Governs the reconciliation matcher's funnel fallback policy (activation toggle,
    #    candidate limit, and confidence score assigned to tier3_fts_fallback).
    # 2) RerankConfig (reconcile.rerank in reconciliator.rerank.bm25):
    #    In-memory candidate evaluation in Python using the rank-bm25 library for
    #    candidate pair scoring, dense/sparse score fusion, and metadata discriminators.
    # -----------------------------------------------------------------------

    def _build_fts_query_and_order(
        self,
        query: str,
        table_name: str,
        limit: Optional[int] = None,
        ranking_function: Optional[str] = None,
        weights: Optional[list[float]] = None,
        prefix: bool = True,
    ) -> tuple[str, list[Any]]:
        """Construct FTS query string and ordering clause using PostgreSQL full-text search."""
        return "", []

    def search_works_fts(
        self,
        query: str,
        limit: Optional[int] = None,
        ranking_function: Optional[str] = None,
        weights: Optional[list[float]] = None,
        prefix_search: Optional[bool] = None,
        authoritative_only: bool = False,
        work_type: Optional[str] = None,
    ) -> list[tuple[WorkEntity, float]]:
        """Search works via PostgreSQL full-text search. Returns list of (WorkEntity, rank)."""
        clean_query = query.strip()
        if not clean_query:
            return []

        eff_limit = (
            limit
            if limit is not None
            else (self.fts_config.default_limit if self.fts_config else 20)
        )
        eff_prefix = (
            prefix_search
            if prefix_search is not None
            else (self.fts_config.prefix_search if self.fts_config else True)
        )

        parts = re.split(r"\s+OR\s+", clean_query, flags=re.IGNORECASE)
        or_clauses = []
        for part in parts:
            words = re.findall(r"\w+", part)
            if not words:
                continue
            clause = " & ".join(f"{w}:*" if eff_prefix else w for w in words)
            or_clauses.append(f"({clause})" if len(words) > 1 else clause)
        ts_query_str = " | ".join(or_clauses)
        if not ts_query_str:
            return []

        ts_query = func.to_tsquery("simple", ts_query_str)
        ts_vector = func.to_tsvector("simple", func.coalesce(works_table.c.serialized_text, ""))
        rank_col = func.ts_rank_cd(ts_vector, ts_query).label("rank")

        match_cond = ts_vector.op("@@")(ts_query)

        stmt = select(works_table, rank_col).where(match_cond)
        if authoritative_only:
            auth_sources = self.authoritative_sources()
            if auth_sources:
                stmt = stmt.where(works_table.c.record_origin.in_(auth_sources))
        if work_type:
            stmt = stmt.where(works_table.c.work_type == work_type)

        stmt = stmt.order_by(rank_col.desc()).limit(eff_limit)

        with self.get_connection() as conn:
            cursor = conn.execute(stmt).mappings()
            return [
                (_row_to_work(row), float(row["rank"]) if row["rank"] is not None else 0.0)
                for row in cursor
            ]

    search_works_lexical = search_works_fts

    # -----------------------------------------------------------------------
    # Dense Vector Search
    # -----------------------------------------------------------------------

    def search_work_vectors(
        self,
        query_vector: np.ndarray,
        top_k: int = 20,
        authoritative_only: bool = False,
        overfetch: int = 4,
    ) -> list[tuple[WorkEntity, float]]:
        """Retrieve top-k candidate works from the work vector index."""
        if len(query_vector.shape) == 1:
            q_mat = query_vector[np.newaxis, :]
        else:
            q_mat = query_vector
        batches = self.search_work_vectors_batch(
            q_mat, top_k=top_k, authoritative_only=authoritative_only, overfetch=overfetch
        )
        return batches[0] if batches else []

    def search_work_vectors_batch(
        self,
        query_vectors: np.ndarray,
        top_k: int = 20,
        authoritative_only: bool = False,
        overfetch: int = 4,
    ) -> list[list[tuple[WorkEntity, float]]]:
        """Retrieve top-k candidate works for a 2D batch of query vectors [N, dimension]."""
        if (
            self.work_indexer is None
            or self.work_indexer.total_vectors == 0
            or len(query_vectors) == 0
        ):
            return [[] for _ in range(len(query_vectors))]

        fetch_k = top_k * max(1, overfetch) if authoritative_only else top_k
        scores_matrix, indices_matrix = self.work_indexer.search(query_vectors, top_k=fetch_k)
        if len(scores_matrix) == 0 or len(indices_matrix) == 0:
            return [[] for _ in range(len(query_vectors))]

        allowed = set(self.authoritative_sources()) if authoritative_only else None
        if not allowed:
            allowed = None

        # Collect all unique vector indices across all queries
        all_vector_indices: set[int] = set()
        for row in indices_matrix:
            for v_id in row:
                if v_id >= 0:
                    all_vector_indices.add(int(v_id))

        vector_to_work_id: dict[int, int] = {}
        if all_vector_indices:
            with self.get_read_connection() as conn:
                stmt = select(work_vectors_table.c.vector_id, work_vectors_table.c.work_id).where(
                    work_vectors_table.c.vector_id.in_(list(all_vector_indices))
                )
                for r in conn.execute(stmt):
                    vector_to_work_id[r[0]] = r[1]

        # Bulk fetch/cache works
        needed_work_ids = [
            vector_to_work_id[v_id] for v_id in all_vector_indices if v_id in vector_to_work_id
        ]
        works_by_id = self.get_works_by_ids(needed_work_ids)

        batch_results: list[list[tuple[WorkEntity, float]]] = []
        for scores_row, indices_row in zip(scores_matrix, indices_matrix):
            results: list[tuple[WorkEntity, float]] = []
            seen_work_ids: set[int] = set()
            for v_id, score in zip(indices_row.tolist(), scores_row.tolist()):
                work_id = vector_to_work_id.get(v_id, v_id if v_id >= 0 else None)
                if work_id is not None and work_id not in seen_work_ids:
                    seen_work_ids.add(work_id)
                    work = works_by_id.get(work_id)
                    if work and (allowed is None or work.record_origin in allowed):
                        results.append((work, float(score)))
                        if len(results) >= top_k:
                            break
            batch_results.append(results)

        return batch_results

    def search_manifestation_vectors(
        self,
        query_vector: np.ndarray,
        top_k: int = 20,
        authoritative_only: bool = False,
        overfetch: int = 4,
    ) -> list[tuple[ManifestationEntity, float]]:
        """Retrieve top-k candidate manifestations from the manifestation vector index."""
        if self.manifestation_indexer is None or self.manifestation_indexer.total_vectors == 0:
            return []

        # Over-fetch when a filter will be applied: the index cannot filter, so
        # asking for exactly top_k and discarding afterwards silently returns
        # fewer candidates than requested - or none, when a large source fills
        # the whole budget.
        fetch_k = top_k * max(1, overfetch) if authoritative_only else top_k
        scores, indices = self.manifestation_indexer.search(query_vector, top_k=fetch_k)
        if len(scores) == 0 or len(indices) == 0:
            return []

        allowed = set(self.authoritative_sources()) if authoritative_only else None
        if not allowed:
            allowed = None

        vector_indices = indices[0].tolist()
        dense_scores = scores[0].tolist()

        vector_to_manif_id: dict[int, int] = {}
        with self.get_connection() as conn:
            stmt = select(
                manifestation_vectors_table.c.vector_id,
                manifestation_vectors_table.c.manifestation_id,
            ).where(manifestation_vectors_table.c.vector_id.in_(vector_indices))
            for r in conn.execute(stmt):
                vector_to_manif_id[r[0]] = r[1]

        results: list[tuple[ManifestationEntity, float]] = []
        seen_manif_ids: set[int] = set()
        for v_id, score in zip(vector_indices, dense_scores):
            manif_id = vector_to_manif_id.get(v_id, v_id if v_id >= 0 else None)
            if manif_id is not None and manif_id not in seen_manif_ids:
                seen_manif_ids.add(manif_id)
                manif = self.get_manifestation(manif_id)
                if manif and (allowed is None or manif.record_origin in allowed):
                    results.append((manif, float(score)))
                    if len(results) >= top_k:
                        break

        return results

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

    # -----------------------------------------------------------------------
    # Harvest State & Checkpoints
    # -----------------------------------------------------------------------

    def get_harvest_state(self, source_id: str) -> Optional[HarvestState]:
        """Fetch harvest checkpoint from harvest_state."""
        stmt = select(harvest_state_table).where(harvest_state_table.c.source_id == source_id)
        with self.get_connection() as conn:
            row = conn.execute(stmt).fetchone()
            return _row_to_harvest_state(row) if row else None

    def save_harvest_state(self, state: HarvestState) -> None:
        """Upsert a harvest checkpoint in harvest_state."""
        completed_slices_json = (
            json.dumps([list(s) for s in state.completed_slices])
            if state.completed_slices
            else None
        )
        stmt = pg_insert(harvest_state_table).values(
            source_id=state.source_id,
            source_type=state.source_type,
            metadata_prefix=state.metadata_prefix,
            set_spec=state.set_spec,
            last_harvest_from=state.last_harvest_from,
            last_harvest_until=state.last_harvest_until,
            resumption_token=state.resumption_token,
            records_harvested=state.records_harvested,
            status=state.status,
            completed_slices=completed_slices_json,
            updated_at=func.now(),
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[harvest_state_table.c.source_id],
            set_={
                "source_type": stmt.excluded.source_type,
                "metadata_prefix": stmt.excluded.metadata_prefix,
                "set_spec": stmt.excluded.set_spec,
                "last_harvest_from": stmt.excluded.last_harvest_from,
                "last_harvest_until": stmt.excluded.last_harvest_until,
                "resumption_token": stmt.excluded.resumption_token,
                "records_harvested": stmt.excluded.records_harvested,
                "status": stmt.excluded.status,
                "completed_slices": stmt.excluded.completed_slices,
                "updated_at": func.now(),
            },
        )
        with self.get_connection() as conn:
            conn.execute(stmt)

    def insert_wikidata_authors(
        self,
        authors: list[dict[str, Any]],
        use_synchronous_commit_off: bool = True,
    ) -> None:
        """Persist author entity metadata into agents table with relational attributes.

        Wikidata-specific fields (occupations, fields_of_work, affiliations) are
        stored in agent_attributes_table. Identifiers go into identifiers_table.
        Uses RETURNING id, source_id to eliminate secondary SELECT roundtrips.
        """
        if not authors:
            return
        deduped_agent_rows: dict[tuple[str, str], dict[str, Any]] = {}
        # Accumulate attributes: (qid, type, value) -> count
        attr_accum: dict[tuple[str, str, str], int] = {}

        for a in authors:
            qid = a.get("qid")
            if not qid:
                continue

            key = ("wikidata", qid)
            row = {
                "source": "wikidata",
                "source_id": qid,
                "record_origin": "wikidata",
                "name": a.get("name") or qid,
                "name_normalized": (a.get("name") or qid).strip().lower(),
                "surname": a.get("family_name"),
                "birth_year": a.get("birth_year"),
                "death_year": a.get("death_year"),
                "is_personal": True,
            }
            if key in deduped_agent_rows:
                existing = deduped_agent_rows[key]
                for field, val in row.items():
                    if val is not None:
                        existing[field] = val
            else:
                deduped_agent_rows[key] = row

            # Accumulate occupation attributes
            for occ in a.get("occupations") or []:
                attr_accum[(qid, "occupation", occ)] = (
                    attr_accum.get((qid, "occupation", occ), 0) + 1
                )
            # Accumulate field_of_work attributes
            for fow in a.get("fields_of_work") or []:
                attr_accum[(qid, "field_of_work", fow)] = (
                    attr_accum.get((qid, "field_of_work", fow), 0) + 1
                )
            # Accumulate affiliation attributes
            for aff in a.get("affiliations") or []:
                attr_accum[(qid, "affiliation", aff)] = (
                    attr_accum.get((qid, "affiliation", aff), 0) + 1
                )
            # Accumulate qualifier attributes
            for q in a.get("qualifiers") or []:
                attr_accum[(qid, "qualifier", q)] = attr_accum.get((qid, "qualifier", q), 0) + 1
            # Accumulate variant_name attributes
            for vn in a.get("variant_names") or []:
                attr_accum[(qid, "variant_name", vn)] = (
                    attr_accum.get((qid, "variant_name", vn), 0) + 1
                )

        agent_rows = list(deduped_agent_rows.values())
        with self.get_connection() as conn:
            if use_synchronous_commit_off:
                conn.exec_driver_sql("SET LOCAL synchronous_commit = 'off'")

            qid_to_agent_id: dict[str, int] = {}
            if agent_rows:
                agent_chunk_size = calculate_batch_size(num_columns=len(agent_rows[0]))
                for i in range(0, len(agent_rows), agent_chunk_size):
                    chunk = agent_rows[i : i + agent_chunk_size]
                    stmt = pg_insert(agents_table).values(chunk)
                    stmt = stmt.on_conflict_do_update(
                        index_elements=[agents_table.c.source, agents_table.c.source_id],
                        set_={
                            "name": stmt.excluded.name,
                            "name_normalized": stmt.excluded.name_normalized,
                            "surname": func.coalesce(stmt.excluded.surname, agents_table.c.surname),
                            "birth_year": func.coalesce(
                                stmt.excluded.birth_year, agents_table.c.birth_year
                            ),
                            "death_year": func.coalesce(
                                stmt.excluded.death_year, agents_table.c.death_year
                            ),
                        },
                    ).returning(agents_table.c.id, agents_table.c.source_id)
                    inserted = conn.execute(stmt).fetchall()
                    for r in inserted:
                        qid_to_agent_id[r[1]] = r[0]

            # Persist relational author attributes into agent_attributes table
            if qid_to_agent_id and attr_accum:
                attr_dict: dict[tuple[int, str, str], int] = {}
                for (qid, attr_type, value), count in attr_accum.items():
                    aid = qid_to_agent_id.get(qid)
                    if aid:
                        attr_dict[(aid, attr_type, value)] = (
                            attr_dict.get((aid, attr_type, value), 0) + count
                        )

                if attr_dict:
                    attr_rows = [
                        {
                            "agent_id": aid,
                            "attribute_type": atype,
                            "value": val,
                        }
                        for (aid, atype, val) in attr_dict.keys()
                    ]
                    insert_batch(
                        bind=conn,
                        table=agent_attributes_table,
                        rows=attr_rows,
                        on_conflict_do_nothing=True,
                    )

            # Persist relational author identifiers into identifiers table
            if qid_to_agent_id:
                ident_rows = []
                for a in authors:
                    qid = a.get("qid")
                    if not qid or qid not in qid_to_agent_id:
                        continue
                    agent_id = qid_to_agent_id[qid]
                    idents = a.get("identifiers")
                    if isinstance(idents, dict):
                        for scheme, val in idents.items():
                            if val is None:
                                continue
                            val_str = str(val).strip()
                            if not val_str:
                                continue
                            src_name = scheme.lower()
                            if src_name == "wikidata_qid":
                                src_name = "wikidata"
                            norm_val = (
                                re.sub(r"[^0-9a-zA-Z]", "", val_str).lower() or val_str.lower()
                            )
                            ident_rows.append(
                                {
                                    "entity_type": "agent",
                                    "entity_id": agent_id,
                                    "source": src_name,
                                    "source_id": val_str,
                                    "source_id_normalized": norm_val,
                                }
                            )
                if ident_rows:
                    insert_batch(
                        bind=conn,
                        table=identifiers_table,
                        rows=ident_rows,
                        on_conflict_do_nothing=True,
                    )

    def get_wikidata_authors_batch(self, qids: list[str]) -> dict[str, dict[str, Any]]:
        """Fetch cached author entities from agents + identifiers + agent_attributes by QIDs."""
        if not qids:
            return {}
        with self.get_connection() as conn:
            # 1. Query agents table
            stmt = select(
                agents_table.c.id,
                agents_table.c.source_id,
                agents_table.c.name,
                agents_table.c.surname,
                agents_table.c.birth_year,
                agents_table.c.death_year,
            ).where(
                agents_table.c.source == "wikidata",
                agents_table.c.source_id.in_(qids),
            )
            rows = conn.execute(stmt).mappings().fetchall()

            results: dict[str, dict[str, Any]] = {}
            agent_id_map: dict[str, int] = {}

            for row in rows:
                qid = row["source_id"]
                agent_id_map[qid] = row["id"]
                results[qid] = {
                    "qid": qid,
                    "name": row["name"],
                    "family_name": row["surname"],
                    "identifiers": {},
                    "birth_year": row["birth_year"],
                    "death_year": row["death_year"],
                    "occupations": [],
                    "fields_of_work": [],
                    "affiliations": [],
                    "qualifiers": [],
                    "variant_names": [],
                }

            # 2. Fetch agent_attributes for occupations, fields_of_work, affiliations, qualifiers, variant_names
            if agent_id_map:
                attr_stmt = select(
                    agent_attributes_table.c.agent_id,
                    agent_attributes_table.c.attribute_type,
                    agent_attributes_table.c.value,
                ).where(agent_attributes_table.c.agent_id.in_(list(agent_id_map.values())))
                attr_rows = conn.execute(attr_stmt).mappings().fetchall()
                rev_map = {aid: qid for qid, aid in agent_id_map.items()}
                for r in attr_rows:
                    qid = rev_map.get(r["agent_id"])
                    if qid and qid in results:
                        atype = r["attribute_type"]
                        val = r["value"]
                        if atype == "occupation":
                            if val not in results[qid]["occupations"]:
                                results[qid]["occupations"].append(val)
                        elif atype == "field_of_work":
                            if val not in results[qid]["fields_of_work"]:
                                results[qid]["fields_of_work"].append(val)
                        elif atype == "affiliation":
                            if val not in results[qid]["affiliations"]:
                                results[qid]["affiliations"].append(val)
                        elif atype == "qualifier":
                            if val not in results[qid]["qualifiers"]:
                                results[qid]["qualifiers"].append(val)
                        elif atype == "variant_name":
                            if val not in results[qid]["variant_names"]:
                                results[qid]["variant_names"].append(val)

            # 3. Fetch identifiers for each agent
            if agent_id_map:
                id_stmt = select(
                    identifiers_table.c.entity_id,
                    identifiers_table.c.source,
                    identifiers_table.c.source_id,
                ).where(
                    identifiers_table.c.entity_type == "agent",
                    identifiers_table.c.entity_id.in_(list(agent_id_map.values())),
                )
                id_rows = conn.execute(id_stmt).fetchall()
                rev_map = {aid: qid for qid, aid in agent_id_map.items()}
                for r in id_rows:
                    qid = rev_map.get(r[0])
                    if qid and qid in results:
                        results[qid]["identifiers"][r[1]] = r[2]

            return results

    def get_wikidata_author(self, qid: str) -> dict[str, Any] | None:
        """Fetch a single cached author entity from agents + identifiers by QID."""
        result = self.get_wikidata_authors_batch([qid])
        return result.get(qid)

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

    def drop_search_indexes(self) -> None:
        """Drop GIN and full-text search indexes before bulk ingest."""
        indexes_to_drop = [
            "idx_works_title_trgm",
            "idx_agents_name_trgm",
            "idx_manifestations_title_trgm",
            "idx_works_fts",
            "idx_expressions_fts",
            "idx_manifestations_fts",
        ]
        with self.get_connection() as conn:
            for idx in indexes_to_drop:
                try:
                    conn.exec_driver_sql(f"DROP INDEX IF EXISTS {idx};")
                except Exception as exc:
                    logger.debug("Could not drop index %s (may not exist): %s", idx, exc)

    def rebuild_search_indexes(self) -> None:
        """Re-create GIN and full-text search indexes after bulk ingest."""
        from reconciliator.database.schema import PG_SEARCH_DDL_STATEMENTS

        with self.get_connection() as conn:
            for stmt in PG_SEARCH_DDL_STATEMENTS:
                try:
                    conn.exec_driver_sql(stmt)
                except Exception as exc:
                    logger.warning("Failed to execute search DDL '%s': %s", stmt, exc)

    def build_wemi_indexes(
        self,
        embedder: SentenceEmbedder,
        work_indexer: Optional[VectorIndex] = None,
        manifestation_indexer: Optional[VectorIndex] = None,
        batch_size: int = 128,
        include_constituent_works: bool = False,
    ) -> dict[str, int]:
        """Embed stored works and manifestations into their vector indexes.

        Two tiers, each for a different consumer: the work index serves the
        matcher's tier-2 semantic fallback, the manifestation index serves the
        clusterer's near-duplicate detection. There is no expression tier - it
        added no discrimination these two do not already provide.

        Records are embedded from their stored `serialized_text`, which the
        loaders render at ingest, so building an index never re-parses a record.
        Rows without serialized text are skipped rather than embedded blank.

        Constituent works - an album's tracks, an anthology's stories - are
        excluded by default, and the measurement is the argument. On a corpus of
        200 albums of 12 tracks they are **82%** of the work index, and a
        title-only query returns six tracks and no album at all:

            query 'Finlandia'
               1. constituent 'Finlandia nro 4'
               2. constituent 'Finlandia nro 3'
               ...

        Tier 3 takes the top *k* candidates, so for a target album with a short
        or common title the real album candidates are pushed out of the window
        entirely - a recall regression in album matching caused by indexing
        things that are not albums. Constituents are reachable exactly anyway:
        by `analytic_title` key, and by `get_constituent_works` from a matched
        container.

        A source that reconciles *track-level* targets wants the opposite, so
        this is a flag (`constituents.index_works`) rather than a rule. The test
        for "is this only a constituent" is having no manifestation of its own: a
        work nobody published separately is not something a record can match at
        the manifestation level, and a track that *was* released as a single is a
        row of its own with its own manifestation.
        """
        counts = {"works": 0, "manifestations": 0}

        for kind, indexer, table_obj, id_col_name, map_table_obj in (
            ("works", work_indexer, works_table, "work_id", work_vectors_table),
            (
                "manifestations",
                manifestation_indexer,
                manifestations_table,
                "manifestation_id",
                manifestation_vectors_table,
            ),
        ):
            if indexer is None:
                continue

            cond = and_(
                table_obj.c.serialized_text.is_not(None),
                func.trim(table_obj.c.serialized_text) != "",
            )
            if kind == "works" and not include_constituent_works:
                m_exists = select(1).where(
                    manifestations_table.c.work_id == work_relationships_table.c.target_work_id
                )
                subq = select(work_relationships_table.c.target_work_id).where(
                    work_relationships_table.c.relationship_type == "has_constituent",
                    ~m_exists.exists(),
                )
                cond = and_(cond, table_obj.c.id.not_in(subq))

            stmt = (
                select(table_obj.c.id, table_obj.c.serialized_text)
                .where(cond)
                .order_by(table_obj.c.id)
            )

            with self.get_connection() as conn:
                rows = conn.execute(stmt).fetchall()
                for i in range(0, len(rows), batch_size):
                    batch = rows[i : i + batch_size]
                    ids = [int(r[0]) for r in batch]
                    texts = [str(r[1]) for r in batch]
                    if hasattr(embedder, "encode_batch_dense"):
                        vectors = embedder.encode_batch_dense(texts)
                    else:
                        vectors = embedder.embed_texts(texts, batch_size=len(texts))
                    vector_ids = indexer.add_vectors(vectors, ids=ids)
                    values_to_insert = [
                        {"vector_id": v_id, id_col_name: ent_id}
                        for v_id, ent_id in zip(vector_ids, ids)
                    ]
                    map_stmt = pg_insert(map_table_obj).values(values_to_insert)
                    map_stmt = map_stmt.on_conflict_do_update(
                        index_elements=[map_table_obj.c.vector_id],
                        set_={id_col_name: getattr(map_stmt.excluded, id_col_name)},
                    )
                    conn.execute(map_stmt)
                    counts[kind] += len(ids)
            indexer.save()

        return counts

    def build_agent_index(
        self,
        agent_indexer: VectorIndex,
        embedder: SentenceEmbedder,
        batch_size: int = 128,
        template_path: Optional[Path | str] = None,
    ) -> int:
        """Build dedicated agent vector index (agent.index) using concise essential serialization."""
        from reconciliator.embedding.template import WEMISerializationEngine

        wemi_engine = WEMISerializationEngine(agent_template_path=template_path)

        stmt = select(
            agents_table.c.id,
            agents_table.c.source,
            agents_table.c.source_id,
            agents_table.c.name,
            agents_table.c.name_normalized,
            agents_table.c.surname,
            agents_table.c.dates,
            agents_table.c.birth_year,
            agents_table.c.death_year,
            agents_table.c.fuller_name,
        ).order_by(agents_table.c.id)

        with self.get_connection() as conn:
            rows = conn.execute(stmt).mappings().fetchall()

            total_indexed = 0
            for i in range(0, len(rows), batch_size):
                batch = rows[i : i + batch_size]
                ids: list[int] = []
                texts: list[str] = []
                for r in batch:
                    agent_dict = dict(r)
                    text = wemi_engine.render_agent(agent_dict)
                    ids.append(agent_dict["id"])
                    texts.append(text)

                if texts:
                    if hasattr(embedder, "encode_batch_dense"):
                        vectors = embedder.encode_batch_dense(texts)
                    else:
                        vectors = embedder.embed_texts(texts, batch_size=len(texts))
                    agent_indexer.add_vectors(vectors, ids=ids)
                    total_indexed += len(texts)

        agent_indexer.save()
        return total_indexed

    # -----------------------------------------------------------------------
    # Relational Disambiguation Evidence (ported from AuthorityStore)
    # -----------------------------------------------------------------------

    def get_relational_disambiguation_evidence(
        self,
        candidate_agent_ids: list[int],
        target_coauthors: list[str],
        target_publisher: Optional[str] = None,
        target_classifications: Optional[list[tuple[str, str]]] = None,
    ) -> dict[int, dict[str, Any]]:
        """Execute targeted SQL queries on WEMI contributions/agents/manifestations/works

        to check co-author graph overlap, publisher overlap, and scheme-agnostic classification overlap.
        """
        evidence: dict[int, dict[str, Any]] = {
            cid: {
                "has_coauthor_match": False,
                "matched_coauthors": [],
                "has_publisher_match": False,
                "matched_publishers": [],
                "has_classification_match": False,
                "matched_classifications": [],
            }
            for cid in candidate_agent_ids
        }
        if not candidate_agent_ids:
            return evidence

        norm_target_coauthors = {fold_label(ca) for ca in target_coauthors if ca and fold_label(ca)}
        norm_target_pub = fold_label(target_publisher) if target_publisher else ""
        norm_target_classes = {
            f"{scheme.strip().lower()}:{code.strip().lower()}"
            for code, scheme in (target_classifications or [])
            if code and scheme
        }

        with self.get_connection() as conn:
            for cid in candidate_agent_ids:
                # Get contributions for this agent
                stmt = select(
                    contributions_table.c.entity_type,
                    contributions_table.c.entity_id,
                ).where(contributions_table.c.agent_id == cid)
                contribs = conn.execute(stmt).fetchall()

                work_ids = [c[1] for c in contribs if c[0] == "work"]
                manif_ids = [c[1] for c in contribs if c[0] == "manifestation"]
                expr_ids = [c[1] for c in contribs if c[0] == "expression"]

                # Contributions land on whichever tier the relator implies, so the
                # tiers have to be walked in both directions before evidence is
                # gathered: a work-level author still needs its manifestations'
                # publishers, and a manifestation-level one still needs its work.
                if work_ids:
                    manif_stmt = select(manifestations_table.c.id).where(
                        manifestations_table.c.work_id.in_(work_ids)
                    )
                    for row in conn.execute(manif_stmt):
                        if row[0] not in manif_ids:
                            manif_ids.append(row[0])
                if manif_ids:
                    manif_work_stmt = select(distinct(manifestations_table.c.work_id)).where(
                        manifestations_table.c.id.in_(manif_ids),
                        manifestations_table.c.work_id.is_not(None),
                    )
                    for row in conn.execute(manif_work_stmt):
                        if row[0] not in work_ids:
                            work_ids.append(row[0])
                if expr_ids:
                    expr_work_stmt = select(distinct(expressions_table.c.work_id)).where(
                        expressions_table.c.id.in_(expr_ids),
                        expressions_table.c.work_id.is_not(None),
                    )
                    for row in conn.execute(expr_work_stmt):
                        if row[0] not in work_ids:
                            work_ids.append(row[0])

                if norm_target_coauthors and (work_ids or manif_ids or expr_ids):
                    # Co-contributors are collected across every tier the candidate
                    # touches, since roles are split between work and expression.
                    conds = []
                    for entity_type, ids in (
                        ("work", work_ids),
                        ("expression", expr_ids),
                        ("manifestation", manif_ids),
                    ):
                        if ids:
                            conds.append(
                                and_(
                                    contributions_table.c.entity_type == entity_type,
                                    contributions_table.c.entity_id.in_(ids),
                                )
                            )
                    if conds:
                        co_stmt = (
                            select(distinct(agents_table.c.name), agents_table.c.name_normalized)
                            .select_from(
                                contributions_table.join(
                                    agents_table,
                                    contributions_table.c.agent_id == agents_table.c.id,
                                )
                            )
                            .where(or_(*conds), contributions_table.c.agent_id != cid)
                        )
                        co_agents = conn.execute(co_stmt).mappings().fetchall()
                        for co in co_agents:
                            c_norm = fold_label(co["name_normalized"] or co["name"])
                            for t_norm in norm_target_coauthors:
                                if (
                                    t_norm in c_norm
                                    or c_norm in t_norm
                                    or fuzz.token_set_ratio(t_norm, c_norm) >= 85.0
                                ):
                                    evidence[cid]["has_coauthor_match"] = True
                                    if co["name"] not in evidence[cid]["matched_coauthors"]:
                                        evidence[cid]["matched_coauthors"].append(co["name"])

                if manif_ids and norm_target_pub:
                    pub_stmt = select(distinct(manifestations_table.c.publisher)).where(
                        manifestations_table.c.id.in_(manif_ids),
                        manifestations_table.c.publisher.is_not(None),
                        manifestations_table.c.publisher != "",
                    )
                    pubs = conn.execute(pub_stmt).scalars().all()
                    for pb in pubs:
                        p_norm = fold_label(pb)
                        if p_norm and (
                            p_norm in norm_target_pub
                            or norm_target_pub in p_norm
                            or fuzz.token_set_ratio(norm_target_pub, p_norm) >= 80.0
                        ):
                            evidence[cid]["has_publisher_match"] = True
                            if pb not in evidence[cid]["matched_publishers"]:
                                evidence[cid]["matched_publishers"].append(pb)

                if work_ids and norm_target_classes:
                    class_stmt = select(
                        distinct(works_table.c.classification), works_table.c.classification_scheme
                    ).where(
                        works_table.c.id.in_(work_ids),
                        works_table.c.classification.is_not(None),
                        works_table.c.classification != "",
                    )
                    classes = conn.execute(class_stmt).all()
                    for cl, cl_scheme in classes:
                        code = str(cl).strip().lower()
                        cl_scheme_norm = (cl_scheme or "").strip().lower()
                        for tc in norm_target_classes:
                            t_scheme, _, t_code = tc.partition(":")
                            t_scheme_norm = t_scheme.strip().lower()
                            if (
                                not t_scheme_norm
                                or not cl_scheme_norm
                                or t_scheme_norm == cl_scheme_norm
                            ):
                                if code.startswith(t_code) or t_code.startswith(code):
                                    evidence[cid]["has_classification_match"] = True
                                    matched_str = f"{cl_scheme_norm or t_scheme_norm}:{code}"
                                    if matched_str not in evidence[cid]["matched_classifications"]:
                                        evidence[cid]["matched_classifications"].append(matched_str)

                    # Also check classifications in entity_subjects
                    subj_stmt = select(
                        distinct(entity_subjects_table.c.concept_id),
                        entity_subjects_table.c.vocabulary,
                    ).where(
                        entity_subjects_table.c.entity_type == "work",
                        entity_subjects_table.c.entity_id.in_(work_ids),
                        entity_subjects_table.c.concept_id.is_not(None),
                    )
                    for code_val, scheme_val in conn.execute(subj_stmt):
                        code = str(code_val or "").strip().lower()
                        scheme = str(scheme_val or "").strip().lower()
                        if not code:
                            continue
                        for tc in norm_target_classes:
                            t_scheme, _, t_code = tc.partition(":")
                            if t_scheme == scheme:
                                if code.startswith(t_code) or t_code.startswith(code):
                                    evidence[cid]["has_classification_match"] = True
                                    matched_str = f"{scheme}:{code}"
                                    if matched_str not in evidence[cid]["matched_classifications"]:
                                        evidence[cid]["matched_classifications"].append(matched_str)

        return evidence

    # -----------------------------------------------------------------------
    # Index Persistence
    # -----------------------------------------------------------------------

    def save_indices(self, metadata: Optional[dict[str, Any]] = None) -> None:
        """Persist all active vector indices to disk."""
        if self.work_indexer is not None:
            self.work_indexer.save(metadata=metadata)
        if self.manifestation_indexer is not None:
            self.manifestation_indexer.save(metadata=metadata)
        if hasattr(self, "agent_indexer") and self.agent_indexer is not None:
            self.agent_indexer.save(metadata=metadata)

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

    def insert_agent_clusters_batch(self, clusters: list[AgentCluster]) -> None:
        """Batch insert or replace agent identity clusters."""
        if not clusters:
            return
        deduped = {
            c.cluster_id: {
                "cluster_id": c.cluster_id,
                "canonical_agent_id": c.canonical_agent_id,
                "canonical_agent_source": c.canonical_agent_source,
                "canonical_agent_source_id": c.canonical_agent_source_id,
                "canonical_name": c.canonical_name,
                "canonical_surname": c.canonical_surname,
                "canonical_uri": c.canonical_uri,
                "birth_year": c.birth_year,
                "death_year": c.death_year,
                "member_count": c.member_count,
                "work_count": c.work_count,
                "manifestation_count": c.manifestation_count,
                "confidence_score": c.confidence_score,
            }
            for c in clusters
        }
        upsert_batch(
            bind=self.get_corpus_connection(),
            table=corpus_agent_clusters_table,
            rows=list(deduped.values()),
            conflict_index_elements=["cluster_id"],
        )

    def insert_agent_cluster_members_batch(self, members: list[AgentClusterMember]) -> None:
        """Batch insert or replace agent cluster members."""
        if not members:
            return
        deduped = {
            m.agent_id: {
                "agent_id": m.agent_id,
                "cluster_id": m.cluster_id,
                "source": m.source,
                "source_id": m.source_id,
                "link_method": m.link_method,
                "confidence": m.confidence,
            }
            for m in members
        }
        upsert_batch(
            bind=self.get_corpus_connection(),
            table=corpus_agent_cluster_members_table,
            rows=list(deduped.values()),
            conflict_index_elements=["agent_id"],
        )

    def insert_agent_cluster_attributes_batch(
        self, attributes: list[AgentClusterAttribute]
    ) -> None:
        """Batch insert or update agent cluster aggregated attributes."""
        if not attributes:
            return
        rows = [
            {
                "cluster_id": a.cluster_id,
                "attribute_type": a.attribute_type,
                "value": a.value,
                "concept_id": a.concept_id,
                "frequency": a.frequency,
                "is_canonical": a.is_canonical,
            }
            for a in attributes
        ]
        upsert_batch(
            bind=self.get_corpus_connection(),
            table=corpus_agent_cluster_attributes_table,
            rows=rows,
            conflict_index_elements=["cluster_id", "attribute_type", "value"],
        )

    def get_agent_cluster(self, cluster_id: str) -> Optional[AgentCluster]:
        """Fetch agent cluster by cluster_id with members and aggregated attributes."""
        with self.get_corpus_connection() as conn:
            row = (
                conn.execute(
                    select(corpus_agent_clusters_table).where(
                        corpus_agent_clusters_table.c.cluster_id == cluster_id
                    )
                )
                .mappings()
                .first()
            )
            if not row:
                return None
            cluster = AgentCluster(**dict(row))
            m_rows = (
                conn.execute(
                    select(corpus_agent_cluster_members_table).where(
                        corpus_agent_cluster_members_table.c.cluster_id == cluster_id
                    )
                )
                .mappings()
                .fetchall()
            )
            cluster.members = [AgentClusterMember(**dict(m)) for m in m_rows]
            a_rows = (
                conn.execute(
                    select(corpus_agent_cluster_attributes_table).where(
                        corpus_agent_cluster_attributes_table.c.cluster_id == cluster_id
                    )
                )
                .mappings()
                .fetchall()
            )
            cluster.attributes = [AgentClusterAttribute(**dict(a)) for a in a_rows]
            return cluster

    def get_agent_cluster_members(self, cluster_id: str) -> list[AgentClusterMember]:
        """Fetch member records of an agent cluster."""
        with self.get_corpus_connection() as conn:
            rows = (
                conn.execute(
                    select(corpus_agent_cluster_members_table).where(
                        corpus_agent_cluster_members_table.c.cluster_id == cluster_id
                    )
                )
                .mappings()
                .fetchall()
            )
            return [AgentClusterMember(**dict(r)) for r in rows]

    def get_agent_cluster_attributes(self, cluster_id: str) -> list[AgentClusterAttribute]:
        """Fetch aggregated attributes of an agent cluster."""
        with self.get_corpus_connection() as conn:
            rows = (
                conn.execute(
                    select(corpus_agent_cluster_attributes_table).where(
                        corpus_agent_cluster_attributes_table.c.cluster_id == cluster_id
                    )
                )
                .mappings()
                .fetchall()
            )
            return [AgentClusterAttribute(**dict(r)) for r in rows]

    def get_canonical_agent_uri(self, agent_id: int) -> Optional[str]:
        """Get canonical URI for an agent_id through cluster membership."""
        with self.get_corpus_connection() as conn:
            stmt = (
                select(corpus_agent_clusters_table.c.canonical_uri)
                .select_from(
                    corpus_agent_cluster_members_table.join(
                        corpus_agent_clusters_table,
                        corpus_agent_cluster_members_table.c.cluster_id
                        == corpus_agent_clusters_table.c.cluster_id,
                    )
                )
                .where(corpus_agent_cluster_members_table.c.agent_id == agent_id)
            )
            val = conn.execute(stmt).scalar_one_or_none()
            return str(val) if val is not None else None

    def get_agent_cluster_id(self, agent_id: int) -> Optional[str]:
        """Get cluster_id for an agent_id through cluster membership."""
        with self.get_corpus_connection() as conn:
            stmt = select(corpus_agent_cluster_members_table.c.cluster_id).where(
                corpus_agent_cluster_members_table.c.agent_id == agent_id
            )
            val = conn.execute(stmt).scalar_one_or_none()
            return str(val) if val is not None else None

    def find_records_by_key(self, key_type: str, key_value: str) -> list[str]:
        """Find record ids (manifestation source_ids) carrying a key.

        Reads `entity_keys` rather than the removed `target_keys` staging table.
        """
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

    # =======================================================================
    # Data Readiness & System State Inspection
    # =======================================================================

    def get_data_readiness_summary(self) -> DataReadinessSummary:
        """Inspect and report counts across entities, vocabularies, clusters, and vector indices."""
        counts: dict[str, int] = {}
        tables_to_count = {
            "sources": sources_table,
            "works": works_table,
            "expressions": expressions_table,
            "manifestations": manifestations_table,
            "items": items_table,
            "agents": agents_table,
            "concepts": concepts_table,
            "concept_mappings": concept_mappings_table,
            "work_consolidations": work_consolidations_table,
            "agent_clusters": corpus_agent_clusters_table,
            "corpus_linkages": corpus_linkages_table,
            "changesets": review_changesets_table,
            "pins": entity_pins_table,
        }

        with self.get_connection() as conn:
            for name, tbl in tables_to_count.items():
                try:
                    c = conn.execute(select(func.count()).select_from(tbl)).scalar() or 0
                    counts[name] = int(c)
                except Exception as e:
                    logger.debug("Failed to count table %s: %s", name, e)
                    counts[name] = 0

        # Vector store introspection
        work_idx = getattr(self, "work_indexer", None)
        manif_idx = getattr(self, "manifestation_indexer", None)
        agent_idx = getattr(self, "agent_indexer", None)

        work_vecs = work_idx.total_vectors if work_idx else 0
        manif_vecs = manif_idx.total_vectors if manif_idx else 0
        agent_vecs = agent_idx.total_vectors if agent_idx else 0
        has_vectors = (work_vecs + manif_vecs + agent_vecs) > 0

        vector_info = {
            "available": has_vectors,
            "work_vectors": work_vecs,
            "manifestation_vectors": manif_vecs,
            "agent_vectors": agent_vecs,
        }

        # Pipeline stages readiness assessment
        stages: dict[str, dict[str, Any]] = {
            "vocabularies": {
                "count": counts.get("concepts", 0),
                "ready": counts.get("concepts", 0) > 0,
                "status": "ready" if counts.get("concepts", 0) > 0 else "empty",
                "message": (
                    f"{counts.get('concepts', 0):,} concepts loaded"
                    if counts.get("concepts", 0) > 0
                    else "0 concepts loaded. Run 'ingest-vocab' to load YSO/SLM."
                ),
            },
            "authorities": {
                "count": counts.get("works", 0),
                "ready": counts.get("works", 0) > 0,
                "status": "ready" if counts.get("works", 0) > 0 else "empty",
                "message": (
                    f"{counts.get('works', 0):,} works, {counts.get('agents', 0):,} agents ingested"
                    if counts.get("works", 0) > 0
                    else "0 authority records. Run 'ingest-oai' or 'ingest-wikidata'."
                ),
            },
            "vector_index": {
                "count": work_vecs,
                "ready": work_vecs > 0,
                "status": "ready" if work_vecs > 0 else "empty",
                "message": (
                    f"{work_vecs:,} works indexed into vector store"
                    if work_vecs > 0
                    else "Vector store empty. Run 'build-index' to enable semantic search."
                ),
            },
            "work_consolidation": {
                "count": counts.get("work_consolidations", 0),
                "ready": counts.get("work_consolidations", 0) > 0,
                "status": "ready" if counts.get("work_consolidations", 0) > 0 else "empty",
                "message": (
                    f"{counts.get('work_consolidations', 0):,} works consolidated"
                    if counts.get("work_consolidations", 0) > 0
                    else "No work consolidations. Run 'consolidate-works' to elect canonical works."
                ),
            },
            "agent_clustering": {
                "count": counts.get("agent_clusters", 0),
                "ready": counts.get("agent_clusters", 0) > 0,
                "status": "ready" if counts.get("agent_clusters", 0) > 0 else "empty",
                "message": (
                    f"{counts.get('agent_clusters', 0):,} agent clusters"
                    if counts.get("agent_clusters", 0) > 0
                    else "No agent clusters. Run 'cluster-agents' to link cross-source agents."
                ),
            },
            "corpus_reconciliation": {
                "count": counts.get("corpus_linkages", 0),
                "ready": counts.get("corpus_linkages", 0) > 0,
                "status": "ready" if counts.get("corpus_linkages", 0) > 0 else "empty",
                "message": (
                    f"{counts.get('corpus_linkages', 0):,} corpus records reconciled"
                    if counts.get("corpus_linkages", 0) > 0
                    else "No corpus records reconciled. Run 'reconcile' to process target catalogue."
                ),
            },
        }

        # Determine overall status
        if counts.get("works", 0) == 0 and counts.get("concepts", 0) == 0:
            overall = "unpopulated"
        elif counts.get("corpus_linkages", 0) > 0 and counts.get("work_consolidations", 0) > 0:
            overall = "ready"
        else:
            overall = "partially_populated"

        return DataReadinessSummary(
            database_connected=True,
            overall_readiness=overall,
            entity_counts=counts,
            vector_store=vector_info,
            pipeline_readiness=stages,
        )

    # =======================================================================
    # Review Changesets & Curation Staging Engine
    # =======================================================================

    def create_changeset(
        self,
        title: str,
        description: Optional[str],
        created_by: str,
        assigned_to: Optional[str] = None,
    ) -> ReviewChangeset:
        """Create a new staged review changeset."""
        stmt = (
            insert(review_changesets_table)
            .values(
                title=title,
                description=description,
                status="draft",
                created_by=created_by,
                assigned_to=assigned_to,
            )
            .returning(review_changesets_table)
        )
        with self.get_connection() as conn:
            row = conn.execute(stmt).mappings().fetchone()
            return ReviewChangeset.model_validate(_row_to_dict(row))

    def list_changesets(
        self,
        status: Optional[str] = None,
        created_by: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[ReviewChangeset], int]:
        """List review changesets with optional status or author filtering."""
        stmt = select(review_changesets_table)
        count_stmt = select(func.count()).select_from(review_changesets_table)

        if status:
            stmt = stmt.where(review_changesets_table.c.status == status)
            count_stmt = count_stmt.where(review_changesets_table.c.status == status)
        if created_by:
            stmt = stmt.where(review_changesets_table.c.created_by == created_by)
            count_stmt = count_stmt.where(review_changesets_table.c.created_by == created_by)

        stmt = (
            stmt.order_by(review_changesets_table.c.created_at.desc()).limit(limit).offset(offset)
        )

        with self.get_connection() as conn:
            total = conn.execute(count_stmt).scalar() or 0
            rows = conn.execute(stmt).mappings().fetchall()
            results: list[ReviewChangeset] = []
            for r in rows:
                cs = ReviewChangeset.model_validate(_row_to_dict(r))
                # Fetch count of operations for this changeset
                op_count_stmt = (
                    select(func.count())
                    .select_from(changeset_operations_table)
                    .where(changeset_operations_table.c.changeset_id == cs.id)
                )
                cs.operations_count = int(conn.execute(op_count_stmt).scalar() or 0)
                results.append(cs)
            return results, int(total)

    def get_changeset(self, changeset_id: int) -> Optional[ReviewChangeset]:
        """Fetch changeset by ID along with its staged operations."""
        stmt = select(review_changesets_table).where(review_changesets_table.c.id == changeset_id)
        with self.get_connection() as conn:
            row = conn.execute(stmt).mappings().fetchone()
            if not row:
                return None
            cs = ReviewChangeset.model_validate(_row_to_dict(row))

            ops_stmt = (
                select(changeset_operations_table)
                .where(changeset_operations_table.c.changeset_id == changeset_id)
                .order_by(changeset_operations_table.c.sequence.asc())
            )
            op_rows = conn.execute(ops_stmt).mappings().fetchall()
            operations: list[ChangesetOperation] = []
            for op_r in op_rows:
                d = _row_to_dict(op_r)
                if isinstance(d.get("parameters_json"), str):
                    try:
                        d["parameters"] = json.loads(d["parameters_json"])
                    except Exception:
                        d["parameters"] = {}
                elif isinstance(d.get("parameters"), dict):
                    pass
                else:
                    d["parameters"] = {}

                if isinstance(d.get("pre_image_json"), str):
                    try:
                        d["pre_image"] = json.loads(d["pre_image_json"])
                    except Exception:
                        d["pre_image"] = None

                operations.append(ChangesetOperation.model_validate(d))

            cs.operations = operations
            cs.operations_count = len(operations)
            return cs

    def add_changeset_operation(
        self,
        changeset_id: int,
        operation_type: str,
        entity_type: str,
        entity_id: str,
        target_id: Optional[str] = None,
        parameters: Optional[dict[str, Any]] = None,
        pre_image: Optional[dict[str, Any]] = None,
        notes: Optional[str] = None,
    ) -> ChangesetOperation:
        """Stage an operation inside a review changeset."""
        params_json = json.dumps(parameters or {}, ensure_ascii=False)
        pre_img_json = json.dumps(pre_image, ensure_ascii=False) if pre_image is not None else None

        with self.get_connection() as conn:
            seq_stmt = select(
                func.coalesce(func.max(changeset_operations_table.c.sequence), 0)
            ).where(changeset_operations_table.c.changeset_id == changeset_id)
            next_seq = int(conn.execute(seq_stmt).scalar() or 0) + 1

            stmt = (
                insert(changeset_operations_table)
                .values(
                    changeset_id=changeset_id,
                    sequence=next_seq,
                    operation_type=operation_type,
                    entity_type=entity_type,
                    entity_id=str(entity_id),
                    target_id=str(target_id) if target_id is not None else None,
                    parameters_json=params_json,
                    pre_image_json=pre_img_json,
                    notes=notes,
                )
                .returning(changeset_operations_table)
            )
            row = conn.execute(stmt).mappings().fetchone()
            d = _row_to_dict(row)
            d["parameters"] = parameters or {}
            d["pre_image"] = pre_image
            return ChangesetOperation.model_validate(d)

    def delete_changeset_operation(self, operation_id: int) -> bool:
        """Remove a staged operation from a changeset."""
        stmt = (
            delete(changeset_operations_table)
            .where(changeset_operations_table.c.id == operation_id)
            .returning(changeset_operations_table.c.id)
        )
        with self.get_connection() as conn:
            row = conn.execute(stmt).fetchone()
            return row is not None

    def commit_changeset(self, changeset_id: int, committed_by: str) -> ReviewChangeset:
        """Atomically apply all staged operations in a changeset, pinning decisions."""
        cs = self.get_changeset(changeset_id)
        if not cs:
            raise ValueError(f"Changeset {changeset_id} not found")
        if cs.status == "committed":
            raise ValueError(f"Changeset {changeset_id} is already committed")

        def _safe_upsert(c: Any, tbl: Table, keys: list[str], vals: dict[str, Any]) -> None:
            cond = and_(*[tbl.c[k] == vals[k] for k in keys])
            existing_val = c.execute(select(tbl.c[keys[0]]).where(cond)).scalar()
            if existing_val is not None:
                updates = {k: v for k, v in vals.items() if k not in keys}
                if updates:
                    c.execute(update(tbl).where(cond).values(**updates))
            else:
                c.execute(insert(tbl).values(**vals))

        with self.get_connection() as conn:
            for op in cs.operations:
                op_type = op.operation_type
                params = op.parameters

                if op_type == "work_merge":
                    # Merge works into an equivalence group
                    work_ids = [int(x) for x in params.get("work_ids", [op.entity_id])]
                    canonical_id = int(params.get("canonical_id", work_ids[0]))
                    for wid in work_ids:
                        # Update work_consolidations
                        _safe_upsert(
                            conn,
                            work_consolidations_table,
                            ["work_id"],
                            {
                                "work_id": wid,
                                "canonical_work_id": canonical_id,
                                "evidence": "manual_curation",
                            },
                        )
                        # Create pin
                        _safe_upsert(
                            conn,
                            entity_pins_table,
                            ["entity_type", "entity_id"],
                            {
                                "entity_type": "work_consolidation",
                                "entity_id": str(wid),
                                "changeset_id": changeset_id,
                                "pinned_by": committed_by,
                            },
                        )
                    # Add symmetric same_work_as relationships between all pairs
                    for i in range(len(work_ids)):
                        for j in range(i + 1, len(work_ids)):
                            for src, tgt in (
                                (work_ids[i], work_ids[j]),
                                (work_ids[j], work_ids[i]),
                            ):
                                rel_cond = and_(
                                    work_relationships_table.c.source_work_id == src,
                                    work_relationships_table.c.target_work_id == tgt,
                                    work_relationships_table.c.relationship_type == "same_work_as",
                                )
                                if (
                                    conn.execute(
                                        select(work_relationships_table.c.id).where(rel_cond)
                                    ).scalar()
                                    is None
                                ):
                                    conn.execute(
                                        insert(work_relationships_table).values(
                                            source_work_id=src,
                                            target_work_id=tgt,
                                            relationship_type="same_work_as",
                                        )
                                    )

                elif op_type == "work_split":
                    # Remove a work from an equivalence group
                    wid = int(op.entity_id)
                    # Delete same_work_as edges touching this work
                    conn.execute(
                        delete(work_relationships_table).where(
                            and_(
                                work_relationships_table.c.relationship_type == "same_work_as",
                                or_(
                                    work_relationships_table.c.source_work_id == wid,
                                    work_relationships_table.c.target_work_id == wid,
                                ),
                            )
                        )
                    )
                    # Remove from work_consolidations
                    conn.execute(
                        delete(work_consolidations_table).where(
                            work_consolidations_table.c.work_id == wid
                        )
                    )
                    # Add pin indicating work is pinned as singleton
                    _safe_upsert(
                        conn,
                        entity_pins_table,
                        ["entity_type", "entity_id"],
                        {
                            "entity_type": "work_consolidation",
                            "entity_id": str(wid),
                            "changeset_id": changeset_id,
                            "pinned_by": committed_by,
                        },
                    )

                elif op_type == "work_elect_canonical":
                    # Change canonical work for an existing group
                    canonical_id = int(op.target_id or op.entity_id)
                    member_ids = [int(x) for x in params.get("member_ids", [op.entity_id])]
                    for mid in member_ids:
                        conn.execute(
                            update(work_consolidations_table)
                            .where(work_consolidations_table.c.work_id == mid)
                            .values(canonical_work_id=canonical_id)
                        )
                        _safe_upsert(
                            conn,
                            entity_pins_table,
                            ["entity_type", "entity_id"],
                            {
                                "entity_type": "work_consolidation",
                                "entity_id": str(mid),
                                "changeset_id": changeset_id,
                                "pinned_by": committed_by,
                            },
                        )

                elif op_type == "agent_cluster_merge":
                    # Merge agent clusters
                    target_cluster_id = str(op.target_id or op.entity_id)
                    source_cluster_id = str(params.get("source_cluster_id", op.entity_id))
                    # Reassign members to target cluster
                    conn.execute(
                        update(corpus_agent_cluster_members_table)
                        .where(corpus_agent_cluster_members_table.c.cluster_id == source_cluster_id)
                        .values(cluster_id=target_cluster_id)
                    )
                    # Pin target cluster
                    _safe_upsert(
                        conn,
                        entity_pins_table,
                        ["entity_type", "entity_id"],
                        {
                            "entity_type": "agent_cluster",
                            "entity_id": target_cluster_id,
                            "changeset_id": changeset_id,
                            "pinned_by": committed_by,
                        },
                    )

                elif op_type == "agent_cluster_split":
                    # Detach member agent from cluster
                    agent_id = int(op.entity_id)
                    conn.execute(
                        delete(corpus_agent_cluster_members_table).where(
                            corpus_agent_cluster_members_table.c.agent_id == agent_id
                        )
                    )
                    _safe_upsert(
                        conn,
                        entity_pins_table,
                        ["entity_type", "entity_id"],
                        {
                            "entity_type": "agent_cluster_member",
                            "entity_id": str(agent_id),
                            "changeset_id": changeset_id,
                            "pinned_by": committed_by,
                        },
                    )

                elif op_type == "reconciliation_relink":
                    # Relink a target record to a different authority work / manifestation
                    record_id = str(op.entity_id)
                    new_work_uri = str(params.get("work_uri", ""))
                    new_exp_uri = str(params.get("expression_uri", ""))
                    conn.execute(
                        update(corpus_linkages_table)
                        .where(corpus_linkages_table.c.record_id == record_id)
                        .values(
                            final_work_source="manual",
                            final_work_source_id=new_work_uri,
                            final_expression_source="manual",
                            final_expression_source_id=new_exp_uri,
                        )
                    )
                    _safe_upsert(
                        conn,
                        entity_pins_table,
                        ["entity_type", "entity_id"],
                        {
                            "entity_type": "corpus_linkage",
                            "entity_id": record_id,
                            "changeset_id": changeset_id,
                            "pinned_by": committed_by,
                        },
                    )

                elif op_type == "reconciliation_reject":
                    # Reject match for target record
                    record_id = str(op.entity_id)
                    conn.execute(
                        update(corpus_linkages_table)
                        .where(corpus_linkages_table.c.record_id == record_id)
                        .values(
                            final_work_source="",
                            final_work_source_id="",
                            final_expression_source="",
                            final_expression_source_id="",
                        )
                    )
                    _safe_upsert(
                        conn,
                        entity_pins_table,
                        ["entity_type", "entity_id"],
                        {
                            "entity_type": "corpus_linkage",
                            "entity_id": record_id,
                            "changeset_id": changeset_id,
                            "pinned_by": committed_by,
                        },
                    )

            # Mark changeset as committed
            conn.execute(
                update(review_changesets_table)
                .where(review_changesets_table.c.id == changeset_id)
                .values(
                    status="committed",
                    committed_by=committed_by,
                    committed_at=func.current_timestamp(),
                    updated_at=func.current_timestamp(),
                )
            )

        return self.get_changeset(changeset_id) or cs

    def revert_changeset(self, changeset_id: int, reverted_by: str) -> ReviewChangeset:
        """Rollback a committed changeset using pre-images and release entity pins."""
        cs = self.get_changeset(changeset_id)
        if not cs:
            raise ValueError(f"Changeset {changeset_id} not found")
        if cs.status != "committed":
            raise ValueError(f"Changeset {changeset_id} is not committed (status: {cs.status})")

        with self.get_connection() as conn:
            # Delete pins created by this changeset
            conn.execute(
                delete(entity_pins_table).where(entity_pins_table.c.changeset_id == changeset_id)
            )

            # Restore pre-images in reverse sequence
            for op in reversed(cs.operations):
                pre = op.pre_image or {}
                if not pre:
                    continue
                if op.operation_type in ("work_merge", "work_split", "work_elect_canonical"):
                    # Restore original canonical work ID if available
                    orig_canon = pre.get("canonical_work_id")
                    if orig_canon is not None:
                        conn.execute(
                            update(work_consolidations_table)
                            .where(work_consolidations_table.c.work_id == int(op.entity_id))
                            .values(canonical_work_id=int(orig_canon))
                        )
                elif op.operation_type in ("reconciliation_relink", "reconciliation_reject"):
                    orig_w = pre.get("final_work_source_id", "")
                    orig_e = pre.get("final_expression_source_id", "")
                    conn.execute(
                        update(corpus_linkages_table)
                        .where(corpus_linkages_table.c.record_id == str(op.entity_id))
                        .values(
                            final_work_source_id=orig_w,
                            final_expression_source_id=orig_e,
                        )
                    )

            # Update changeset status
            conn.execute(
                update(review_changesets_table)
                .where(review_changesets_table.c.id == changeset_id)
                .values(
                    status="reverted",
                    updated_at=func.current_timestamp(),
                )
            )

        return self.get_changeset(changeset_id) or cs

    def get_pinned_entities(self, entity_type: Optional[str] = None) -> set[str]:
        """Fetch all entity IDs currently pinned by curators."""
        stmt = select(entity_pins_table.c.entity_id)
        if entity_type:
            stmt = stmt.where(entity_pins_table.c.entity_type == entity_type)
        with self.get_connection() as conn:
            return {str(r[0]) for r in conn.execute(stmt)}

    def is_entity_pinned(self, entity_type: str, entity_id: str) -> bool:
        """Check if an entity is pinned against automated pipeline modification."""
        stmt = (
            select(entity_pins_table.c.id)
            .where(
                and_(
                    entity_pins_table.c.entity_type == entity_type,
                    entity_pins_table.c.entity_id == str(entity_id),
                )
            )
            .limit(1)
        )
        with self.get_connection() as conn:
            return conn.execute(stmt).scalar() is not None

    # =======================================================================
    # Entity Browsing & Search Helpers for API
    # =======================================================================

    def get_works_paginated(
        self,
        query: Optional[str] = None,
        source: Optional[str] = None,
        work_type: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[WorkEntity], int]:
        """Fetch paginated works with optional text query and source filters."""
        stmt = select(works_table)
        count_stmt = select(func.count()).select_from(works_table)

        if source:
            stmt = stmt.where(works_table.c.record_origin == source)
            count_stmt = count_stmt.where(works_table.c.record_origin == source)
        if work_type:
            stmt = stmt.where(works_table.c.work_type == work_type)
            count_stmt = count_stmt.where(works_table.c.work_type == work_type)

        if query and query.strip():
            clean_q = query.strip()
            # Try fuzzy trigram or title search
            fuzzy_cond = works_table.c.title_normalized.ilike(f"%{clean_q.lower()}%")
            stmt = stmt.where(fuzzy_cond)
            count_stmt = count_stmt.where(fuzzy_cond)

        stmt = stmt.order_by(works_table.c.id.desc()).limit(limit).offset(offset)
        with self.get_connection() as conn:
            total = conn.execute(count_stmt).scalar() or 0
            rows = conn.execute(stmt).mappings().fetchall()
            return [_row_to_work(r) for r in rows], int(total)

    def get_manifestations_paginated(
        self,
        query: Optional[str] = None,
        source: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[ManifestationEntity], int]:
        """Fetch paginated manifestations."""
        stmt = select(manifestations_table)
        count_stmt = select(func.count()).select_from(manifestations_table)

        if source:
            stmt = stmt.where(manifestations_table.c.record_origin == source)
            count_stmt = count_stmt.where(manifestations_table.c.record_origin == source)
        if query and query.strip():
            clean_q = query.strip()
            cond = manifestations_table.c.title_normalized.ilike(f"%{clean_q.lower()}%")
            stmt = stmt.where(cond)
            count_stmt = count_stmt.where(cond)

        stmt = stmt.order_by(manifestations_table.c.id.desc()).limit(limit).offset(offset)
        with self.get_connection() as conn:
            total = conn.execute(count_stmt).scalar() or 0
            rows = conn.execute(stmt).mappings().fetchall()
            return [_row_to_manifestation(r) for r in rows], int(total)

    def get_agents_paginated(
        self,
        query: Optional[str] = None,
        source: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[AgentEntity], int]:
        """Fetch paginated agents."""
        stmt = select(agents_table)
        count_stmt = select(func.count()).select_from(agents_table)

        if source:
            stmt = stmt.where(agents_table.c.record_origin == source)
            count_stmt = count_stmt.where(agents_table.c.record_origin == source)
        if query and query.strip():
            clean_q = query.strip()
            cond = agents_table.c.name_normalized.ilike(f"%{clean_q.lower()}%")
            stmt = stmt.where(cond)
            count_stmt = count_stmt.where(cond)

        stmt = stmt.order_by(agents_table.c.id.desc()).limit(limit).offset(offset)
        with self.get_connection() as conn:
            total = conn.execute(count_stmt).scalar() or 0
            rows = conn.execute(stmt).mappings().fetchall()
            return [_row_to_agent(r) for r in rows], int(total)

    def get_consolidation_groups_paginated(
        self,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        """Fetch paginated work consolidation equivalence groups."""
        # Find distinct canonical_work_id groups
        sub = (
            select(
                work_consolidations_table.c.canonical_work_id,
                func.count(work_consolidations_table.c.work_id).label("member_count"),
            )
            .group_by(work_consolidations_table.c.canonical_work_id)
            .subquery()
        )

        stmt = (
            select(
                sub.c.canonical_work_id,
                sub.c.member_count,
                works_table.c.title,
                works_table.c.source,
                works_table.c.source_id,
                works_table.c.work_type,
            )
            .join(works_table, sub.c.canonical_work_id == works_table.c.id)
            .order_by(sub.c.member_count.desc(), sub.c.canonical_work_id.asc())
            .limit(limit)
            .offset(offset)
        )
        count_stmt = select(func.count()).select_from(sub)

        with self.get_connection() as conn:
            total = conn.execute(count_stmt).scalar() or 0
            rows = conn.execute(stmt).mappings().fetchall()
            results: list[dict[str, Any]] = []
            for r in rows:
                canon_id = int(r["canonical_work_id"])
                # Fetch members
                members_stmt = (
                    select(
                        works_table.c.id,
                        works_table.c.title,
                        works_table.c.source,
                        works_table.c.source_id,
                        work_consolidations_table.c.evidence,
                    )
                    .join(works_table, work_consolidations_table.c.work_id == works_table.c.id)
                    .where(work_consolidations_table.c.canonical_work_id == canon_id)
                )
                member_rows = conn.execute(members_stmt).mappings().fetchall()
                results.append(
                    {
                        "canonical_work_id": canon_id,
                        "canonical_title": r["title"],
                        "canonical_source": r["source"],
                        "canonical_source_id": r["source_id"],
                        "work_type": r["work_type"],
                        "member_count": int(r["member_count"]),
                        "members": [dict(m) for m in member_rows],
                    }
                )
            return results, int(total)

    def get_consolidation_group_graph(self, canonical_work_id: int) -> dict[str, Any]:
        """Fetch Cytoscape-formatted graph data for a work consolidation equivalence group."""
        with self.get_connection() as conn:
            # Fetch members of this canonical group
            members_stmt = (
                select(
                    works_table.c.id,
                    works_table.c.title,
                    works_table.c.source,
                    works_table.c.source_id,
                    works_table.c.work_type,
                    work_consolidations_table.c.evidence,
                )
                .join(works_table, work_consolidations_table.c.work_id == works_table.c.id)
                .where(work_consolidations_table.c.canonical_work_id == canonical_work_id)
            )
            member_rows = conn.execute(members_stmt).mappings().fetchall()
            if not member_rows:
                # Check if this work exists as a singleton
                w = self.get_work(canonical_work_id)
                if not w:
                    return {"nodes": [], "edges": []}
                return {
                    "nodes": [
                        {
                            "data": {
                                "id": str(w.id),
                                "label": w.title,
                                "source": w.source,
                                "source_id": w.source_id,
                                "is_canonical": True,
                                "work_type": w.work_type,
                            }
                        }
                    ],
                    "edges": [],
                }

            member_ids = [int(m["id"]) for m in member_rows]
            nodes = [
                {
                    "data": {
                        "id": str(m["id"]),
                        "label": m["title"],
                        "source": m["source"],
                        "source_id": m["source_id"],
                        "is_canonical": int(m["id"]) == canonical_work_id,
                        "work_type": m["work_type"],
                        "evidence": m["evidence"],
                    }
                }
                for m in member_rows
            ]

            # Fetch same_work_as edges
            edges_stmt = select(work_relationships_table).where(
                and_(
                    work_relationships_table.c.relationship_type == "same_work_as",
                    work_relationships_table.c.source_work_id.in_(member_ids),
                    work_relationships_table.c.target_work_id.in_(member_ids),
                )
            )
            edge_rows = conn.execute(edges_stmt).mappings().fetchall()
            seen_edges: set[tuple[int, int]] = set()
            edges = []
            for er in edge_rows:
                src, tgt = int(er["source_work_id"]), int(er["target_work_id"])
                edge_key = (min(src, tgt), max(src, tgt))
                if edge_key in seen_edges:
                    continue
                seen_edges.add(edge_key)
                edges.append(
                    {
                        "data": {
                            "id": f"edge_{src}_{tgt}",
                            "source": str(src),
                            "target": str(tgt),
                            "relationship": "same_work_as",
                        }
                    }
                )

            return {"nodes": nodes, "edges": edges}

    def get_agent_clusters_paginated(
        self,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[AgentCluster], int]:
        """Fetch paginated agent clusters."""
        stmt = (
            select(corpus_agent_clusters_table)
            .order_by(corpus_agent_clusters_table.c.member_count.desc())
            .limit(limit)
            .offset(offset)
        )
        count_stmt = select(func.count()).select_from(corpus_agent_clusters_table)
        with self.get_connection() as conn:
            total = conn.execute(count_stmt).scalar() or 0
            rows = conn.execute(stmt).mappings().fetchall()
            return [AgentCluster.model_validate(dict(r)) for r in rows], int(total)

    def get_reconciliation_records_paginated(
        self,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        """Fetch paginated reconciled corpus records with their WEMI linkage status."""
        stmt = (
            select(corpus_linkages_table)
            .order_by(corpus_linkages_table.c.record_id.asc())
            .limit(limit)
            .offset(offset)
        )
        count_stmt = select(func.count()).select_from(corpus_linkages_table)
        with self.get_corpus_connection() as conn:
            total = conn.execute(count_stmt).scalar() or 0
            rows = conn.execute(stmt).mappings().fetchall()
            return [dict(r) for r in rows], int(total)

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

    def get_agent_cluster_full(self, cluster_id: str) -> Optional[AgentCluster]:
        """Fetch agent cluster with all member records and aggregated attributes."""
        cluster = self.get_agent_cluster(cluster_id)
        if not cluster:
            return None
        cluster.members = self.get_agent_cluster_members(cluster_id)
        cluster.attributes = self.get_agent_cluster_attributes(cluster_id)
        return cluster

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
