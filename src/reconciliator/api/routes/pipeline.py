"""Pipeline execution and real-time task progress streaming."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, AsyncGenerator

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select

from reconciliator.api.auth import get_current_user, require_admin
from reconciliator.database import WEMIRepository, task_progress_table
from reconciliator.models import TaskProgress, UserProfile

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/pipeline", tags=["Pipeline & Telemetry"])


def _fetch_tasks(repo: WEMIRepository) -> list[TaskProgress]:
    """Query task_progress table for active and recent pipeline tasks."""
    stmt = select(task_progress_table).order_by(task_progress_table.c.start_time.desc()).limit(50)
    with repo.get_connection() as conn:
        rows = conn.execute(stmt).mappings().fetchall()
        tasks: list[TaskProgress] = []
        for r in rows:
            d = dict(r)
            if isinstance(d.get("metadata_json"), str):
                try:
                    d["metadata_dict"] = json.loads(d["metadata_json"])
                except Exception:
                    d["metadata_dict"] = {}
            tasks.append(TaskProgress.model_validate(d))
        return tasks


@router.get("/tasks")
def list_tasks(
    request: Request,
    user: UserProfile = Depends(get_current_user),
) -> dict[str, Any]:
    """List ongoing and completed background tasks (all authenticated users)."""
    repo: WEMIRepository = getattr(request.app.state, "repo", None)
    if not repo:
        return {"tasks": []}
    tasks = _fetch_tasks(repo)
    return {"tasks": [t.model_dump() for t in tasks]}


@router.get("/stream")
async def stream_progress(request: Request) -> StreamingResponse:
    """Server-Sent Events (SSE) streaming live task progress updates."""
    repo: WEMIRepository = getattr(request.app.state, "repo", None)

    async def event_generator() -> AsyncGenerator[str, None]:
        while True:
            if await request.is_disconnected():
                break
            if repo:
                try:
                    tasks = _fetch_tasks(repo)
                    data = json.dumps([t.model_dump() for t in tasks], ensure_ascii=False)
                    yield f"data: {data}\n\n"
                except Exception as e:
                    logger.debug("Error streaming task progress: %s", e)
            await asyncio.sleep(2.0)

    return StreamingResponse(event_generator(), media_type="text/event-stream")


def _run_consolidate_works(repo: WEMIRepository, config: Any) -> None:
    from reconciliator.reconciliation.consolidation import WorkConsolidator

    try:
        consolidator = WorkConsolidator(repo=repo, config=config.consolidation)
        consolidator.consolidate(dry_run=False)
    except Exception as e:
        logger.error("Background consolidate-works failed: %s", e, exc_info=True)


def _run_cluster_agents(repo: WEMIRepository, config: Any) -> None:
    from reconciliator.clustering import AgentClusterer

    try:
        clusterer = AgentClusterer(repo=repo, config=config.agent_clustering)
        clusterer.cluster_agents()
    except Exception as e:
        logger.error("Background cluster-agents failed: %s", e, exc_info=True)


@router.post("/run/{task_type}", status_code=status.HTTP_202_ACCEPTED)
def trigger_pipeline_task(
    request: Request,
    task_type: str,
    background_tasks: BackgroundTasks,
    user: UserProfile = Depends(require_admin),
) -> dict[str, Any]:
    """Trigger execution of an asynchronous batch pipeline task (Admin only)."""
    repo: WEMIRepository = getattr(request.app.state, "repo", None)
    cfg = getattr(request.app.state, "config", None)

    if not repo or not cfg:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Server state uninitialized"
        )

    supported = ("consolidate-works", "cluster-agents", "build-index")
    if task_type not in supported:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported task type '{task_type}'. Supported: {', '.join(supported)}",
        )

    if task_type == "consolidate-works":
        background_tasks.add_task(_run_consolidate_works, repo, cfg)
    elif task_type == "cluster-agents":
        background_tasks.add_task(_run_cluster_agents, repo, cfg)
    elif task_type == "build-index":
        embedder = getattr(request.app.state, "embedder", None)
        if not embedder:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="Embedder is not initialized"
            )
        background_tasks.add_task(repo.build_wemi_indexes, embedder=embedder)

    return {
        "status": "accepted",
        "task_type": task_type,
        "message": f"Task '{task_type}' launched in background",
        "triggered_by": user.email,
    }
