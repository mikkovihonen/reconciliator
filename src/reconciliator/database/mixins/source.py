"""Source management and registry repository mixin for WEMIRepository."""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import delete, select

from reconciliator.database.schema import sources_table

logger = logging.getLogger(__name__)


class SourceManagementMixin:
    """Mixin providing source registration, checking, and registry inspection."""

    engine: Any

    def register_source(
        self,
        name: str,
        is_authoritative: bool = True,
        priority: int = 100,
        description: str = "",
    ) -> None:
        """Register a source in the sources table.

        Idempotent: updates authority status and priority if the source exists.
        """
        clean_name = name.strip()
        if not clean_name:
            return

        with self.engine.begin() as conn:
            existing = conn.execute(
                select(sources_table).where(sources_table.c.name == clean_name)
            ).fetchone()

            if existing:
                conn.execute(
                    sources_table.update()
                    .where(sources_table.c.name == clean_name)
                    .values(
                        is_authoritative=is_authoritative,
                        priority=priority,
                        description=description or existing.description,
                    )
                )
            else:
                conn.execute(
                    sources_table.insert().values(
                        name=clean_name,
                        is_authoritative=is_authoritative,
                        priority=priority,
                        description=description,
                    )
                )

    def is_source_authoritative(self, name: str) -> bool:
        """Check whether a source is marked as authoritative in the sources table."""
        clean_name = name.strip()
        if not clean_name:
            return True

        with self.engine.connect() as conn:
            row = conn.execute(
                select(sources_table.c.is_authoritative).where(sources_table.c.name == clean_name)
            ).fetchone()
            if row is not None:
                return bool(row[0])

        return True

    def get_source_priority(self, name: str) -> int:
        """Return priority score for a source from the sources table (default 100)."""
        clean_name = name.strip()
        if not clean_name:
            return 100

        with self.engine.connect() as conn:
            row = conn.execute(
                select(sources_table.c.priority).where(sources_table.c.name == clean_name)
            ).fetchone()
            if row is not None:
                return int(row[0])

        return 100

    def check_source_registry(self) -> list[str]:
        """Verify sources table alignment against manifestations."""
        findings: list[str] = []
        with self.engine.connect() as conn:
            from reconciliator.database.schema import manifestations_table

            res = conn.execute(
                select(manifestations_table.c.record_origin).distinct()
            ).fetchall()
            active_origins = {r[0] for r in res if r[0]}

            registered_res = conn.execute(select(sources_table.c.name)).fetchall()
            registered = {r[0] for r in registered_res}

            unregistered = active_origins - registered
            for u in unregistered:
                findings.append(f"Origin '{u}' found in manifestations but not in sources registry")

        return findings

    def get_registered_sources(self) -> list[dict[str, Any]]:
        """Return list of all registered source records."""
        with self.engine.connect() as conn:
            rows = conn.execute(select(sources_table).order_by(sources_table.c.priority)).fetchall()
            return [dict(r._mapping) for r in rows]

    def delete_source(self, name: str) -> bool:
        """Remove a source registration from the sources table."""
        with self.engine.begin() as conn:
            res = conn.execute(delete(sources_table).where(sources_table.c.name == name))
            return res.rowcount > 0

