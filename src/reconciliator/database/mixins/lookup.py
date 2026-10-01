"""Entity lookup repository mixin for WEMIRepository."""

from __future__ import annotations

import logging
from typing import Any, Optional

from sqlalchemy import select

from reconciliator.database.mixins.common import row_to_dict
from reconciliator.database.schema import (
    expressions_table,
    items_table,
    manifestations_table,
    works_table,
)
from reconciliator.models import (
    ExpressionEntity,
    ItemEntity,
    ManifestationEntity,
    WorkEntity,
)
from reconciliator.utils.curie import parse_curie

logger = logging.getLogger(__name__)


class EntityLookupRepositoryMixin:
    """Mixin providing direct ID and CURIE lookup for WEMI domain entities."""

    engine: Any

    def get_work(self, work_id: int) -> Optional[WorkEntity]:
        """Fetch WorkEntity by primary key integer id."""
        with self.engine.connect() as conn:
            row = conn.execute(works_table.select().where(works_table.c.id == work_id)).fetchone()
            if row is not None:
                return WorkEntity(**row_to_dict(row))
        return None

    def get_work_by_curie(self, curie: str) -> Optional[WorkEntity]:
        """Fetch WorkEntity by source:source_id CURIE."""
        source, source_id = parse_curie(curie)
        if not source or not source_id:
            return None
        with self.engine.connect() as conn:
            row = conn.execute(
                works_table.select().where(
                    works_table.c.source == source,
                    works_table.c.source_id == source_id,
                )
            ).fetchone()
            if row is not None:
                return WorkEntity(**row_to_dict(row))
        return None

    def get_manifestation(self, manifestation_id: int) -> Optional[ManifestationEntity]:
        """Fetch ManifestationEntity by primary key integer id."""
        with self.engine.connect() as conn:
            row = conn.execute(
                manifestations_table.select().where(manifestations_table.c.id == manifestation_id)
            ).fetchone()
            if row is not None:
                return ManifestationEntity(**row_to_dict(row))
        return None

    def get_manifestation_by_curie(self, curie: str) -> Optional[ManifestationEntity]:
        """Fetch ManifestationEntity by source:source_id CURIE."""
        source, source_id = parse_curie(curie)
        if not source or not source_id:
            return None
        with self.engine.connect() as conn:
            row = conn.execute(
                manifestations_table.select().where(
                    manifestations_table.c.source == source,
                    manifestations_table.c.source_id == source_id,
                )
            ).fetchone()
            if row is not None:
                return ManifestationEntity(**row_to_dict(row))
        return None

    def get_expression(self, expression_id: int) -> Optional[ExpressionEntity]:
        """Fetch ExpressionEntity by primary key integer id."""
        with self.engine.connect() as conn:
            row = conn.execute(
                expressions_table.select().where(expressions_table.c.id == expression_id)
            ).fetchone()
            if row is not None:
                return ExpressionEntity(**row_to_dict(row))
        return None

    def get_expression_by_curie(self, curie: str) -> Optional[ExpressionEntity]:
        """Fetch ExpressionEntity by source:source_id CURIE."""
        source, source_id = parse_curie(curie)
        if not source or not source_id:
            return None
        with self.engine.connect() as conn:
            row = conn.execute(
                expressions_table.select().where(
                    expressions_table.c.source == source,
                    expressions_table.c.source_id == source_id,
                )
            ).fetchone()
            if row is not None:
                return ExpressionEntity(**row_to_dict(row))
        return None

    def get_item(self, item_id: int) -> Optional[ItemEntity]:
        """Fetch ItemEntity by primary key integer id."""
        with self.engine.connect() as conn:
            row = conn.execute(items_table.select().where(items_table.c.id == item_id)).fetchone()
            if row is not None:
                return ItemEntity(**row_to_dict(row))
        return None

    def get_item_by_curie(self, curie: str) -> Optional[ItemEntity]:
        """Fetch ItemEntity by source:source_id CURIE."""
        source, source_id = parse_curie(curie)
        if not source or not source_id:
            return None
        with self.engine.connect() as conn:
            row = conn.execute(
                items_table.select().where(
                    items_table.c.source == source,
                    items_table.c.source_id == source_id,
                )
            ).fetchone()
            if row is not None:
                return ItemEntity(**row_to_dict(row))
        return None

