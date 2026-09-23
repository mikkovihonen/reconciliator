"""Review changesets and curation staging endpoints."""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from reconciliator.api.auth import get_current_user, require_admin, require_curator
from reconciliator.database import WEMIRepository
from reconciliator.models import ChangesetOperation, ReviewChangeset, UserProfile

router = APIRouter(prefix="/changesets", tags=["Review Changesets"])


class CreateChangesetRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=256)
    description: Optional[str] = None
    assigned_to: Optional[str] = None


class AddOperationRequest(BaseModel):
    operation_type: str
    entity_type: str
    entity_id: str
    target_id: Optional[str] = None
    parameters: dict[str, Any] = Field(default_factory=dict)
    pre_image: Optional[dict[str, Any]] = None
    notes: Optional[str] = None


@router.get("", response_model=dict[str, Any])
def list_changesets(
    request: Request,
    status_filter: Optional[str] = Query(None, alias="status"),
    created_by: Optional[str] = None,
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    user: UserProfile = Depends(get_current_user),
) -> dict[str, Any]:
    """List review changesets with optional status and author filters."""
    repo: WEMIRepository = getattr(request.app.state, "repo", None)
    if not repo:
        return {"items": [], "total": 0}
    items, total = repo.list_changesets(
        status=status_filter, created_by=created_by, limit=limit, offset=offset
    )
    return {"items": [item.model_dump() for item in items], "total": total}


@router.post("", response_model=ReviewChangeset, status_code=status.HTTP_201_CREATED)
def create_changeset(
    request: Request,
    payload: CreateChangesetRequest,
    user: UserProfile = Depends(require_curator),
) -> ReviewChangeset:
    """Create a new staged review changeset (Curators and Admins)."""
    repo: WEMIRepository = getattr(request.app.state, "repo", None)
    if not repo:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Database repository uninitialized",
        )

    return repo.create_changeset(
        title=payload.title,
        description=payload.description,
        created_by=user.email,
        assigned_to=payload.assigned_to,
    )


@router.get("/{changeset_id}", response_model=ReviewChangeset)
def get_changeset(
    request: Request,
    changeset_id: int,
    user: UserProfile = Depends(get_current_user),
) -> ReviewChangeset:
    """Retrieve full changeset details including staged operations."""
    repo: WEMIRepository = getattr(request.app.state, "repo", None)
    if not repo:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Changeset not found")
    cs = repo.get_changeset(changeset_id)
    if not cs:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Changeset {changeset_id} not found"
        )
    return cs


@router.post(
    "/{changeset_id}/operations",
    response_model=ChangesetOperation,
    status_code=status.HTTP_201_CREATED,
)
def add_operation(
    request: Request,
    changeset_id: int,
    payload: AddOperationRequest,
    user: UserProfile = Depends(require_curator),
) -> ChangesetOperation:
    """Stage a curation operation inside a draft changeset (Curators and Admins)."""
    repo: WEMIRepository = getattr(request.app.state, "repo", None)
    if not repo:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Database repository uninitialized",
        )

    cs = repo.get_changeset(changeset_id)
    if not cs:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Changeset {changeset_id} not found"
        )
    if cs.status != "draft":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot add operations to a {cs.status} changeset",
        )

    return repo.add_changeset_operation(
        changeset_id=changeset_id,
        operation_type=payload.operation_type,
        entity_type=payload.entity_type,
        entity_id=payload.entity_id,
        target_id=payload.target_id,
        parameters=payload.parameters,
        pre_image=payload.pre_image,
        notes=payload.notes,
    )


@router.delete("/{changeset_id}/operations/{operation_id}")
def delete_operation(
    request: Request,
    changeset_id: int,
    operation_id: int,
    user: UserProfile = Depends(require_curator),
) -> dict[str, Any]:
    """Remove a staged operation from a draft changeset (Curators and Admins)."""
    repo: WEMIRepository = getattr(request.app.state, "repo", None)
    if not repo:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Database repository uninitialized",
        )

    cs = repo.get_changeset(changeset_id)
    if not cs:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Changeset {changeset_id} not found"
        )
    if cs.status != "draft":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=f"Cannot modify a {cs.status} changeset"
        )

    success = repo.delete_changeset_operation(operation_id)
    if not success:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Operation {operation_id} not found"
        )
    return {"status": "deleted", "operation_id": operation_id}


@router.post("/{changeset_id}/commit", response_model=ReviewChangeset)
def commit_changeset(
    request: Request,
    changeset_id: int,
    user: UserProfile = Depends(require_admin),
) -> ReviewChangeset:
    """Atomically commit all staged operations and pin decisions (Admin only)."""
    repo: WEMIRepository = getattr(request.app.state, "repo", None)
    if not repo:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Database repository uninitialized",
        )

    try:
        committed = repo.commit_changeset(changeset_id, committed_by=user.email)
        return committed
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Commit failed: {e}"
        ) from e


@router.post("/{changeset_id}/revert", response_model=ReviewChangeset)
def revert_changeset(
    request: Request,
    changeset_id: int,
    user: UserProfile = Depends(require_admin),
) -> ReviewChangeset:
    """Rollback a committed changeset and release entity pins (Admin only)."""
    repo: WEMIRepository = getattr(request.app.state, "repo", None)
    if not repo:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Database repository uninitialized",
        )

    try:
        reverted = repo.revert_changeset(changeset_id, reverted_by=user.email)
        return reverted
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Revert failed: {e}"
        ) from e
