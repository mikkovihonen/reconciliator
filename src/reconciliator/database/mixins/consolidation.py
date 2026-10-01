"""Work consolidation and intra-cluster co-reference repository mixin."""

from __future__ import annotations

import logging
from typing import Any, Optional

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from reconciliator.database.schema import (
    work_consolidations_table,
    work_relationships_table,
)

logger = logging.getLogger(__name__)


class ConsolidationRepositoryMixin:
    """Repository mixin providing work consolidation and equivalence mapping queries."""

    def get_connection(self) -> Any:
        raise NotImplementedError

    def get_read_connection(self) -> Any:
        raise NotImplementedError
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
