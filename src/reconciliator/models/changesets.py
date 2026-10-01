"""Curation changesets, review operations, and audit models."""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class ChangesetOperation(BaseModel):
    """An individual staged curation operation within a review changeset."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    id: Optional[int] = None
    changeset_id: int
    sequence: int = 1
    operation_type: str
    entity_type: str
    entity_id: str
    target_id: Optional[str] = None
    parameters: dict[str, Any] = Field(default_factory=dict)
    pre_image: Optional[dict[str, Any]] = None
    notes: Optional[str] = None
    created_at: Optional[str] = None


class ReviewChangeset(BaseModel):
    """A review changeset staging manual curation operations before atomic commit."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    id: Optional[int] = None
    title: str
    description: Optional[str] = None
    status: Literal["draft", "submitted", "committed", "reverted", "archived"] = "draft"
    created_by: str
    assigned_to: Optional[str] = None
    committed_by: Optional[str] = None
    committed_at: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    operations: list[ChangesetOperation] = Field(default_factory=list)
    operations_count: int = 0


class EntityPin(BaseModel):
    """Pin indicating an entity decision made by a curator must not be overwritten by automated pipelines."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    id: Optional[int] = None
    entity_type: str
    entity_id: str
    changeset_id: int
    pinned_at: Optional[str] = None
    pinned_by: str


class DataReadinessSummary(BaseModel):
    """System state and data readiness inspection across all pipeline stages."""

    database_connected: bool = True
    overall_readiness: Literal["unpopulated", "partially_populated", "ready"] = "unpopulated"
    entity_counts: dict[str, int] = Field(default_factory=dict)
    vector_store: dict[str, Any] = Field(default_factory=dict)
    pipeline_readiness: dict[str, dict[str, Any]] = Field(default_factory=dict)


class UserProfile(BaseModel):
    """Authenticated user profile with role assignments."""

    sub: str
    email: str
    name: str
    roles: list[str] = Field(default_factory=list)
    is_admin: bool = False
    is_curator: bool = False
    is_reader: bool = True
