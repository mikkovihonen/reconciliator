"""WEMIRepository mediating access to SQLite authority.db, corpus database, and vector index."""

from __future__ import annotations

import json
import logging
import re
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
    nulls_last,
    or_,
    select,
    union,
    update,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert

from reconciliator.curie import parse_curie, to_curie
from reconciliator.db import (
    ConnectionAdapter,
    agents_table,
    contributions_table,
    corpus_expression_clusters_table,
    corpus_linkages_table,
    corpus_manifestation_clusters_table,
    corpus_work_clusters_table,
    entity_keys_table,
    entity_subjects_table,
    expressions_table,
    get_engine,
    harvest_state_table,
    identifiers_table,
    init_authority_db,
    items_table,
    manifestation_expressions_table,
    manifestation_vectors_table,
    manifestations_table,
    sammelband_bindings_table,
    sources_table,
    work_medium_of_performance_table,
    work_relationships_table,
    work_vectors_table,
    works_table,
)
from reconciliator.indexer import VectorIndex, create_vector_engine
from reconciliator.models import (
    AgentEntity,
    ContributionEntity,
    CorpusLinkage,
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
    MediumOfPerformance,
    RecordOrigin,
    SammelbandBinding,
    WorkCluster,
    WorkEntity,
    WorkRelationship,
)
from reconciliator.normalizers.linguistic import fold_label

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

    from reconciliator.config import RepositoryFTSConfig, VectorEngineConfig
    from reconciliator.embedder import SentenceEmbedder

logger = logging.getLogger(__name__)


def _hydrate_agent_profile(row_dict: dict[str, Any]) -> dict[str, Any]:
    """Hydrate intrinsic count distributions, orcid, and variant names from metadata_json."""
    meta: dict[str, Any] = {}
    if row_dict.get("metadata_json"):
        try:
            meta = json.loads(row_dict["metadata_json"])
        except Exception:
            meta = {}
    row_dict["qualifier_counts"] = meta.get("qualifier_counts", {})
    row_dict["affiliation_counts"] = meta.get("affiliation_counts", {})
    row_dict["role_counts"] = meta.get("role_counts", {})
    row_dict["language_counts"] = meta.get("language_counts", {})
    row_dict["subject_counts"] = meta.get("subject_counts", {})
    row_dict["orcid"] = meta.get("orcid") or row_dict.get("orcid")
    row_dict["variant_names"] = meta.get("variant_names") or row_dict.get("variant_names") or []

    # Fallback to legacy columns if count dicts are empty
    if not row_dict["qualifier_counts"] and row_dict.get("qualifier"):
        row_dict["qualifier_counts"] = {str(row_dict["qualifier"]): 1}
    if not row_dict["affiliation_counts"] and row_dict.get("affiliation"):
        row_dict["affiliation_counts"] = {str(row_dict["affiliation"]): 1}
    if not row_dict["role_counts"] and row_dict.get("role"):
        row_dict["role_counts"] = {str(row_dict["role"]): 1}

    return row_dict


# ---------------------------------------------------------------------------
# WEMI Entity Core Repository Helpers
# ---------------------------------------------------------------------------


def _row_to_agent(row: Any) -> AgentEntity:
    return AgentEntity.model_validate(_hydrate_agent_profile(dict(row)))


def _row_to_work(row: Any) -> WorkEntity:
    return WorkEntity.model_validate(dict(row))


def _row_to_expression(row: Any) -> ExpressionEntity:
    return ExpressionEntity.model_validate(dict(row))


def _row_to_manifestation(row: Any) -> ManifestationEntity:
    return ManifestationEntity.model_validate(dict(row))


def _row_to_item(row: Any) -> ItemEntity:
    return ItemEntity.model_validate(dict(row))


def _row_to_identifier(row: Any) -> IdentifierEntity:
    return IdentifierEntity.model_validate(dict(row))


def _row_to_contribution(row: Any) -> ContributionEntity:
    return ContributionEntity.model_validate(dict(row))


def _row_to_work_relationship(row: Any) -> WorkRelationship:
    return WorkRelationship.model_validate(dict(row))


