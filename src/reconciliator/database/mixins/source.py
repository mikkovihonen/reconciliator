"""Source management, origin statistics, and registry repository mixin."""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import Table, delete, distinct, func, select, union
from sqlalchemy.dialects.postgresql import insert as pg_insert

from reconciliator.database.schema import (
    agents_table,
    contributions_table,
    entity_keys_table,
    expressions_table,
    identifiers_table,
    items_table,
    manifestation_expressions_table,
    manifestation_vectors_table,
    manifestations_table,
    sources_table,
    work_relationships_table,
    work_vectors_table,
    works_table,
)

logger = logging.getLogger(__name__)


class SourceManagementMixin:
    """Repository mixin for source registration, validation, telemetry, and cascading purging."""

    def get_connection(self) -> Any:
        raise NotImplementedError

    def get_read_connection(self) -> Any:
        raise NotImplementedError
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

