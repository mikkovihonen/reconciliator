"""Full-text search, dense vector retrieval, and search index lifecycle mixin."""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

import numpy as np
from sqlalchemy import and_, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from reconciliator.database.mixins.common import row_to_dict
from reconciliator.database.schema import (
    agents_table,
    manifestation_vectors_table,
    manifestations_table,
    work_relationships_table,
    work_vectors_table,
    works_table,
)
from reconciliator.models import ManifestationEntity, WorkEntity

if TYPE_CHECKING:
    from reconciliator.embedding.embedder import VectorEmbedder
    from reconciliator.vector_indices import VectorIndex

logger = logging.getLogger(__name__)


def _row_to_work(row: Any) -> WorkEntity:
    return WorkEntity.model_validate(row_to_dict(row))


class SearchRepositoryMixin:
    """Repository mixin for FTS search, vector similarity search, and search index management."""

    def get_connection(self) -> Any:
        raise NotImplementedError

    def get_read_connection(self) -> Any:
        raise NotImplementedError

    def authoritative_sources(self) -> list[str]:
        raise NotImplementedError

    def get_works_by_ids(self, work_ids: list[int]) -> dict[int, WorkEntity]:
        raise NotImplementedError

    def get_manifestation(self, manifestation_id: int) -> Optional[ManifestationEntity]:
        raise NotImplementedError

    def embed_texts_cached(
        self,
        embedder: VectorEmbedder,
        texts: list[str],
        entity_type: Optional[str] = None,
    ) -> np.ndarray:
        raise NotImplementedError

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

        fts_cfg = getattr(self, "fts_config", None)
        eff_limit = limit if limit is not None else (fts_cfg.default_limit if fts_cfg else 20)
        eff_prefix = (
            prefix_search if prefix_search is not None else (fts_cfg.prefix_search if fts_cfg else True)
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
        indexer = getattr(self, "work_indexer", None)
        if indexer is None or indexer.total_vectors == 0 or len(query_vectors) == 0:
            return [[] for _ in range(len(query_vectors))]

        fetch_k = top_k * max(1, overfetch) if authoritative_only else top_k
        scores_matrix, indices_matrix = indexer.search(query_vectors, top_k=fetch_k)
        if len(scores_matrix) == 0 or len(indices_matrix) == 0:
            return [[] for _ in range(len(query_vectors))]

        allowed = set(self.authoritative_sources()) if authoritative_only else None
        if not allowed:
            allowed = None

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
        indexer = getattr(self, "manifestation_indexer", None)
        if indexer is None or indexer.total_vectors == 0:
            return []

        fetch_k = top_k * max(1, overfetch) if authoritative_only else top_k
        scores, indices = indexer.search(query_vector, top_k=fetch_k)
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
        embedder: VectorEmbedder,
        work_indexer: Optional[VectorIndex] = None,
        manifestation_indexer: Optional[VectorIndex] = None,
        batch_size: int = 128,
        include_constituent_works: bool = False,
    ) -> dict[str, int]:
        """Embed stored works and manifestations into their vector indexes."""
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
                    vectors = self.embed_texts_cached(embedder, texts, entity_type=kind)
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
        embedder: VectorEmbedder,
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
                    vectors = self.embed_texts_cached(embedder, texts, entity_type="agent")
                    agent_indexer.add_vectors(vectors, ids=ids)
                    total_indexed += len(texts)

        agent_indexer.save()
        return total_indexed

    def save_indices(self, metadata: Optional[dict[str, Any]] = None) -> None:
        """Persist all active vector indices to disk."""
        work_indexer = getattr(self, "work_indexer", None)
        manifestation_indexer = getattr(self, "manifestation_indexer", None)
        agent_indexer = getattr(self, "agent_indexer", None)

        if work_indexer is not None:
            work_indexer.save(metadata=metadata)
        if manifestation_indexer is not None:
            manifestation_indexer.save(metadata=metadata)
        if agent_indexer is not None:
            agent_indexer.save(metadata=metadata)