def _row_to_harvest_state(row: Any) -> HarvestState:
    return HarvestState.model_validate(dict(row))


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
        corpus_db_path: Optional[Path | str] = None,
        engine: Optional[Engine] = None,
    ) -> None:
        if engine is not None:
            self.engine = engine
            self.db_path = engine.url.render_as_string(hide_password=False)
        else:
            self.engine = get_engine(db_path)
            self.db_path = self.engine.url.render_as_string(hide_password=False)
        # Corpus staging lives in the authority database; this attribute survives
        # only so callers that still ask can see there is no separate file.
        self.corpus_db_path = self.db_path
        self.dimension = dimension
        self.vector_engine_config = vector_engine_config
        if fts_config is not None:
            self.fts_config = fts_config
        else:
            from reconciliator.config import RepositoryFTSConfig

            self.fts_config = RepositoryFTSConfig()

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

    def get_connection(self) -> ConnectionAdapter:
        """Obtain connection to authority.db with foreign keys enabled."""
        return ConnectionAdapter(self.engine.connect(), auto_close=True)

    def get_corpus_connection(self) -> ConnectionAdapter:
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

    def insert_agents_batch(self, agents: list[AgentEntity]) -> list[int]:
        """Insert or update AgentEntity records in batch."""
        if not agents:
            return []
        agent_ids: list[int] = []
        with self.get_connection() as conn:
            for agent in agents:
                agent_meta: dict[str, Any] = {}
                if agent.metadata_json:
                    try:
                        agent_meta = json.loads(agent.metadata_json)
                    except Exception:
                        agent_meta = {}
                if agent.qualifier_counts:
                    agent_meta["qualifier_counts"] = agent.qualifier_counts
                if agent.affiliation_counts:
                    agent_meta["affiliation_counts"] = agent.affiliation_counts
                if agent.role_counts:
                    agent_meta["role_counts"] = agent.role_counts
                if agent.language_counts:
                    agent_meta["language_counts"] = agent.language_counts
                if agent.subject_counts:
                    agent_meta["subject_counts"] = agent.subject_counts
                if agent.orcid:
                    agent_meta["orcid"] = agent.orcid
                if agent.variant_names:
                    agent_meta["variant_names"] = agent.variant_names
                meta_str = json.dumps(agent_meta, ensure_ascii=False) if agent_meta else None

                stmt = pg_insert(agents_table).values(
                    source=agent.source,
                    source_id=agent.source_id,
                    record_origin=agent.record_origin,
                    name=agent.name,
                    name_normalized=agent.name_normalized,
                    surname=agent.surname,
                    qualifier=None,
                    dates=agent.dates,
                    birth_year=agent.birth_year,
                    death_year=agent.death_year,
                    fuller_name=agent.fuller_name,
                    affiliation=None,
                    metadata_json=meta_str,
                    is_personal=bool(agent.is_personal),
                    is_fictional=bool(agent.is_fictional),
                )
                stmt = stmt.on_conflict_do_update(
                    index_elements=["source", "source_id"],
                    set_={
                        "record_origin": stmt.excluded.record_origin,
                        "name": stmt.excluded.name,
                        "name_normalized": stmt.excluded.name_normalized,
                        "surname": func.coalesce(stmt.excluded.surname, agents_table.c.surname),
                        "qualifier": func.coalesce(
                            stmt.excluded.qualifier, agents_table.c.qualifier
                        ),
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
                        "affiliation": func.coalesce(
                            stmt.excluded.affiliation, agents_table.c.affiliation
                        ),
                        "metadata_json": func.coalesce(
                            stmt.excluded.metadata_json, agents_table.c.metadata_json
                        ),
                        "is_personal": stmt.excluded.is_personal,
                        "is_fictional": stmt.excluded.is_fictional,
                    },
                ).returning(agents_table.c.id)
                res = conn.execute(stmt).fetchone()
                agent_ids.append(res["id"] if res else 0)
        return agent_ids

    def insert_works_batch(
        self,
        works: list[WorkEntity],
        vectors: Optional[np.ndarray] = None,
    ) -> list[int]:
        """Insert or update WorkEntity records and optional dense vectors in batch."""
        if not works:
            return []
        work_ids: list[int] = []
        with self.get_connection() as conn:
            for work in works:
                stmt = pg_insert(works_table).values(
                    source=work.source,
                    source_id=work.source_id,
                    record_origin=work.record_origin,
                    title=work.title,
                    title_normalized=work.title_normalized,
                    original_language=work.original_language,
                    primary_agent_id=work.primary_agent_id,
                    ykl_class=work.ykl_class,
                    work_type=work.work_type,
                    subjects_json=json.dumps(work.subjects_json, ensure_ascii=False),
                    serialized_text=work.serialized_text,
                )
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
                        "ykl_class": func.coalesce(
                            stmt.excluded.ykl_class, works_table.c.ykl_class
                        ),
                        "work_type": stmt.excluded.work_type,
                        "subjects_json": stmt.excluded.subjects_json,
                        "serialized_text": stmt.excluded.serialized_text,
                    },
                ).returning(works_table.c.id)
                res = conn.execute(stmt).fetchone()
                work_ids.append(res["id"] if res else 0)

        if vectors is not None and self.work_indexer is not None and len(vectors) == len(works):
            vector_ids = self.work_indexer.add_vectors(vectors, ids=work_ids)
            with self.get_connection() as conn:
                for v_id, w_id in zip(vector_ids, work_ids):
                    v_stmt = pg_insert(work_vectors_table).values(vector_id=v_id, work_id=w_id)
                    v_stmt = v_stmt.on_conflict_do_update(
                        index_elements=["vector_id"],
                        set_={"work_id": v_stmt.excluded.work_id},
                    )
                    conn.execute(v_stmt)

        for work, wid in zip(works, work_ids):
            if getattr(work, "wikidata_qid", None) and work.source != "wikidata":
                self.insert_identifiers_batch(
                    [
                        IdentifierEntity(
                            entity_type="work",
                            entity_id=wid,
                            source="wikidata",
                            source_id=work.wikidata_qid,
                            value=work.wikidata_qid,
                        )
                    ]
                )

        return work_ids

    def insert_expressions_batch(
        self,
        expressions: list[ExpressionEntity],
    ) -> list[int]:
        """Insert or update ExpressionEntity records and optional dense vectors in batch."""
        if not expressions:
            return []
        expression_ids: list[int] = []
        with self.get_connection() as conn:
            for exp in expressions:
                stmt = pg_insert(expressions_table).values(
                    work_id=exp.work_id,
                    source=exp.source,
                    source_id=exp.source_id,
                    record_origin=exp.record_origin,
                    language=exp.language,
                    content_type=exp.content_type,
                    title=exp.title,
                    version_designation=exp.version_designation,
                    arrangement_note=exp.arrangement_note,
                    medium_of_performance=exp.medium_of_performance,
                    serialized_text=exp.serialized_text,
                )
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
                ).returning(expressions_table.c.id)
                res = conn.execute(stmt).fetchone()
                expression_ids.append(res["id"] if res else 0)

        return expression_ids

    def insert_manifestations_batch(
        self,
        manifestations: list[ManifestationEntity],
        vectors: Optional[np.ndarray] = None,
    ) -> list[int]:
        """Insert or update ManifestationEntity records and optional dense vectors in batch."""
        if not manifestations:
            return []
        manif_ids: list[int] = []
        with self.get_connection() as conn:
            for manif in manifestations:
                stmt = pg_insert(manifestations_table).values(
                    work_id=manif.work_id,
                    container_id=manif.container_id,
                    is_component=bool(manif.is_component),
                    component_type=manif.component_type,
                    source=manif.source,
                    source_id=manif.source_id,
                    record_origin=manif.record_origin,
                    title=manif.title,
                    title_normalized=manif.title_normalized,
                    subtitle=manif.subtitle,
                    publication_year=manif.publication_year,
                    publisher=manif.publisher,
                    publication_place=manif.publication_place,
                    edition_statement=manif.edition_statement,
                    edition_number=manif.edition_number,
                    series_title=manif.series_title,
                    series_numbering=manif.series_numbering,
                    volume_designation=manif.volume_designation,
                    issue_enumeration=manif.issue_enumeration,
                    pagination=manif.pagination,
                    extent=manif.extent,
                    carrier_type=manif.carrier_type,
                    host_title=manif.host_title,
                    serialized_text=manif.serialized_text,
                )
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
                ).returning(manifestations_table.c.id)
                res = conn.execute(stmt).fetchone()
                manif_ids.append(res["id"] if res else 0)

        if (
            vectors is not None
            and self.manifestation_indexer is not None
            and len(vectors) == len(manifestations)
        ):
            vector_ids = self.manifestation_indexer.add_vectors(vectors, ids=manif_ids)
            with self.get_connection() as conn:
                for v_id, m_id in zip(vector_ids, manif_ids):
                    v_stmt = pg_insert(manifestation_vectors_table).values(
                        vector_id=v_id, manifestation_id=m_id
                    )
                    v_stmt = v_stmt.on_conflict_do_update(
                        index_elements=["vector_id"],
                        set_={"manifestation_id": v_stmt.excluded.manifestation_id},
                    )
                    conn.execute(v_stmt)

        return manif_ids

    def insert_manifestation_expressions_batch(
        self, assocs: list[ManifestationExpressionAssociation]
    ) -> None:
        """Insert or replace junction associations between Manifestations and Expressions."""
        if not assocs:
            return
        with self.get_connection() as conn:
            for a in assocs:
                stmt = pg_insert(manifestation_expressions_table).values(
                    manifestation_id=a.manifestation_id,
                    expression_id=a.expression_id,
                    track_number=a.track_number,
                    disc_number=a.disc_number,
                    duration=a.duration,
                    is_primary=bool(a.is_primary),
                )
                stmt = stmt.on_conflict_do_update(
                    index_elements=["manifestation_id", "expression_id"],
                    set_={
                        "track_number": stmt.excluded.track_number,
                        "disc_number": stmt.excluded.disc_number,
                        "duration": stmt.excluded.duration,
                        "is_primary": stmt.excluded.is_primary,
                    },
                )
                conn.execute(stmt)

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

    def insert_items_batch(self, items: list[ItemEntity]) -> list[int]:
        """Insert ItemEntity records in batch."""
        if not items:
            return []
        item_ids: list[int] = []
        with self.get_connection() as conn:
            for item in items:
                stmt = (
                    pg_insert(items_table)
                    .values(
                        manifestation_id=item.manifestation_id,
                        source=item.source,
                        source_id=item.source_id,
                        record_origin=item.record_origin,
                        holding_organization=item.holding_organization,
                        shelf_mark=item.shelf_mark,
                        barcode=item.barcode,
                        custodial_history=item.custodial_history,
                        item_condition=item.item_condition,
                    )
                    .returning(items_table.c.id)
                )
                res = conn.execute(stmt).fetchone()
                item_ids.append(res["id"] if res else 0)
        return item_ids

    def insert_identifiers_batch(self, identifiers: list[IdentifierEntity]) -> list[int]:
        """Insert polymorphic IdentifierEntity records in batch."""
        if not identifiers:
            return []
        id_ids: list[int] = []
        with self.get_connection() as conn:
            for ident in identifiers:
                stmt = (
                    pg_insert(identifiers_table)
                    .values(
                        entity_type=ident.entity_type,
                        entity_id=ident.entity_id,
                        source=ident.source,
                        source_id=ident.source_id,
                        source_id_normalized=ident.source_id_normalized,
                    )
                    .returning(identifiers_table.c.id)
                )
                res = conn.execute(stmt).fetchone()
                id_ids.append(res["id"] if res else 0)
        return id_ids

    def insert_entity_keys_batch(self, keys: list[EntityKey]) -> None:
        """Insert derived domain blocking keys in batch.

        Idempotent: the table's UNIQUE constraint makes re-ingesting a record a
        no-op for its keys rather than an accumulation of duplicates.
        """
        if not keys:
            return
        with self.get_connection() as conn:
            for k in keys:
                stmt = (
                    pg_insert(entity_keys_table)
                    .values(
                        entity_type=k.entity_type,
                        entity_id=k.entity_id,
                        key_type=k.key_type,
                        key_value=k.key_value,
                    )
                    .on_conflict_do_nothing(
                        index_elements=["entity_type", "entity_id", "key_type", "key_value"]
                    )
                )
                conn.execute(stmt)

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
            rows = conn.execute(stmt).fetchall()
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
            rows = conn.execute(stmt).fetchall()
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
            rows = conn.execute(stmt).fetchall()

            key_bags = self.get_all_entity_keys("manifestation")

            ident_stmt = select(
                identifiers_table.c.entity_type,
                identifiers_table.c.entity_id,
                identifiers_table.c.source.label("scheme"),
                identifiers_table.c.source_id.label("value"),
            ).where(identifiers_table.c.entity_type.in_(("manifestation", "work", "expression")))
            ident_rows = conn.execute(ident_stmt).fetchall()

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
            rows = conn.execute(stmt).fetchall()
        return [row["entity_id"] for row in rows]

    def insert_contributions_batch(self, contributions: list[ContributionEntity]) -> list[int]:
        """Insert ContributionEntity records in batch."""
        if not contributions:
            return []
        c_ids: list[int] = []
        with self.get_connection() as conn:
            for c in contributions:
                stmt = (
                    pg_insert(contributions_table)
                    .values(
                        entity_type=c.entity_type,
                        entity_id=c.entity_id,
                        agent_id=c.agent_id,
                        role=c.role,
                        is_primary=bool(c.is_primary),
                        field_tag=c.field_tag,
                    )
                    .returning(contributions_table.c.id)
                )
                res = conn.execute(stmt).fetchone()
                c_ids.append(res["id"] if res else 0)
        return c_ids

    def insert_work_relationships_batch(self, rels: list[WorkRelationship]) -> list[int]:
        """Insert WorkRelationship records in batch."""
        if not rels:
            return []
        r_ids: list[int] = []
        with self.get_connection() as conn:
            for rel in rels:
                stmt = pg_insert(work_relationships_table).values(
                    source_work_id=rel.source_work_id,
                    target_work_id=rel.target_work_id,
                    relationship_type=rel.relationship_type,
                )
                stmt = stmt.on_conflict_do_update(
                    index_elements=["source_work_id", "target_work_id", "relationship_type"],
                    set_={"relationship_type": stmt.excluded.relationship_type},
                ).returning(work_relationships_table.c.id)
                res = conn.execute(stmt).fetchone()
                r_ids.append(res["id"] if res else 0)
        return r_ids

    def insert_medium_of_performance_batch(self, mops: list[MediumOfPerformance]) -> None:
        """Insert or replace MediumOfPerformance records in batch."""
        if not mops:
            return
        with self.get_connection() as conn:
            for m in mops:
                stmt = pg_insert(work_medium_of_performance_table).values(
                    work_id=m.work_id,
                    medium_uri=m.medium_uri,
                    label=m.label,
                    player_count=m.player_count,
                    is_soloist=bool(m.is_soloist),
                )
                stmt = stmt.on_conflict_do_update(
                    index_elements=["work_id", "medium_uri"],
                    set_={
                        "label": stmt.excluded.label,
                        "player_count": stmt.excluded.player_count,
                        "is_soloist": stmt.excluded.is_soloist,
                    },
                )
                conn.execute(stmt)

    def insert_entity_subjects_batch(self, subjects: list[EntitySubject]) -> list[int]:
        """Insert EntitySubject records in batch."""
        if not subjects:
            return []
        s_ids: list[int] = []
        with self.get_connection() as conn:
            for s in subjects:
                stmt = (
                    pg_insert(entity_subjects_table)
                    .values(
                        entity_type=s.entity_type,
                        entity_id=s.entity_id,
                        source=s.source,
                        source_record_id=s.source_record_id,
                        relation_type=s.relation_type,
                        vocabulary=s.vocabulary,
                        concept_id=s.concept_id,
                        raw_label=s.raw_label,
                        is_resolved=bool(s.is_resolved),
                        is_primary=bool(s.is_primary),
                    )
                    .returning(entity_subjects_table.c.id)
                )
                row = conn.execute(stmt).fetchone()
                s_ids.append(row["id"] if row else 0)
        return s_ids

    def insert_wemi_batch(
        self,
        works: list[WorkEntity],
        expressions: list[ExpressionEntity],
        manifestations: list[ManifestationEntity],
        items: Optional[list[ItemEntity]] = None,
        agents: Optional[list[AgentEntity]] = None,
        relationships: Optional[list[WorkRelationship]] = None,
        identifiers: Optional[list[IdentifierEntity]] = None,
        manifestation_expressions: Optional[list[ManifestationExpressionAssociation]] = None,
        mediums_of_performance: Optional[list[MediumOfPerformance]] = None,
        contributions: Optional[list[ContributionEntity]] = None,
        entity_keys: Optional[list[EntityKey]] = None,
        entity_subjects: Optional[list[EntitySubject]] = None,
        work_vectors: Optional[Any] = None,
        manifestation_vectors: Optional[Any] = None,
        record_origin: Optional[RecordOrigin | str] = None,
    ) -> None:
        """Coordinated batch insertion of WEMI domain entities with foreign key binding."""
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

        # 1. Agents
        agent_key_to_id: dict[Any, int] = {}
        if agents:
            a_ids = self.insert_agents_batch(agents)
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
            w_ids = self.insert_works_batch(works, vectors=work_vectors)
            for w, wid in zip(works, w_ids):
                w.id = wid
                work_key_to_id[w.curie] = wid
                work_key_to_id[(w.source, w.source_id)] = wid
                work_key_to_id[w.source_id] = wid

        # 3. Expressions
        expr_key_to_id: dict[Any, int] = {}
        if expressions:
            for exp in expressions:
                if not exp.work_id or exp.work_id <= 0:
                    cand_id = _resolve(work_key_to_id, exp.work_curie)
                    if cand_id is None:
                        cand_id = work_key_to_id.get((exp.source, exp.source_id))
                    if cand_id is None and len(works) == 1:
                        cand_id = works[0].id
                    if cand_id:
                        exp.work_id = cand_id
            e_ids = self.insert_expressions_batch(expressions)
            for exp, eid in zip(expressions, e_ids):
                exp.id = eid
                expr_key_to_id[exp.curie] = eid
                expr_key_to_id[(exp.source, exp.source_id)] = eid
                expr_key_to_id[exp.source_id] = eid
                expr_key_to_id[f"{exp.source}:{exp.source_id}:{exp.language}"] = eid
                expr_key_to_id[(exp.source, f"{exp.source_id}:{exp.language}")] = eid

        # 4. Manifestations, containers before components
        manif_key_to_id: dict[Any, int] = {}
        if manifestations:
            for m in manifestations:
                if not m.work_id or m.work_id <= 0:
                    cand_id = _resolve(work_key_to_id, m.work_curie)
                    if cand_id is None:
                        cand_id = work_key_to_id.get((m.source, m.source_id))
                    if cand_id is None and len(works) == 1:
                        cand_id = works[0].id
                    if cand_id:
                        m.work_id = cand_id

            def _insert_manifestations(rows: list[ManifestationEntity], idx: list[int]) -> None:
                if not rows:
                    return
                vectors = (
                    [manifestation_vectors[i] for i in idx]
                    if manifestation_vectors is not None
                    else None
                )
                ids = self.insert_manifestations_batch(rows, vectors=vectors)
                for row, row_id in zip(rows, ids):
                    row.id = row_id
                    manif_key_to_id[row.curie] = row_id
                    manif_key_to_id[(row.source, row.source_id)] = row_id
                    manif_key_to_id[row.source_id] = row_id

            component_idx = [i for i, m in enumerate(manifestations) if m.container_curie]
            if not component_idx:
                _insert_manifestations(manifestations, list(range(len(manifestations))))
            else:
                container_idx = [i for i, m in enumerate(manifestations) if not m.container_curie]
                _insert_manifestations([manifestations[i] for i in container_idx], container_idx)
                for i in component_idx:
                    component = manifestations[i]
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
                _insert_manifestations([manifestations[i] for i in component_idx], component_idx)

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
                if (not assoc.expression_id or assoc.expression_id <= 0) and assoc.expression_curie:
                    assoc.expression_id = _resolve(expr_key_to_id, assoc.expression_curie) or 0
                if (not assoc.manifestation_id or assoc.manifestation_id <= 0) and len(
                    manifestations
                ) == 1:
                    assoc.manifestation_id = manifestations[0].id or 0
                if (not assoc.expression_id or assoc.expression_id <= 0) and len(expressions) == 1:
                    assoc.expression_id = expressions[0].id or 0
                if assoc.manifestation_id > 0 and assoc.expression_id > 0:
                    bound_assocs.append(assoc)
                else:
                    logger.warning(
                        "Dropping manifestation-expression association that binds "
                        "to no row: manifestation=%s expression=%s",
                        assoc.manifestation_curie,
                        assoc.expression_curie,
                    )
            if bound_assocs:
                self.insert_manifestation_expressions_batch(bound_assocs)

        # 6. Items
        if items:
            for item in items:
                if not item.manifestation_id or item.manifestation_id <= 0:
                    cand_id = manif_key_to_id.get((item.source, item.source_id))
                    if cand_id is None and len(manifestations) == 1:
                        cand_id = manifestations[0].id
                    if cand_id:
                        item.manifestation_id = cand_id
            self.insert_items_batch(items)

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
                self.insert_identifiers_batch(bound_identifiers)

        # 8. Work Relationships
        if relationships:
            bound_rels: list[WorkRelationship] = []
            for rel in relationships:
                if (not rel.source_work_id or rel.source_work_id <= 0) and rel.source_work_curie:
                    rel.source_work_id = _resolve(work_key_to_id, rel.source_work_curie) or 0
                if (not rel.target_work_id or rel.target_work_id <= 0) and rel.target_work_curie:
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
                self.insert_work_relationships_batch(bound_rels)

        # 9. Mediums of Performance
        if mediums_of_performance:
            for mop in mediums_of_performance:
                if (not mop.work_id or mop.work_id <= 0) and works:
                    mop.work_id = works[0].id
                self.insert_medium_of_performance_batch(mediums_of_performance)

        # 10. Contributions
        if contributions:
            valid_contribs: list[ContributionEntity] = []
            for c in contributions:
                if (not c.entity_id or c.entity_id <= 0) and c.entity_curie:
                    if c.entity_type == "work":
                        c.entity_id = _resolve(work_key_to_id, c.entity_curie) or 0
                    elif c.entity_type == "expression":
                        c.entity_id = _resolve(expr_key_to_id, c.entity_curie) or 0
                    elif c.entity_type == "manifestation":
                        c.entity_id = _resolve(manif_key_to_id, c.entity_curie) or 0
                if (not c.agent_id or c.agent_id <= 0) and c.agent_curie:
                    c.agent_id = _resolve(agent_key_to_id, c.agent_curie) or 0
                if not c.entity_id or c.entity_id <= 0:
                    if c.entity_type == "work" and works:
                        c.entity_id = works[0].id
                    elif c.entity_type == "expression" and expressions:
                        c.entity_id = expressions[0].id
                    elif c.entity_type == "manifestation" and manifestations:
                        c.entity_id = manifestations[0].id
                if not c.agent_id or c.agent_id <= 0:
                    if agents:
                        c.agent_id = agents[0].id
                if c.agent_id and c.agent_id > 0 and c.entity_id and c.entity_id > 0:
                    valid_contribs.append(c)
            if valid_contribs:
                self.insert_contributions_batch(valid_contribs)

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
                self.insert_entity_keys_batch(bound_keys)

        # 12. Entity subjects
        if entity_subjects:
            bound_subjects: list[EntitySubject] = []
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
                if not subj.entity_id or subj.entity_id <= 0:
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
                    bound_subjects.append(subj)
            if bound_subjects:
                self.insert_entity_subjects_batch(bound_subjects)

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
            rows = conn.execute(stmt).fetchall()
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
            rows = conn.execute(stmt).fetchall()
            return [dict(r) for r in rows]

    def get_agent(self, agent_id: int) -> Optional[AgentEntity]:
        """Fetch AgentEntity by ID."""
        stmt = select(agents_table).where(agents_table.c.id == agent_id)
        with self.get_connection() as conn:
            row = conn.execute(stmt).fetchone()
            return _row_to_agent(row) if row else None

    def get_agent_by_curie(self, curie_or_uri: str) -> Optional[AgentEntity]:
        """Fetch AgentEntity by CURIE or URI."""
        source, source_id = parse_curie(curie_or_uri)
        if not source_id:
            return None
        with self.get_connection() as conn:
            stmt = select(agents_table).where(
                agents_table.c.source == source,
                agents_table.c.source_id == source_id,
            )
            row = conn.execute(stmt).fetchone()
            if not row and source == "kanto":
                fallback_stmt = select(agents_table).where(
                    agents_table.c.source.in_(("kanto", "finaf", "asteri")),
                    agents_table.c.source_id == source_id,
                )
                row = conn.execute(fallback_stmt).fetchone()
            return _row_to_agent(row) if row else None

    def get_agent_by_uri(self, agent_uri: str) -> Optional[AgentEntity]:
        """Fetch AgentEntity by agent_uri or CURIE (backward-compatibility alias)."""
        return self.get_agent_by_curie(agent_uri)

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
            ident_row = conn.execute(stmt).fetchone()
            if ident_row:
                work.wikidata_qid = ident_row["source_id"]
        return work

    def get_work(self, work_id: int) -> Optional[WorkEntity]:
        """Fetch WorkEntity by ID."""
        stmt = select(works_table).where(works_table.c.id == work_id)
        with self.get_connection() as conn:
            row = conn.execute(stmt).fetchone()
            return self._enrich_work_identifiers(conn, _row_to_work(row)) if row else None

    def get_work_by_curie(self, curie_or_uri: str) -> Optional[WorkEntity]:
        """Fetch WorkEntity by CURIE or URI."""
        source, source_id = parse_curie(curie_or_uri)
        if not source_id:
            return None
        return self.get_work_by_source(source, source_id)

    def get_work_by_uri(self, work_uri: str) -> Optional[WorkEntity]:
        """Fetch WorkEntity by work_uri or CURIE (backward-compatibility alias)."""
        return self.get_work_by_curie(work_uri)

    def get_work_by_source(self, source: str, source_id: str) -> Optional[WorkEntity]:
        """Fetch WorkEntity by source and source_id."""
        stmt = select(works_table).where(
            works_table.c.source == source,
            works_table.c.source_id == source_id,
        )
        with self.get_connection() as conn:
            row = conn.execute(stmt).fetchone()
            return self._enrich_work_identifiers(conn, _row_to_work(row)) if row else None

    def get_expression(self, expression_id: int) -> Optional[ExpressionEntity]:
        """Fetch ExpressionEntity by ID."""
        stmt = select(expressions_table).where(expressions_table.c.id == expression_id)
        with self.get_connection() as conn:
            row = conn.execute(stmt).fetchone()
            return _row_to_expression(row) if row else None

    def get_expression_by_curie(self, curie_or_uri: str) -> Optional[ExpressionEntity]:
        """Fetch ExpressionEntity by CURIE or URI."""
        source, source_id = parse_curie(curie_or_uri)
        if not source_id:
            return None
        with self.get_connection() as conn:
            stmt = select(expressions_table).where(
                expressions_table.c.source == source.lower(),
                expressions_table.c.source_id == source_id,
            )
            row = conn.execute(stmt).fetchone()
            return _row_to_expression(row) if row else None

    def get_expression_by_uri(self, expression_uri: str) -> Optional[ExpressionEntity]:
        """Fetch ExpressionEntity by expression_uri or CURIE (backward-compatibility alias)."""
        return self.get_expression_by_curie(expression_uri)

    def get_manifestation(self, manifestation_id: int) -> Optional[ManifestationEntity]:
        """Fetch ManifestationEntity by ID."""
        stmt = select(manifestations_table).where(manifestations_table.c.id == manifestation_id)
        with self.get_connection() as conn:
            row = conn.execute(stmt).fetchone()
            return _row_to_manifestation(row) if row else None

    def get_manifestation_by_source(
        self, source: str, source_id: str
    ) -> Optional[ManifestationEntity]:
        """Fetch ManifestationEntity by source and source_id."""
        stmt = select(manifestations_table).where(
            manifestations_table.c.source == source,
            manifestations_table.c.source_id == source_id,
        )
        with self.get_connection() as conn:
            row = conn.execute(stmt).fetchone()
            return _row_to_manifestation(row) if row else None

    def get_expressions_for_work(
        self, work_id: int, record_origin: Optional[RecordOrigin | str] = None
    ) -> list[ExpressionEntity]:
        """Fetch all Expression entities linked to a given work_id, with optional record_origin filter."""
        stmt = select(expressions_table).where(expressions_table.c.work_id == work_id)
        if record_origin is not None:
            stmt = stmt.where(expressions_table.c.record_origin == record_origin)
        stmt = stmt.order_by(expressions_table.c.id)
        with self.get_connection() as conn:
            rows = conn.execute(stmt).fetchall()
            return [_row_to_expression(r) for r in rows]

    def get_constituent_works(self, work_id: int) -> list[WorkEntity]:
        """The works this one contains, by stored `has_constituent` edges."""
        stmt = (
            select(
                works_table,
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
                work_relationships_table.c.relationship_type == "has_constituent",
            )
            .group_by(*works_table.c)
            .order_by(
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

    def get_manifestation_by_uri(self, manifestation_uri: str) -> Optional[ManifestationEntity]:
        """Fetch ManifestationEntity by URI or CURIE (backward-compatibility alias)."""
        return self.get_manifestation_by_curie(manifestation_uri)

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
            return {r["name"]: bool(r["is_authoritative"]) for r in conn.execute(stmt)}

    def authoritative_sources(self) -> list[str]:
        """Names of the sources whose records may be used as authority."""
        stmt = (
            select(sources_table.c.name)
            .where(sources_table.c.is_authoritative == True)  # noqa: E712
            .order_by(sources_table.c.name)
        )
        with self.get_connection() as conn:
            return [r["name"] for r in conn.execute(stmt)]

    def non_authoritative_sources(self) -> list[str]:
        """Source names registered as non-authoritative, i.e. corpora to cluster."""
        return sorted(name for name, auth in self.get_sources().items() if not auth)

    def _origin_clause(
        self, column: str = "record_origin", authoritative_only: bool = True
    ) -> tuple[str, list[Any]]:
        """SQL fragment restricting rows to authoritative sources."""
        if not authoritative_only:
            return "", []
        names = self.authoritative_sources()
        if not names:
            return "", []
        placeholders = ",".join("?" for _ in names)
        return f" AND {column} IN ({placeholders})", list(names)

    def set_canonical_works(self, mapping: dict[int, int]) -> None:
        """Record the elected canonical work for each consolidated work id."""
        if not mapping:
            return
        with self.get_connection() as conn:
            for work_id, canonical in mapping.items():
                stmt = (
                    update(works_table)
                    .where(works_table.c.id == work_id)
                    .values(canonical_work_id=canonical)
                )
                conn.execute(stmt)

    def clear_canonical_works(self) -> None:
        """Undo consolidation, leaving every work its own canonical."""
        with self.get_connection() as conn:
            conn.execute(update(works_table).values(canonical_work_id=None))
            conn.execute(
                delete(work_relationships_table).where(
                    work_relationships_table.c.relationship_type == "same_work_as"
                )
            )

    def get_work_equivalence_map(self) -> dict[int, int]:
        """Map each consolidated work id to its group's canonical work id."""
        stmt = select(works_table.c.id, works_table.c.canonical_work_id).where(
            works_table.c.canonical_work_id.is_not(None),
            works_table.c.canonical_work_id != works_table.c.id,
        )
        with self.get_connection() as conn:
            return {r["id"]: r["canonical_work_id"] for r in conn.execute(stmt)}

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

    def lookup_by_identifier(
        self, scheme: str, value: str, authoritative_only: bool = False
    ) -> list[tuple[EntityType, int]]:
        """Look up entity types and IDs carrying an identifier, optionally authoritative only."""
        src = scheme.lower()
        if src == "wikidata_qid":
            src = "wikidata"
        val_clean = value.strip()
        norm_val = val_clean.lower()
        alnum_val = re.sub(r"[^0-9a-zA-Z]", "", val_clean).lower()
        stmt = select(identifiers_table.c.entity_type, identifiers_table.c.entity_id).where(
            identifiers_table.c.source == src,
            or_(
                identifiers_table.c.source_id_normalized == norm_val,
                identifiers_table.c.source_id_normalized == alnum_val,
                identifiers_table.c.source_id == val_clean,
            ),
        )
        with self.get_connection() as conn:
            rows = conn.execute(stmt).fetchall()
            candidates = [(r["entity_type"], r["entity_id"]) for r in rows]
            if not authoritative_only:
                return candidates

            allowed = set(self.authoritative_sources())
            if not allowed:
                return candidates

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
            filtered: list[tuple[EntityType, int]] = []
            for etype, eid in candidates:
                tbl = table_map.get(etype)
                if tbl is not None:
                    check_stmt = select(tbl.c.record_origin).where(tbl.c.id == eid)
                    row = conn.execute(check_stmt).fetchone()
                    if row and row["record_origin"] in allowed:
                        filtered.append((etype, eid))
            return filtered

    def get_mediums_of_performance(self, work_id: int) -> list[MediumOfPerformance]:
        """Fetch MediumOfPerformance records for a given work."""
        stmt = select(work_medium_of_performance_table).where(
            work_medium_of_performance_table.c.work_id == work_id
        )
        with self.get_connection() as conn:
            rows = conn.execute(stmt).fetchall()
            return [
                MediumOfPerformance(
                    work_id=r["work_id"],
                    medium_uri=r["medium_uri"],
                    label=r["label"],
                    player_count=r["player_count"],
                    is_soloist=bool(r["is_soloist"]),
                )
                for r in rows
            ]

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
            cursor = conn.execute(stmt)
            return {row["source_id"] for row in cursor if row["source_id"]}

    # -----------------------------------------------------------------------
    # Database-Level SQLite FTS5 Lexical Search (Engine-Level BM25 Ranking)
    #
    # NOTE ON ARCHITECTURAL DISTINCTION:
    # This module provides database-level full-text search against SQLite FTS5
    # virtual tables (works_fts, expressions_fts, manifestations_fts). Ranking is
    # computed directly inside SQLite using its native C implementation of bm25()
    # (or configured ranking expression/weights from RepositoryFTSConfig in storage.fts).
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
        prefix_search: Optional[bool] = None,
    ) -> tuple[str, str, int]:
        """Format MATCH query string, ORDER BY clause, and limit based on config or explicit parameters."""
        eff_limit = (
            limit
            if limit is not None
            else (self.fts_config.default_limit if self.fts_config else 20)
        )
        eff_rank = ranking_function or (
            self.fts_config.ranking_function if self.fts_config else "bm25"
        )
        eff_weights = (
            weights
            if weights is not None
            else (self.fts_config.column_weights if self.fts_config else None)
        )
        eff_prefix = (
            prefix_search
            if prefix_search is not None
            else (self.fts_config.prefix_search if self.fts_config else False)
        )
        clean_query = query.strip()
        if eff_prefix and clean_query:
            tokens = clean_query.split()
            processed_tokens: list[str] = []
            for t in tokens:
                token_clean = t.rstrip("*")
                if not token_clean:
                    continue
                if (
                    token_clean.upper() in {"OR", "AND", "NOT"}
                    or token_clean.startswith('"')
                    or token_clean.endswith('"')
                ):
                    processed_tokens.append(token_clean)
                else:
                    processed_tokens.append(f"{token_clean}*")
            clean_query = " ".join(processed_tokens)

        if eff_weights:
            weights_str = ", ".join(str(float(w)) for w in eff_weights)
            order_clause = f"bm25({table_name}, {weights_str})"
        elif eff_rank and eff_rank.lower() != "bm25":
            order_clause = eff_rank
        else:
            order_clause = "f.rank"

        return clean_query, order_clause, eff_limit

    def search_works_fts(
        self,
        query: str,
        limit: Optional[int] = None,
        ranking_function: Optional[str] = None,
        weights: Optional[list[float]] = None,
        prefix_search: Optional[bool] = None,
        authoritative_only: bool = False,
    ) -> list[tuple[WorkEntity, float]]:
        """Search works via PostgreSQL full-text search. Returns list of (WorkEntity, rank)."""
        if not query or not query.strip():
            return []
        eff_limit = (
            limit
            if limit is not None
            else (self.fts_config.default_limit if self.fts_config else 20)
        )
        clean_query = query.strip()
        origin_clause, origin_params = self._origin_clause("w.record_origin", authoritative_only)

        eff_prefix = (
            prefix_search
            if prefix_search is not None
            else (self.fts_config.prefix_search if self.fts_config else True)
        )

        ts_vector = func.to_tsvector(
            "simple", func.unaccent(func.coalesce(works_table.c.serialized_text, ""))
        )
        ts_query = func.websearch_to_tsquery("simple", func.unaccent(clean_query))
        rank_col = func.ts_rank_cd(ts_vector, ts_query).label("rank")

        match_cond = ts_vector.op("@@")(ts_query)
        if eff_prefix:
            match_cond = or_(
                match_cond,
                works_table.c.title_normalized.op("%")(clean_query),
            )

        stmt = select(works_table, rank_col).where(match_cond)
        if authoritative_only:
            auth_sources = self.authoritative_sources()
            if auth_sources:
                stmt = stmt.where(works_table.c.record_origin.in_(auth_sources))

        stmt = stmt.order_by(rank_col.desc()).limit(eff_limit)

        with self.get_connection() as conn:
            cursor = conn.execute(stmt)
            return [
                (_row_to_work(row), float(row["rank"]) if row["rank"] is not None else 0.0)
                for row in cursor
            ]

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
        if self.work_indexer is None or self.work_indexer.total_vectors == 0:
            return []

        # Over-fetch when a filter will be applied: the index cannot filter, so
        # asking for exactly top_k and discarding afterwards silently returns
        # fewer candidates than requested - or none, when a large source fills
        # the whole budget.
        fetch_k = top_k * max(1, overfetch) if authoritative_only else top_k
        scores, indices = self.work_indexer.search(query_vector, top_k=fetch_k)
        if len(scores) == 0 or len(indices) == 0:
            return []

        allowed = set(self.authoritative_sources()) if authoritative_only else None
        if not allowed:
            allowed = None

        vector_indices = indices[0].tolist()
        dense_scores = scores[0].tolist()

        vector_to_work_id: dict[int, int] = {}
        with self.get_connection() as conn:
            stmt = select(work_vectors_table.c.vector_id, work_vectors_table.c.work_id).where(
                work_vectors_table.c.vector_id.in_(vector_indices)
            )
            rows = conn.execute(stmt).fetchall()
            for r in rows:
                vector_to_work_id[r["vector_id"]] = r["work_id"]

        results: list[tuple[WorkEntity, float]] = []
        seen_work_ids: set[int] = set()
        for v_id, score in zip(vector_indices, dense_scores):
            work_id = vector_to_work_id.get(v_id, v_id if v_id >= 0 else None)
            if work_id is not None and work_id not in seen_work_ids:
                seen_work_ids.add(work_id)
                work = self.get_work(work_id)
                if work and (allowed is None or work.record_origin in allowed):
                    results.append((work, float(score)))
                    if len(results) >= top_k:
                        break

        return results

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
            rows = conn.execute(stmt).fetchall()
            for r in rows:
                vector_to_manif_id[r["vector_id"]] = r["manifestation_id"]

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
                rows = conn.execute(stmt).fetchall()
                for r in rows:
                    orig = r["record_origin"]
                    if orig in counts[tier]:
                        counts[tier][orig] = r["cnt"]
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

    def insert_wikidata_authors(self, authors: list[dict[str, Any]]) -> None:
        """Persist author entity metadata into agents table and identifiers table.

        Wikidata-specific fields (occupations, fields_of_work, affiliations, family_name)
        are stored inside agents.metadata_json.
        """
        if not authors:
            return
        agent_rows = []
        for a in authors:
            if not a.get("qid"):
                continue
            # Merge wikidata-specific fields into metadata_json
            existing_meta = (
                json.loads(a.get("metadata_json") or "{}") if a.get("metadata_json") else {}
            )
            if a.get("occupations"):
                existing_meta["occupations"] = a["occupations"]
            if a.get("fields_of_work"):
                existing_meta["fields_of_work"] = a["fields_of_work"]
            if a.get("affiliations"):
                existing_meta["affiliations"] = a["affiliations"]
            if a.get("family_name"):
                existing_meta["family_name"] = a["family_name"]
            metadata_json = json.dumps(existing_meta, ensure_ascii=False)

            agent_rows.append(
                {
                    "source": "wikidata",
                    "source_id": a["qid"],
                    "record_origin": "wikidata",
                    "name": a.get("name") or a["qid"],
                    "name_normalized": (a.get("name") or a["qid"]).strip().lower(),
                    "surname": a.get("family_name"),
                    "birth_year": a.get("birth_year"),
                    "death_year": a.get("death_year"),
                    "metadata_json": metadata_json,
                    "is_personal": True,
                }
            )
        with self.get_connection() as conn:
            if agent_rows:
                stmt = pg_insert(agents_table).values(agent_rows)
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
                        "metadata_json": func.coalesce(
                            stmt.excluded.metadata_json, agents_table.c.metadata_json
                        ),
                    },
                )
                conn.execute(stmt)

            # Option 3: Persist relational author identifiers into identifiers table (entity_type = 'agent')
            qids_with_idents = [a["qid"] for a in authors if a.get("qid") and a.get("identifiers")]
            if qids_with_idents:
                agent_records = conn.execute(
                    select(agents_table.c.id, agents_table.c.source_id).where(
                        agents_table.c.source == "wikidata",
                        agents_table.c.source_id.in_(qids_with_idents),
                    )
                ).fetchall()
                qid_to_agent_id = {r["source_id"]: r["id"] for r in agent_records}

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
                    agent_id_list = list(qid_to_agent_id.values())
                    existing_idents = set()
                    chk_stmt = select(
                        identifiers_table.c.entity_id,
                        identifiers_table.c.source,
                        identifiers_table.c.source_id,
                    ).where(
                        identifiers_table.c.entity_type == "agent",
                        identifiers_table.c.entity_id.in_(agent_id_list),
                    )
                    for r in conn.execute(chk_stmt):
                        existing_idents.add((r["entity_id"], r["source"], r["source_id"]))

                    to_insert = [
                        row
                        for row in ident_rows
                        if (row["entity_id"], row["source"], row["source_id"])
                        not in existing_idents
                    ]
                    if to_insert:
                        conn.execute(pg_insert(identifiers_table).values(to_insert))

    def get_wikidata_authors_batch(self, qids: list[str]) -> dict[str, dict[str, Any]]:
        """Fetch cached author entities from agents + identifiers by QIDs.

        Reconstructs the same dict shape that was previously returned from
        the wikidata_authors table so that all existing callers continue to work.
        """
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
                agents_table.c.metadata_json,
            ).where(
                agents_table.c.source == "wikidata",
                agents_table.c.source_id.in_(qids),
            )
            rows = conn.execute(stmt).fetchall()

            results: dict[str, dict[str, Any]] = {}
            agent_id_map: dict[str, int] = {}

            for row in rows:
                qid = row["source_id"]
                agent_id_map[qid] = row["id"]
                meta = json.loads(row["metadata_json"] or "{}") if row["metadata_json"] else {}

                results[qid] = {
                    "qid": qid,
                    "name": row["name"],
                    "family_name": meta.get("family_name") or row["surname"],
                    "identifiers": {},  # populated below
                    "birth_year": row["birth_year"],
                    "death_year": row["death_year"],
                    "occupations": meta.get("occupations"),
                    "fields_of_work": meta.get("fields_of_work"),
                    "affiliations": meta.get("affiliations"),
                    "metadata_json": row["metadata_json"],
                }

            # 2. Fetch identifiers for each agent
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
                    qid = rev_map.get(r["entity_id"])
                    if qid and qid in results:
                        results[qid]["identifiers"][r["source"]] = r["source_id"]

            return results

    def get_wikidata_author(self, qid: str) -> dict[str, Any] | None:
        """Fetch a single cached author entity from agents + identifiers by QID."""
        result = self.get_wikidata_authors_batch([qid])
        return result.get(qid)

    def get_agents_by_ids(self, agent_ids: list[int]) -> dict[int, dict[str, Any]]:
        """Fetch rich agent profiles by IDs from the agents table."""
        if not agent_ids:
            return {}
        results: dict[int, dict[str, Any]] = {}
        with self.get_connection() as conn:
            try:
                stmt = select(
                    agents_table.c.id,
                    agents_table.c.source,
                    agents_table.c.source_id,
                    agents_table.c.name,
                    agents_table.c.name_normalized,
                    agents_table.c.surname,
                    agents_table.c.qualifier,
                    agents_table.c.dates,
                    agents_table.c.birth_year,
                    agents_table.c.death_year,
                    agents_table.c.fuller_name,
                    agents_table.c.affiliation,
                    agents_table.c.metadata_json,
                ).where(agents_table.c.id.in_(agent_ids))
                rows = conn.execute(stmt).fetchall()
                for r in rows:
                    results[r["id"]] = _hydrate_agent_profile(dict(r))
            except Exception:
                pass
        return results

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
        can name any tier and SQLite cannot express that as one constraint, so
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
                cur = conn.execute(stmt)
                while True:
                    rows = cur.fetchmany(batch_size)
                    if not rows:
                        break
                    ids = [int(r["id"]) for r in rows]
                    texts = [str(r["serialized_text"]) for r in rows]
                    vectors = embedder.embed_texts(texts, batch_size=len(texts))
                    vector_ids = indexer.add_vectors(vectors, ids=ids)
                    with self.get_connection() as write_conn:
                        values_to_insert = [
                            {"vector_id": v_id, id_col_name: ent_id}
                            for v_id, ent_id in zip(vector_ids, ids)
                        ]
                        map_stmt = pg_insert(map_table_obj).values(values_to_insert)
                        map_stmt = map_stmt.on_conflict_do_update(
                            index_elements=[map_table_obj.c.vector_id],
                            set_={id_col_name: getattr(map_stmt.excluded, id_col_name)},
                        )
                        write_conn.execute(map_stmt)
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
        from reconciliator.template import WEMISerializationEngine

        wemi_engine = WEMISerializationEngine(agent_template_path=template_path)

        stmt = select(
            agents_table.c.id,
            agents_table.c.source,
            agents_table.c.source_id,
            agents_table.c.name,
            agents_table.c.name_normalized,
            agents_table.c.surname,
            agents_table.c.qualifier,
            agents_table.c.dates,
            agents_table.c.birth_year,
            agents_table.c.death_year,
            agents_table.c.fuller_name,
            agents_table.c.affiliation,
            agents_table.c.metadata_json,
        ).order_by(agents_table.c.id)

        with self.get_connection() as conn:
            cur = conn.execute(stmt)

            total_indexed = 0
            while True:
                rows = cur.fetchmany(batch_size)
                if not rows:
                    break
                ids: list[int] = []
                texts: list[str] = []
                for r in rows:
                    agent_dict = dict(r)
                    text = wemi_engine.render_agent(agent_dict)
                    ids.append(agent_dict["id"])
                    texts.append(text)

                if texts:
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

                work_ids = [c["entity_id"] for c in contribs if c["entity_type"] == "work"]
                manif_ids = [
                    c["entity_id"] for c in contribs if c["entity_type"] == "manifestation"
                ]
                expr_ids = [c["entity_id"] for c in contribs if c["entity_type"] == "expression"]

                # Contributions land on whichever tier the relator implies, so the
                # tiers have to be walked in both directions before evidence is
                # gathered: a work-level author still needs its manifestations'
                # publishers, and a manifestation-level one still needs its work.
                if work_ids:
                    manif_stmt = select(manifestations_table.c.id).where(
                        manifestations_table.c.work_id.in_(work_ids)
                    )
                    for row in conn.execute(manif_stmt):
                        if row["id"] not in manif_ids:
                            manif_ids.append(row["id"])
                if manif_ids:
                    manif_work_stmt = select(distinct(manifestations_table.c.work_id)).where(
                        manifestations_table.c.id.in_(manif_ids),
                        manifestations_table.c.work_id.is_not(None),
                    )
                    for row in conn.execute(manif_work_stmt):
                        if row["work_id"] not in work_ids:
                            work_ids.append(row["work_id"])
                if expr_ids:
                    expr_work_stmt = select(distinct(expressions_table.c.work_id)).where(
                        expressions_table.c.id.in_(expr_ids),
                        expressions_table.c.work_id.is_not(None),
                    )
                    for row in conn.execute(expr_work_stmt):
                        if row["work_id"] not in work_ids:
                            work_ids.append(row["work_id"])

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
                        co_agents = conn.execute(co_stmt).fetchall()
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
                    pubs = conn.execute(pub_stmt).fetchall()
                    for pb in pubs:
                        p_norm = fold_label(pb["publisher"])
                        if p_norm and (
                            p_norm in norm_target_pub
                            or norm_target_pub in p_norm
                            or fuzz.token_set_ratio(norm_target_pub, p_norm) >= 80.0
                        ):
                            evidence[cid]["has_publisher_match"] = True
                            if pb["publisher"] not in evidence[cid]["matched_publishers"]:
                                evidence[cid]["matched_publishers"].append(pb["publisher"])

                if work_ids and norm_target_classes:
                    class_stmt = select(distinct(works_table.c.ykl_class)).where(
                        works_table.c.id.in_(work_ids),
                        works_table.c.ykl_class.is_not(None),
                        works_table.c.ykl_class != "",
                    )
                    classes = conn.execute(class_stmt).fetchall()
                    for cl in classes:
                        code = str(cl["ykl_class"]).strip().lower()
                        for tc in norm_target_classes:
                            t_scheme, _, t_code = tc.partition(":")
                            if t_scheme in ("ykl", ""):
                                if code.startswith(t_code) or t_code.startswith(code):
                                    evidence[cid]["has_classification_match"] = True
                                    matched_str = f"ykl:{code}"
                                    if matched_str not in evidence[cid]["matched_classifications"]:
                                        evidence[cid]["matched_classifications"].append(matched_str)

                    # Also check subjects_json
                    subj_stmt = select(distinct(works_table.c.subjects_json)).where(
                        works_table.c.id.in_(work_ids),
                        works_table.c.subjects_json.is_not(None),
                    )
                    subjects = conn.execute(subj_stmt).fetchall()
                    for sj in subjects:
                        try:
                            subj_data = json.loads(sj["subjects_json"])
                            if isinstance(subj_data, list):
                                for subj in subj_data:
                                    code = str(subj.get("code", "")).strip().lower()
                                    scheme = str(subj.get("scheme", "")).strip().lower()
                                    if not code:
                                        continue
                                    for tc in norm_target_classes:
                                        t_scheme, _, t_code = tc.partition(":")
                                        if t_scheme == scheme:
                                            if code.startswith(t_code) or t_code.startswith(code):
                                                evidence[cid]["has_classification_match"] = True
                                                matched_str = f"{scheme}:{code}"
                                                if (
                                                    matched_str
                                                    not in evidence[cid]["matched_classifications"]
                                                ):
                                                    evidence[cid]["matched_classifications"].append(
                                                        matched_str
                                                    )
                        except Exception:
                            pass

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
        rows = [
            {
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
        ]
        stmt = pg_insert(corpus_manifestation_clusters_table).values(rows)
        stmt = stmt.on_conflict_do_update(
            index_elements=[corpus_manifestation_clusters_table.c.cluster_id],
            set_={
                "canonical_record_id": stmt.excluded.canonical_record_id,
                "publisher": stmt.excluded.publisher,
                "publication_year": stmt.excluded.publication_year,
                "edition_statement": stmt.excluded.edition_statement,
                "extent": stmt.excluded.extent,
                "carrier_type": stmt.excluded.carrier_type,
                "member_record_count": stmt.excluded.member_record_count,
            },
        )
        with self.get_corpus_connection() as conn:
            conn.execute(stmt)

    def insert_work_clusters_batch(self, clusters: list[WorkCluster]) -> None:
        """Batch insert or replace work family clusters."""
        if not clusters:
            return
        rows = [
            {
                "cluster_id": c.cluster_id,
                "work_type": c.work_type,
                "canonical_work_source": c.canonical_work_source,
                "canonical_work_source_id": c.canonical_work_source_id,
                "canonical_title": c.canonical_title,
                "canonical_author": c.canonical_author,
            }
            for c in clusters
        ]
        stmt = pg_insert(corpus_work_clusters_table).values(rows)
        stmt = stmt.on_conflict_do_update(
            index_elements=[corpus_work_clusters_table.c.cluster_id],
            set_={
                "work_type": stmt.excluded.work_type,
                "canonical_work_source": stmt.excluded.canonical_work_source,
                "canonical_work_source_id": stmt.excluded.canonical_work_source_id,
                "canonical_title": stmt.excluded.canonical_title,
                "canonical_author": stmt.excluded.canonical_author,
            },
        )
        with self.get_corpus_connection() as conn:
            conn.execute(stmt)

    def insert_expression_clusters_batch(self, clusters: list[ExpressionCluster]) -> None:
        """Batch insert or replace expression realization clusters."""
        if not clusters:
            return
        rows = [
            {
                "cluster_id": c.cluster_id,
                "work_cluster_id": c.work_cluster_id,
                "canonical_expression_source": c.canonical_expression_source,
                "canonical_expression_source_id": c.canonical_expression_source_id,
                "content_type": c.content_type,
                "language": c.language,
                "version_designation": c.version_designation,
                "translators_json": json.dumps(c.translators_json, ensure_ascii=False),
                "performers_json": json.dumps(c.performers_json, ensure_ascii=False),
            }
            for c in clusters
        ]
        stmt = pg_insert(corpus_expression_clusters_table).values(rows)
        stmt = stmt.on_conflict_do_update(
            index_elements=[corpus_expression_clusters_table.c.cluster_id],
            set_={
                "work_cluster_id": stmt.excluded.work_cluster_id,
                "canonical_expression_source": stmt.excluded.canonical_expression_source,
                "canonical_expression_source_id": stmt.excluded.canonical_expression_source_id,
                "content_type": stmt.excluded.content_type,
                "language": stmt.excluded.language,
                "version_designation": stmt.excluded.version_designation,
                "translators_json": stmt.excluded.translators_json,
                "performers_json": stmt.excluded.performers_json,
            },
        )
        with self.get_corpus_connection() as conn:
            conn.execute(stmt)

    def insert_corpus_linkages_batch(self, linkages: list[CorpusLinkage]) -> None:
        """Batch insert or replace final record-to-cluster linkages."""
        if not linkages:
            return
        rows = [
            {
                "record_id": lnk.record_id,
                "manifestation_cluster_id": lnk.manifestation_cluster_id,
                "work_cluster_id": lnk.work_cluster_id,
                "expression_cluster_id": lnk.expression_cluster_id,
                "final_work_source": lnk.final_work_source,
                "final_work_source_id": lnk.final_work_source_id,
                "final_expression_source": lnk.final_expression_source,
                "final_expression_source_id": lnk.final_expression_source_id,
                "sibling_manifestation_ids_json": lnk.sibling_manifestation_ids_json,
            }
            for lnk in linkages
        ]
        stmt = pg_insert(corpus_linkages_table).values(rows)
        stmt = stmt.on_conflict_do_update(
            index_elements=[corpus_linkages_table.c.record_id],
            set_={
                "manifestation_cluster_id": stmt.excluded.manifestation_cluster_id,
                "work_cluster_id": stmt.excluded.work_cluster_id,
                "expression_cluster_id": stmt.excluded.expression_cluster_id,
                "final_work_source": stmt.excluded.final_work_source,
                "final_work_source_id": stmt.excluded.final_work_source_id,
                "final_expression_source": stmt.excluded.final_expression_source,
                "final_expression_source_id": stmt.excluded.final_expression_source_id,
                "sibling_manifestation_ids_json": stmt.excluded.sibling_manifestation_ids_json,
            },
        )
        with self.get_corpus_connection() as conn:
            conn.execute(stmt)

    def insert_sammelband_bindings_batch(self, bindings: list[SammelbandBinding]) -> None:
        """Batch insert or replace sammelband item-level composite bindings."""
        if not bindings:
            return
        rows = [
            {
                "binding_id": b.binding_id,
                "record_id": b.record_id,
                "sequence_in_binding": b.sequence_in_binding,
                "binding_notes": b.binding_notes,
            }
            for b in bindings
        ]
        stmt = pg_insert(sammelband_bindings_table).values(rows)
        stmt = stmt.on_conflict_do_update(
            index_elements=[
                sammelband_bindings_table.c.binding_id,
                sammelband_bindings_table.c.record_id,
            ],
            set_={
                "sequence_in_binding": stmt.excluded.sequence_in_binding,
                "binding_notes": stmt.excluded.binding_notes,
            },
        )
        with self.get_corpus_connection() as conn:
            conn.execute(stmt)

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
            rows = conn.execute(stmt).fetchall()
            return [WorkCluster.model_validate(dict(r)) for r in rows]

    def get_expression_clusters_for_work(self, work_cluster_id: str) -> list[ExpressionCluster]:
        """Fetch all expression clusters under a work cluster."""
        stmt = (
            select(corpus_expression_clusters_table)
            .where(corpus_expression_clusters_table.c.work_cluster_id == work_cluster_id)
            .order_by(corpus_expression_clusters_table.c.cluster_id)
        )
        with self.get_corpus_connection() as conn:
            rows = conn.execute(stmt).fetchall()
            clusters = []
            for r in rows:
                d = dict(r)
                d["translators_json"] = (
                    json.loads(d.get("translators_json") or "[]")
                    if isinstance(d.get("translators_json"), str)
                    else d.get("translators_json") or []
                )
                d["performers_json"] = (
                    json.loads(d.get("performers_json") or "[]")
                    if isinstance(d.get("performers_json"), str)
                    else d.get("performers_json") or []
                )
                clusters.append(ExpressionCluster.model_validate(d))
            return clusters

    def get_all_expression_clusters(self) -> list[ExpressionCluster]:
        """Fetch all expression clusters."""
        stmt = select(corpus_expression_clusters_table).order_by(
            corpus_expression_clusters_table.c.cluster_id
        )
        with self.get_corpus_connection() as conn:
            rows = conn.execute(stmt).fetchall()
            clusters = []
            for r in rows:
                d = dict(r)
                d["translators_json"] = (
                    json.loads(d.get("translators_json") or "[]")
                    if isinstance(d.get("translators_json"), str)
                    else d.get("translators_json") or []
                )
                d["performers_json"] = (
                    json.loads(d.get("performers_json") or "[]")
                    if isinstance(d.get("performers_json"), str)
                    else d.get("performers_json") or []
                )
                clusters.append(ExpressionCluster.model_validate(d))
            return clusters

    def get_all_manifestation_clusters(self) -> list[ManifestationCluster]:
        """Fetch all manifestation clusters."""
        stmt = select(corpus_manifestation_clusters_table).order_by(
            corpus_manifestation_clusters_table.c.cluster_id
        )
        with self.get_corpus_connection() as conn:
            rows = conn.execute(stmt).fetchall()
            return [ManifestationCluster.model_validate(dict(r)) for r in rows]

    def get_corpus_linkage(self, record_id: str) -> Optional[CorpusLinkage]:
        """Fetch linkage record for a staged target record."""
        stmt = select(corpus_linkages_table).where(corpus_linkages_table.c.record_id == record_id)
        with self.get_corpus_connection() as conn:
            row = conn.execute(stmt).fetchone()
            return CorpusLinkage.model_validate(dict(row)) if row else None

    def get_all_corpus_linkages(self) -> list[CorpusLinkage]:
        """Fetch all corpus linkage records."""
        stmt = select(corpus_linkages_table).order_by(corpus_linkages_table.c.record_id)
        with self.get_corpus_connection() as conn:
            rows = conn.execute(stmt).fetchall()
            return [CorpusLinkage.model_validate(dict(r)) for r in rows]

    def get_sammelband_bindings(self, binding_id: str) -> list[SammelbandBinding]:
        """Fetch all constituent items in a composite binding."""
        stmt = (
            select(sammelband_bindings_table)
            .where(sammelband_bindings_table.c.binding_id == binding_id)
            .order_by(sammelband_bindings_table.c.sequence_in_binding)
        )
        with self.get_corpus_connection() as conn:
            rows = conn.execute(stmt).fetchall()
            return [SammelbandBinding.model_validate(dict(r)) for r in rows]
