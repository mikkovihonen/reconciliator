"""Administrative and API pagination query repository mixin."""

from __future__ import annotations

import logging
from typing import Any, Optional

from sqlalchemy import and_, func, select

from reconciliator.database.mixins.common import row_to_dict
from reconciliator.database.schema import (
    agents_table,
    concept_mappings_table,
    concepts_table,
    corpus_agent_clusters_table,
    corpus_linkages_table,
    entity_pins_table,
    expressions_table,
    items_table,
    manifestations_table,
    review_changesets_table,
    sources_table,
    work_consolidations_table,
    work_relationships_table,
    works_table,
)
from reconciliator.models import (
    AgentCluster,
    AgentEntity,
    DataReadinessSummary,
    ManifestationEntity,
    WorkEntity,
)

logger = logging.getLogger(__name__)


def _row_to_work(row: Any) -> WorkEntity:
    return WorkEntity.model_validate(row_to_dict(row))


def _row_to_manifestation(row: Any) -> ManifestationEntity:
    return ManifestationEntity.model_validate(row_to_dict(row))


def _row_to_agent(row: Any) -> AgentEntity:
    return AgentEntity.model_validate(row_to_dict(row))


class PaginationRepositoryMixin:
    """Repository mixin for paginated entity queries, readiness summaries, and graph traversal."""

    def get_connection(self) -> Any:
        raise NotImplementedError

    def get_corpus_connection(self) -> Any:
        raise NotImplementedError

    def get_work(self, work_id: int) -> Optional[WorkEntity]:
        raise NotImplementedError

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
