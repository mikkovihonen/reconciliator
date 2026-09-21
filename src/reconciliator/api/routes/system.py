"""System health and data readiness endpoints."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Request

from reconciliator.database import WEMIRepository
from reconciliator.models import DataReadinessSummary

router = APIRouter(prefix="/system", tags=["System"])


@router.get("/health")
def get_health() -> dict[str, Any]:
    """Basic health check endpoint."""
    return {
        "status": "ok",
        "service": "reconciliator",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@router.get("/readiness", response_model=DataReadinessSummary)
def get_readiness(request: Request) -> DataReadinessSummary:
    """Introspect database and vector store readiness across all pipeline stages."""
    repo: WEMIRepository = getattr(request.app.state, "repo", None)
    if not repo:
        return DataReadinessSummary(
            database_connected=False,
            overall_readiness="unpopulated",
            entity_counts={},
            vector_store={"available": False},
            pipeline_readiness={},
        )
    return repo.get_data_readiness_summary()

