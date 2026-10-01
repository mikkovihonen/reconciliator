"""Concept hierarchy storage and graph traversal mixin for VocabStore."""

from __future__ import annotations

import functools
import logging
from typing import TYPE_CHECKING, Any, Optional, Protocol

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from reconciliator.database import calculate_batch_size, concept_hierarchy_table

if TYPE_CHECKING:
    from sqlalchemy import Engine

    from reconciliator.config import VocabulariesConfig

logger = logging.getLogger(__name__)


class HierarchyStoreProtocol(Protocol):
    """Protocol for hierarchy store dependencies."""

    engine: Engine
    config: VocabulariesConfig

    def clear_caches(self) -> None: ...


class HierarchyStoreMixin:
    """Manages concept hierarchy relations (skos:broader / skos:narrower) and recursive tree traversals."""

    def insert_hierarchy_batch(
        self: HierarchyStoreProtocol, edges: list[dict[str, Any]]
    ) -> None:
        """Bulk insert concept hierarchy edges (skos:broader / skos:narrower).

        Each edge dict must provide:
          - vocabulary: str
          - concept_id: str (the narrower concept)
          - broader_concept_id: str (the broader concept)
          - relation_type: Optional[str] (default 'broader')
        """
        if not edges:
            return

        seen: set[tuple[str, str, str]] = set()
        clean_rows: list[dict[str, Any]] = []
        for e in edges:
            voc = str(e["vocabulary"]).strip().lower()
            cid = str(e["concept_id"]).strip()
            pcid = str(e["broader_concept_id"]).strip()
            if not voc or not cid or not pcid or cid == pcid:
                continue
            key = (voc, cid, pcid)
            if key not in seen:
                seen.add(key)
                clean_rows.append(
                    {
                        "vocabulary": voc,
                        "concept_id": cid,
                        "broader_concept_id": pcid,
                        "relation_type": str(e.get("relation_type", "broader")),
                    }
                )

        if not clean_rows:
            return

        hierarchy_batch_size = calculate_batch_size(num_columns=len(concept_hierarchy_table.c))
        with self.engine.begin() as conn:
            for i in range(0, len(clean_rows), hierarchy_batch_size):
                chunk = clean_rows[i : i + hierarchy_batch_size]
                stmt = pg_insert(concept_hierarchy_table).values(chunk)
                stmt = stmt.on_conflict_do_nothing(
                    index_elements=["vocabulary", "concept_id", "broader_concept_id"]
                )
                conn.execute(stmt)

        self.clear_caches()

    @functools.lru_cache(maxsize=32768)
    def get_broader_concepts(
        self: HierarchyStoreProtocol,
        vocabulary: str,
        concept_id: str,
        max_depth: Optional[int] = None,
    ) -> list[tuple[str, int]]:
        """Query broader concepts (ancestors) in a hierarchy using recursive CTE.

        Returns list of (broader_concept_id, depth) sorted by shortest distance.
        """
        if not vocabulary or not concept_id:
            return []
        clean_voc = str(vocabulary).strip().lower()
        clean_id = str(concept_id).strip()
        depth_limit = max_depth or self.config.hierarchy.max_traversal_depth

        query = text(
            """
            WITH RECURSIVE broader_tree(concept_id, broader_concept_id, depth) AS (
                SELECT concept_id, broader_concept_id, 1
                FROM concept_hierarchy
                WHERE vocabulary = :vocab AND concept_id = :cid
              UNION
                SELECT bt.concept_id, h.broader_concept_id, bt.depth + 1
                FROM concept_hierarchy h
                JOIN broader_tree bt ON h.concept_id = bt.broader_concept_id
                WHERE h.vocabulary = :vocab AND bt.depth < :max_depth
            )
            SELECT broader_concept_id, min(depth) as min_depth
            FROM broader_tree
            GROUP BY broader_concept_id
            ORDER BY min_depth ASC;
            """
        )

        with self.engine.connect() as conn:
            rows = conn.execute(
                query, {"vocab": clean_voc, "cid": clean_id, "max_depth": depth_limit}
            ).fetchall()
            return [(str(r[0]), int(r[1])) for r in rows]

    @functools.lru_cache(maxsize=32768)
    def get_narrower_concepts(
        self: HierarchyStoreProtocol,
        vocabulary: str,
        concept_id: str,
        max_depth: Optional[int] = None,
    ) -> list[tuple[str, int]]:
        """Query narrower concepts (descendants) in a hierarchy using recursive CTE.

        Returns list of (narrower_concept_id, depth) sorted by shortest distance.
        """
        if not vocabulary or not concept_id:
            return []
        clean_voc = str(vocabulary).strip().lower()
        clean_id = str(concept_id).strip()
        depth_limit = max_depth or self.config.hierarchy.max_traversal_depth

        query = text(
            """
            WITH RECURSIVE narrower_tree(narrower_concept_id, broader_concept_id, depth) AS (
                SELECT concept_id, broader_concept_id, 1
                FROM concept_hierarchy
                WHERE vocabulary = :vocab AND broader_concept_id = :cid
              UNION
                SELECT h.concept_id, nt.broader_concept_id, nt.depth + 1
                FROM concept_hierarchy h
                JOIN narrower_tree nt ON h.broader_concept_id = nt.narrower_concept_id
                WHERE h.vocabulary = :vocab AND nt.depth < :max_depth
            )
            SELECT narrower_concept_id, min(depth) as min_depth
            FROM narrower_tree
            GROUP BY narrower_concept_id
            ORDER BY min_depth ASC;
            """
        )

        with self.engine.connect() as conn:
            rows = conn.execute(
                query, {"vocab": clean_voc, "cid": clean_id, "max_depth": depth_limit}
            ).fetchall()
            return [(str(r[0]), int(r[1])) for r in rows]

    @functools.lru_cache(maxsize=32)
    def load_hierarchy_edges(
        self: HierarchyStoreProtocol, vocabulary: str
    ) -> list[tuple[str, str]]:
        """Load all (concept_id, broader_concept_id) edges for a vocabulary into memory."""
        if not vocabulary:
            return []
        clean_voc = str(vocabulary).strip().lower()
        with self.engine.connect() as conn:
            stmt = select(
                concept_hierarchy_table.c.concept_id,
                concept_hierarchy_table.c.broader_concept_id,
            ).where(concept_hierarchy_table.c.vocabulary == clean_voc)
            rows = conn.execute(stmt).fetchall()
            return [(str(r[0]), str(r[1])) for r in rows]
