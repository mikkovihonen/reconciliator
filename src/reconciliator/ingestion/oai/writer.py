"""Database writer for batching and flushing WEMI entities to storage."""

from __future__ import annotations

import logging
from typing import Any, Optional

from reconciliator.config import OAIIngestConfig
from reconciliator.database import WEMIRepository
from reconciliator.ingestion.sanitizer import sanitize_batch_payload
from reconciliator.progress import ProgressTracker

logger = logging.getLogger(__name__)


def write_wemi_batch(
    repo: WEMIRepository,
    payload: dict[str, Any],
    chunk_size: Optional[int] = None,
    config: Optional[OAIIngestConfig] = None,
    task_id: str = "",
    tracker: Optional[ProgressTracker] = None,
) -> int:
    """Persist a batch detached by WEMIBatchAccumulator.take() using multi-row bulk upserts."""
    if config is not None and config.truncate_overlong_values:
        sanitize_batch_payload(payload, config=config, task_id=task_id, tracker=tracker)

    record_count = int(payload.get("record_count") or 0)
    record_origin = str(payload.get("record_origin") or "unknown")

    works = payload.get("works") or []
    agents = payload.get("agents") or []
    manifestations = payload.get("manifestations") or []
    expressions = payload.get("expressions") or []
    if not (works or agents or manifestations or expressions):
        return 0

    try:
        repo.insert_wemi_batch(
            works=works,
            expressions=payload.get("expressions") or [],
            manifestations=payload.get("manifestations") or [],
            items=payload.get("items") or [],
            agents=agents,
            relationships=payload.get("relationships") or [],
            agent_relationships=payload.get("agent_relationships") or [],
            identifiers=payload.get("identifiers") or [],
            manifestation_expressions=payload.get("associations") or [],
            contributions=payload.get("contributions") or [],
            entity_keys=payload.get("entity_keys") or [],
            entity_subjects=payload.get("entity_subjects") or [],
            record_origin=record_origin,
            chunk_size=chunk_size,
            use_synchronous_commit_off=getattr(config, "use_synchronous_commit_off", False)
            if config
            else False,
        )
    except Exception as exc:
        from sqlalchemy.exc import DataError

        if isinstance(exc, DataError) and config is not None and config.truncate_overlong_values:
            from reconciliator.logging import log_validation

            log_validation(
                logger,
                action="skip_batch",
                entity="wemi_batch",
                source_id=record_origin,
                reason=f"database DataError: {exc}",
                task_id=task_id,
                tracker=tracker,
            )
            return 0
        raise

    return record_count or (len(works) or len(agents))
