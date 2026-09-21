"""Consolidation and clustering endpoints for works and agents."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request, status

from reconciliator.database import WEMIRepository

router = APIRouter(prefix="/consolidation", tags=["Consolidation & Clusters"])


@router.get("/works")
def list_work_consolidations(
    request: Request,
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    """Retrieve paginated cross-source work equivalence groups."""
    repo: WEMIRepository = getattr(request.app.state, "repo", None)
    if not repo:
        return {"items": [], "total": 0}
    groups, total = repo.get_consolidation_groups_paginated(limit=limit, offset=offset)
    return {"items": groups, "total": total}


@router.get("/works/{canonical_id}/graph")
def get_work_consolidation_graph(
    request: Request,
    canonical_id: int,
) -> dict[str, Any]:
    """Retrieve Cytoscape node-link graph data for an equivalence group."""
    repo: WEMIRepository = getattr(request.app.state, "repo", None)
    if not repo:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Consolidation group not found")
    graph = repo.get_consolidation_group_graph(canonical_id)
    return graph


@router.get("/agents")
def list_agent_clusters(
    request: Request,
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    """Retrieve paginated agent clusters."""
    repo: WEMIRepository = getattr(request.app.state, "repo", None)
    if not repo:
        return {"items": [], "total": 0}
    clusters, total = repo.get_agent_clusters_paginated(limit=limit, offset=offset)
    return {"items": [c.model_dump() for c in clusters], "total": total}


@router.get("/agents/{cluster_id}")
def get_agent_cluster(
    request: Request,
    cluster_id: str,
) -> dict[str, Any]:
    """Retrieve detailed information about an agent cluster including members and attributes."""
    repo: WEMIRepository = getattr(request.app.state, "repo", None)
    if not repo:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent cluster not found")
    cluster = repo.get_agent_cluster(cluster_id)
    if not cluster:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent cluster not found")

    c_dict = cluster.model_dump()
    c_dict["members"] = [m.model_dump() for m in repo.get_agent_cluster_members(cluster_id)]
    c_dict["attributes"] = [a.model_dump() for a in repo.get_agent_cluster_attributes(cluster_id)]
    return c_dict

