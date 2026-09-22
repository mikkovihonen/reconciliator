"""FastAPI application factory and middleware configuration."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from reconciliator.api.routes.auth_config import router as auth_router
from reconciliator.api.routes.authority import (
    oai_router,
    reconcile_router,
    resolver_router,
)
from reconciliator.api.routes.changesets import router as changesets_router
from reconciliator.api.routes.consolidation import router as consolidation_router
from reconciliator.api.routes.entities import router as entities_router
from reconciliator.api.routes.pipeline import router as pipeline_router
from reconciliator.api.routes.reconciliation import router as reconciliation_router
from reconciliator.api.routes.search import router as search_router
from reconciliator.api.routes.system import router as system_router
from reconciliator.config import AppConfig
from reconciliator.database import WEMIRepository

logger = logging.getLogger(__name__)


def create_app(
    config: Optional[AppConfig] = None,
    repo: Optional[WEMIRepository] = None,
    embedder: Optional[Any] = None,
) -> FastAPI:
    """Create and configure Reconciliator FastAPI application."""
    app = FastAPI(
        title="Reconciliator API",
        description="High-Performance Bibliographic Authority Reconciliation & Curation API",
        version="0.1.0",
        docs_url="/api/docs",
        redoc_url="/api/redoc",
        openapi_url="/api/openapi.json",
    )

    # Store state
    app.state.config = config or AppConfig()
    app.state.repo = repo
    app.state.embedder = embedder

    # CORS configuration
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Mount API v1 routers
    api_v1_prefix = "/api/v1"
    app.include_router(auth_router, prefix=api_v1_prefix)
    app.include_router(system_router, prefix=api_v1_prefix)
    app.include_router(search_router, prefix=api_v1_prefix)
    app.include_router(entities_router, prefix=api_v1_prefix)
    app.include_router(consolidation_router, prefix=api_v1_prefix)
    app.include_router(reconciliation_router, prefix=api_v1_prefix)
    app.include_router(changesets_router, prefix=api_v1_prefix)
    app.include_router(pipeline_router, prefix=api_v1_prefix)

    # Mount Virtual Authority File (VAF) routers
    app.include_router(reconcile_router)
    app.include_router(resolver_router)
    app.include_router(oai_router)

    # Static assets directory for Web UI
    static_dir = Path(__file__).parent / "static"
    static_dir.mkdir(parents=True, exist_ok=True)

    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    root_assets = Path(__file__).resolve().parents[3] / "assets"
    if root_assets.exists():
        app.mount("/assets", StaticFiles(directory=str(root_assets)), name="assets")

    if static_dir.exists():
        from fastapi import HTTPException

        # Catch-all route to serve SPA index.html for client-side routing
        @app.get("/{full_path:path}", include_in_schema=False)
        async def serve_spa(request: Request, full_path: str):
            # Never intercept API, authority, oai, static, or assets requests with index.html
            if (
                full_path.startswith("api/")
                or full_path.startswith("authority/")
                or full_path.startswith("oai/")
                or full_path.startswith("static/")
                or full_path.startswith("assets/")
            ):
                raise HTTPException(status_code=404, detail="Not Found")

            index_file = static_dir / "index.html"
            if index_file.exists():
                return FileResponse(str(index_file))
            return {
                "message": "Reconciliator API is running. Web UI assets not found in static directory.",
                "docs": "/api/docs",
                "readiness": "/api/v1/system/readiness",
            }

    return app
