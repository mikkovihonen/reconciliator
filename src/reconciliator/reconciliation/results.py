"""Reconciliation result models and data containers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from reconciliator.models import (
    AgentURI,
    BibliographicRecord,
    CorpusLinkage,
    ExpressionEntity,
    ManifestationEntity,
    ReconciliationMatch,
    WorkEntity,
    WorkRelationship,
)


@dataclass
class ReconciliationResult:
    """Consolidated reconciliation result for a single target record."""

    target_record: BibliographicRecord
    matches: list[ReconciliationMatch] = field(default_factory=list)
    accepted_candidates: list[BibliographicRecord] = field(default_factory=list)
    discovered_agent_uris: dict[int, list[AgentURI]] = field(default_factory=dict)
    selected_agent_uris: dict[int, list[str]] = field(default_factory=dict)
    suppressed_agent_uris: dict[int, list[str]] = field(default_factory=dict)
    corpus_linkage: Optional[CorpusLinkage] = None


@dataclass
class ConstituentWorkMatch:
    """Represents a matched constituent work within an aggregate or collection."""

    title: str
    matched_work: Optional[WorkEntity] = None
    work_uri: Optional[str] = None
    expression_uri: Optional[str] = None
    relationship_type: Optional[str] = None
    confidence_score: float = 1.0


@dataclass
class WEMIMatchResult:
    """Consolidated match result for a target record resolved by WorkMatcher."""

    target_record_id: str
    matched_work: Optional[WorkEntity] = None
    matched_expression: Optional[ExpressionEntity] = None
    matched_manifestation: Optional[ManifestationEntity] = None
    work_curie: Optional[str] = None
    expression_curie: Optional[str] = None
    manifestation_curie: Optional[str] = None
    work_uri: Optional[str] = None
    expression_uri: Optional[str] = None
    manifestation_uri: Optional[str] = None
    match_tier: str = "unmatched"
    funnel_used: str = "unknown"
    confidence_score: float = 0.0
    constituent_matches: list[ConstituentWorkMatch] = field(default_factory=list)
    relationships: list[WorkRelationship] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.work_curie and self.work_uri:
            self.work_curie = self.work_uri
        elif not self.work_uri and self.work_curie:
            self.work_uri = self.work_curie

        if not self.expression_curie and self.expression_uri:
            self.expression_curie = self.expression_uri
        elif not self.expression_uri and self.expression_curie:
            self.expression_uri = self.expression_curie

        if not self.manifestation_curie and self.manifestation_uri:
            self.manifestation_curie = self.manifestation_uri
        elif not self.manifestation_uri and self.manifestation_curie:
            self.manifestation_uri = self.manifestation_curie
