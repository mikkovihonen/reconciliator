"""Reconciliation results and candidate review endpoints."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request, status

from reconciliator.database import WEMIRepository

router = APIRouter(prefix="/reconciliation", tags=["Reconciliation"])


@router.get("/records")
def list_reconciliation_records(
    request: Request,
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    """Retrieve paginated reconciled corpus records."""
    repo: WEMIRepository = getattr(request.app.state, "repo", None)
    if not repo:
        return {"items": [], "total": 0}
    records, total = repo.get_reconciliation_records_paginated(limit=limit, offset=offset)
    return {"items": records, "total": total}


@router.get("/records/{record_id}")
def get_reconciliation_record(
    request: Request,
    record_id: str,
) -> dict[str, Any]:
    """Retrieve detailed reconciliation match information for a target corpus record."""
    repo: WEMIRepository = getattr(request.app.state, "repo", None)
    if not repo:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Record not found")

    linkage = repo.get_corpus_linkage(record_id)
    if not linkage:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Reconciled record {record_id} not found")

    result: dict[str, Any] = {
        "record_id": record_id,
        "linkage": linkage.model_dump(),
        "is_pinned": repo.is_entity_pinned("corpus_linkage", record_id),
    }

    # Retrieve work cluster if available
    if linkage.work_cluster_id:
        clusters = repo.get_all_work_clusters()
        for wc in clusters:
            if wc.cluster_id == linkage.work_cluster_id:
                result["work_cluster"] = wc.model_dump()
                break

    # Retrieve expression cluster if available
    if linkage.expression_cluster_id:
        exp_clusters = repo.get_all_expression_clusters()
        for ec in exp_clusters:
            if ec.cluster_id == linkage.expression_cluster_id:
                result["expression_cluster"] = ec.model_dump()
                break

    # Retrieve manifestation cluster if available
    if linkage.manifestation_cluster_id:
        manif_clusters = repo.get_all_manifestation_clusters()
        for mc in manif_clusters:
            if mc.cluster_id == linkage.manifestation_cluster_id:
                result["manifestation_cluster"] = mc.model_dump()
                break

    return result

