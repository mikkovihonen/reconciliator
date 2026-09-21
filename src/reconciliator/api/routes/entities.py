"""WEMI entities browsing and hierarchy tree endpoints."""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query, Request, status

from reconciliator.database import WEMIRepository

router = APIRouter(tags=["Entities"])


@router.get("/works")
def list_works(
    request: Request,
    source: Optional[str] = None,
    work_type: Optional[str] = None,
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    """List paginated works."""
    repo: WEMIRepository = getattr(request.app.state, "repo", None)
    if not repo:
        return {"items": [], "total": 0}
    works, total = repo.get_works_paginated(source=source, work_type=work_type, limit=limit, offset=offset)
    return {"items": [w.model_dump() for w in works], "total": total}


@router.get("/works/{work_id}")
def get_work(request: Request, work_id: int) -> dict[str, Any]:
    """Retrieve detailed information about a single work."""
    repo: WEMIRepository = getattr(request.app.state, "repo", None)
    if not repo:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Work not found")
    w = repo.get_work(work_id)
    if not w:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Work not found")

    w_dict = w.model_dump()
    w_dict["identifiers"] = [i.model_dump() for i in repo.get_identifiers_for_entity("work", work_id)]
    w_dict["contributions"] = [c.model_dump() for c in repo.get_contributions_for_entity("work", work_id)]
    w_dict["subjects"] = [s.model_dump() for s in repo.get_subjects_for_entity("work", work_id)]
    w_dict["relationships"] = [r.model_dump() for r in repo.get_work_relationships(work_id)]
    return w_dict


@router.get("/works/{work_id}/tree")
def get_work_tree(request: Request, work_id: int) -> dict[str, Any]:
    """Retrieve full WEMI hierarchy for a work: Work ➔ Expressions ➔ Manifestations ➔ Items."""
    repo: WEMIRepository = getattr(request.app.state, "repo", None)
    if not repo:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Work not found")
    w = repo.get_work(work_id)
    if not w:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Work not found")

    expressions = repo.get_expressions_for_work(work_id)
    exp_tree = []
    for exp in expressions:
        exp_dict = exp.model_dump()
        manifs = repo.get_manifestations_for_expression(exp.id) if exp.id else []
        manif_list = []
        for m in manifs:
            m_dict = m.model_dump()
            items = repo.get_items_for_manifestation(m.id) if m.id else []
            m_dict["items"] = [it.model_dump() for it in items]
            manif_list.append(m_dict)
        exp_dict["manifestations"] = manif_list
        exp_tree.append(exp_dict)

    work_dict = w.model_dump()
    work_dict["expressions"] = exp_tree
    return work_dict


@router.get("/manifestations")
def list_manifestations(
    request: Request,
    source: Optional[str] = None,
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    """List paginated manifestations."""
    repo: WEMIRepository = getattr(request.app.state, "repo", None)
    if not repo:
        return {"items": [], "total": 0}
    manifs, total = repo.get_manifestations_paginated(source=source, limit=limit, offset=offset)
    return {"items": [m.model_dump() for m in manifs], "total": total}


@router.get("/manifestations/{manif_id}")
def get_manifestation(request: Request, manif_id: int) -> dict[str, Any]:
    """Retrieve detailed information about a single manifestation."""
    repo: WEMIRepository = getattr(request.app.state, "repo", None)
    if not repo:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Manifestation not found")
    m = repo.get_manifestation(manif_id)
    if not m:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Manifestation not found")

    m_dict = m.model_dump()
    m_dict["items"] = [it.model_dump() for it in repo.get_items_for_manifestation(manif_id)]
    m_dict["identifiers"] = [i.model_dump() for i in repo.get_identifiers_for_entity("manifestation", manif_id)]
    m_dict["expressions"] = [e.model_dump() for e in repo.get_expressions_for_manifestation(manif_id)]
    return m_dict


@router.get("/agents")
def list_agents(
    request: Request,
    source: Optional[str] = None,
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    """List paginated agents."""
    repo: WEMIRepository = getattr(request.app.state, "repo", None)
    if not repo:
        return {"items": [], "total": 0}
    agents, total = repo.get_agents_paginated(source=source, limit=limit, offset=offset)
    return {"items": [a.model_dump() for a in agents], "total": total}


@router.get("/agents/{agent_id}")
def get_agent(request: Request, agent_id: int) -> dict[str, Any]:
    """Retrieve detailed information about a single agent."""
    repo: WEMIRepository = getattr(request.app.state, "repo", None)
    if not repo:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")
    a = repo.get_agent(agent_id)
    if not a:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")

    a_dict = a.model_dump()
    a_dict["subjects"] = [s.model_dump() for s in repo.get_subjects_for_agent(agent_id)]
    a_dict["identifiers"] = [i.model_dump() for i in repo.get_identifiers_for_entity("agent", agent_id)]
    return a_dict

