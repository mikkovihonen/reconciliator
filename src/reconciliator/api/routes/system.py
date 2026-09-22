"""System health and data readiness endpoints."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Request, Response

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


@router.get("/bruno.zip", summary="Export Dynamic Bruno Test Suite (ZIP)")
@router.get("/bruno", summary="Export Dynamic Bruno Test Suite (ZIP)")
def get_bruno_test_suite_zip(
    request: Request,
    base_url: Optional[str] = None,
    include_assertions: Optional[bool] = None,
) -> Response:
    """Dynamically generate and export a complete Bruno API test suite as a ZIP archive.

    The collection is compiled from the live OpenAPI 3.x specification and includes:
    - Root bruno.json collection manifest
    - environments/<env>.bru with configured {{baseUrl}}
    - Individual .bru request files organized by route tags
    - Automated assertions for status codes and payloads
    """
    from reconciliator.api.bruno import BrunoCollectionGenerator
    from reconciliator.config import AppConfig, BrunoConfig

    app_cfg: AppConfig = getattr(request.app.state, "config", AppConfig())
    bruno_cfg = app_cfg.bruno.model_copy()

    # Dynamic overrides from request query parameters or request URL
    if base_url:
        bruno_cfg.base_url = base_url.rstrip("/")
    elif not bruno_cfg.base_url:
        bruno_cfg.base_url = str(request.base_url).rstrip("/")

    if include_assertions is not None:
        bruno_cfg.include_assertions = include_assertions

    generator = BrunoCollectionGenerator(
        openapi_spec=request.app.openapi(),
        config=bruno_cfg,
    )
    zip_bytes = generator.generate_zip_archive()

    return Response(
        content=zip_bytes,
        media_type="application/zip",
        headers={
            "Content-Disposition": 'attachment; filename="reconciliator-bruno.zip"',
            "Cache-Control": "no-cache, no-store, must-revalidate",
        },
    )


@router.get("/bruno/json", summary="Export Dynamic Bruno Test Suite (JSON)")
def get_bruno_test_suite_json(
    request: Request,
    base_url: Optional[str] = None,
    include_assertions: Optional[bool] = None,
) -> dict[str, Any]:
    """Inspect the dynamically generated Bruno test suite as structured JSON."""
    from reconciliator.api.bruno import BrunoCollectionGenerator
    from reconciliator.config import AppConfig

    app_cfg: AppConfig = getattr(request.app.state, "config", AppConfig())
    bruno_cfg = app_cfg.bruno.model_copy()

    if base_url:
        bruno_cfg.base_url = base_url.rstrip("/")
    elif not bruno_cfg.base_url:
        bruno_cfg.base_url = str(request.base_url).rstrip("/")

    if include_assertions is not None:
        bruno_cfg.include_assertions = include_assertions

    generator = BrunoCollectionGenerator(
        openapi_spec=request.app.openapi(),
        config=bruno_cfg,
    )
    return generator.generate_json_export()

