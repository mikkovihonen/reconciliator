"""Authority agent cluster and lifecycle event data models."""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class AgentClusterMember(BaseModel):
    """Member agent linked to a canonical agent cluster."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    agent_id: int
    cluster_id: str
    source: str
    source_id: str
    link_method: str
    confidence: float = 1.0


class AgentClusterAttribute(BaseModel):
    """Aggregated cluster attribute with empirical corpus frequency."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    id: Optional[int] = None
    cluster_id: str
    attribute_type: str  # 'variant_name', 'qualifier', 'affiliation', 'occupation', 'role'
    value: str
    concept_id: Optional[str] = None
    frequency: int = 1
    is_canonical: bool = False


class AgentCluster(BaseModel):
    """Canonical agent cluster resolving multiple agent occurrences across sources."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    cluster_id: str
    canonical_agent_id: Optional[int] = None
    canonical_agent_source: Optional[str] = None
    canonical_agent_source_id: Optional[str] = None
    canonical_name: str
    canonical_surname: Optional[str] = None
    canonical_uri: Optional[str] = None
    birth_year: Optional[int] = None
    death_year: Optional[int] = None
    member_count: int = 1
    work_count: int = 0
    manifestation_count: int = 0
    confidence_score: float = 1.0
    authority_status: Literal["established", "provisional"] = "provisional"
    canonical_access_point: Optional[str] = None
    created_at: Optional[str | datetime] = None
    updated_at: Optional[str | datetime] = None
    members: list[AgentClusterMember] = Field(default_factory=list)
    attributes: list[AgentClusterAttribute] = Field(default_factory=list)


class ClusterLifecycleEvent(BaseModel):
    """Event recording the minting, merging, splitting, or deprecation of an authority cluster."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    id: Optional[int] = None
    event_type: Literal["mint", "merge", "split", "deprecate", "reinstate"]
    cluster_type: Literal["agent", "work", "expression"]
    cluster_id: str
    target_cluster_id: Optional[str] = None
    details_json: str = "{}"
    created_at: Optional[str | datetime] = None


class ClusterRedirection(BaseModel):
    """Historical or active redirection mapping from a merged/split/deprecated cluster ID to its target."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    source_cluster_id: str
    target_cluster_id: Optional[str] = None
    cluster_type: Literal["agent", "work", "expression"]
    status_code: int = 308
    reason: str = "merge"
    created_at: Optional[str | datetime] = None


class AgentClusterResult(BaseModel):
    """Summary result of an agent clustering run."""

    total_agents: int = 0
    total_clusters: int = 0
    singletons: int = 0
    multi_member_clusters: int = 0
    max_cluster_size: int = 0
    link_method_counts: dict[str, int] = Field(default_factory=dict)
