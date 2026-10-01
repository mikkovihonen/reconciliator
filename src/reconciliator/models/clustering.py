"""WEMI and corpus multiplex clustering data models."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from reconciliator.models.entities import ExpressionContentType, WorkType
from reconciliator.utils.curie import parse_curie, to_curie


class ManifestationCluster(BaseModel):
    """Carrier-level duplicate cluster in reconcile_corpus.db."""

    cluster_id: str
    canonical_record_id: str
    publisher: Optional[str] = None
    publication_year: Optional[int] = None
    edition_statement: Optional[str] = None
    extent: Optional[str] = None
    carrier_type: Optional[str] = None
    member_record_count: int = 1


class WorkCluster(BaseModel):
    """Discovered Work family cluster in reconcile_corpus.db."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    cluster_id: str
    work_type: WorkType = "text"
    canonical_work_source: Optional[str] = None
    canonical_work_source_id: Optional[str] = None
    canonical_title: str
    canonical_author: Optional[str] = None
    authority_status: Literal["established", "provisional"] = "provisional"
    canonical_access_point: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    @field_validator("created_at", "updated_at", mode="before")
    @classmethod
    def _coerce_datetime_str(cls, v: Any) -> Optional[str]:
        if isinstance(v, datetime):
            return v.isoformat()
        if v is not None:
            return str(v)
        return None

    @model_validator(mode="before")
    @classmethod
    def _sync_cluster(cls, data: Any) -> Any:
        if isinstance(data, dict):
            raw = data.get("canonical_work_curie") or data.get("canonical_work_uri")
            if raw and (
                not data.get("canonical_work_source") or not data.get("canonical_work_source_id")
            ):
                src, sid = parse_curie(str(raw))
                data["canonical_work_source"] = src
                data["canonical_work_source_id"] = sid
        return data

    @property
    def canonical_work_curie(self) -> Optional[str]:
        if self.canonical_work_source_id:
            return to_curie(self.canonical_work_source or "", self.canonical_work_source_id)
        return None

    @property
    def canonical_work_uri(self) -> Optional[str]:
        return self.canonical_work_curie


class ExpressionCluster(BaseModel):
    """Discovered Expression realization cluster in reconcile_corpus.db."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    cluster_id: str
    work_cluster_id: str
    canonical_expression_source: Optional[str] = None
    canonical_expression_source_id: Optional[str] = None
    content_type: ExpressionContentType = "text"
    language: str
    version_designation: Optional[str] = None

    @model_validator(mode="before")
    @classmethod
    def _sync_exp_cluster(cls, data: Any) -> Any:
        if isinstance(data, dict):
            raw = data.get("canonical_expression_curie") or data.get("canonical_expression_uri")
            if raw and (
                not data.get("canonical_expression_source")
                or not data.get("canonical_expression_source_id")
            ):
                src, sid = parse_curie(str(raw))
                data["canonical_expression_source"] = src
                data["canonical_expression_source_id"] = sid
        return data

    @property
    def canonical_expression_curie(self) -> Optional[str]:
        if self.canonical_expression_source_id:
            return to_curie(
                self.canonical_expression_source or "", self.canonical_expression_source_id
            )
        return None

    @property
    def canonical_expression_uri(self) -> Optional[str]:
        return self.canonical_expression_curie


class CorpusLinkage(BaseModel):
    """Final record-to-cluster resolution mapping in reconcile_corpus.db."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    record_id: str
    manifestation_cluster_id: Optional[str] = None
    work_cluster_id: str
    expression_cluster_id: str
    final_work_source: str = ""
    final_work_source_id: str = ""
    final_expression_source: str = ""
    final_expression_source_id: str = ""

    @model_validator(mode="before")
    @classmethod
    def _sync_linkage(cls, data: Any) -> Any:
        if isinstance(data, dict):
            raw_w = data.get("final_work_curie") or data.get("final_work_uri")
            if raw_w and (
                not data.get("final_work_source") or not data.get("final_work_source_id")
            ):
                src, sid = parse_curie(str(raw_w))
                data["final_work_source"] = src
                data["final_work_source_id"] = sid
            raw_e = data.get("final_expression_curie") or data.get("final_expression_uri")
            if raw_e and (
                not data.get("final_expression_source")
                or not data.get("final_expression_source_id")
            ):
                src, sid = parse_curie(str(raw_e))
                data["final_expression_source"] = src
                data["final_expression_source_id"] = sid
        return data

    @property
    def final_work_curie(self) -> str:
        if self.final_work_source_id:
            return to_curie(self.final_work_source or "", self.final_work_source_id)
        return ""

    @property
    def final_work_uri(self) -> str:
        return self.final_work_curie

    @property
    def final_expression_curie(self) -> str:
        if self.final_expression_source_id:
            return to_curie(self.final_expression_source or "", self.final_expression_source_id)
        return ""

    @property
    def final_expression_uri(self) -> str:
        return self.final_expression_curie


class CorpusConstituentLinkage(BaseModel):
    """Constituent work/expression resolution mapping within an aggregate in reconcile_corpus.db."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    id: Optional[int] = None
    record_id: str
    constituent_ordinal: int = 1
    constituent_work_id: str
    constituent_work_cluster_id: Optional[str] = None
    constituent_expression_cluster_id: Optional[str] = None
    title: str
    creator: Optional[str] = None
    field_tag: Optional[str] = None
    final_work_source: str = ""
    final_work_source_id: str = ""
    final_work_uri: Optional[str] = None
    confidence: float = 1.0


class CorpusWorkRelationship(BaseModel):
    """Whole-part graph relationship between discovered work clusters in reconcile_corpus.db."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    id: Optional[int] = None
    source_cluster_id: str
    target_cluster_id: str
    relationship_type: str = "has_part"
    sequence_number: Optional[int] = None
    confidence: float = 1.0


class SammelbandBinding(BaseModel):
    """Post-publication item-level composite binding."""

    binding_id: str
    record_id: str
    sequence_in_binding: int = 1
    binding_notes: Optional[str] = None


class ClusterEdge(BaseModel):
    """Weighted edge in multiplex co-reference graph."""

    source_node: str
    target_node: str
    weight: float
    evidence_type: str


class MultiplexClusterResult(BaseModel):
    """Summary of multiplex clustering execution."""

    work_clusters_count: int = 0
    expression_clusters_count: int = 0
    manifestation_clusters_count: int = 0
    records_clustered: int = 0
    elapsed_seconds: float = 0.0
