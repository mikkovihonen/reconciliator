"""Harvest state checkpoint repository mixin."""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from reconciliator.database.schema import harvest_state_table
from reconciliator.models import HarvestState

logger = logging.getLogger(__name__)


def _row_to_harvest_state(row: Any) -> HarvestState:
    if hasattr(row, "_mapping"):
        d = dict(row._mapping)
    elif isinstance(row, dict):
        d = dict(row)
    else:
        d = dict(row)
    return HarvestState.model_validate(d)


class HarvestStateRepositoryMixin:
    """Repository mixin for harvest checkpoints and progress tracking."""

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
