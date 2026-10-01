"""Report row data models and TSV column definitions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class AgentReportRow:
    target_record_id: str
    target_title: str
    target_year: Optional[int]
    target_isbn: Optional[str]
    field_tag: str
    field_occurrence: int
    subfield_code: str
    target_heading: str
    target_role: Optional[str]
    uri: str
    vocabulary: str
    authority_label: Optional[str]
    authority_record_id: str
    authority_source: str
    match_tier: str
    match_score: float
    dense_score: Optional[float]
    rerank_score: Optional[float]
    agent_name_score: Optional[float]
    action: str  # "injected", "proposed", "existing", "suppressed_by_priority", "filtered_vocab", "below_threshold"
    is_novel: bool
    qualifier: Optional[str] = None
    dates: Optional[str] = None
    fuller_name: Optional[str] = None
    affiliation: Optional[str] = None
    birth_year: Optional[int] = None
    death_year: Optional[int] = None


@dataclass
class SubjectReportRow:
    target_record_id: str
    target_title: str
    source_field_tag: str
    field_occurrence: int
    source_heading: str
    source_vocab: Optional[str]
    source_uri: Optional[str]
    language: str
    target_field_tag: str
    target_vocab: Optional[str]
    target_uri: Optional[str]
    target_pref_label: Optional[str]
    mapping_type: str
    action: str  # "injected", "proposed", "existing", "unmapped", "ambiguous"
    is_novel: bool
    tag_changed: bool


@dataclass
class WorkReportRow:
    target_record_id: str
    work_type: str
    target_title: str
    target_author: Optional[str]
    target_year: Optional[int]
    work_blocking_key: Optional[str]
    matched_work_uri: Optional[str]
    wikidata_qid: Optional[str]
    match_tier: str
    funnel_used: str
    confidence_score: float
    is_clustered: bool
    cluster_id: Optional[str]
    action: str  # "injected", "hoisted", "minted", "unmatched"
    record_level_work_uri: Optional[str] = None


@dataclass
class ExpressionReportRow:
    target_record_id: str
    work_uri: Optional[str]
    expression_uri: Optional[str]
    content_type: str
    language: str
    version_designation: Optional[str]
    translators: Optional[str]
    performers: Optional[str]
    match_tier: str
    confidence_score: float
    cluster_id: Optional[str]
    action: str  # "injected", "aligned", "synthesized", "minted"


AGENT_TSV_COLUMNS = [
    "target_record_id",
    "target_title",
    "target_year",
    "target_isbn",
    "field_tag",
    "field_occurrence",
    "subfield_code",
    "target_heading",
    "target_role",
    "uri",
    "vocabulary",
    "authority_label",
    "authority_record_id",
    "authority_source",
    "match_tier",
    "match_score",
    "dense_score",
    "rerank_score",
    "agent_name_score",
    "action",
    "is_novel",
]

SUBJECT_TSV_COLUMNS = [
    "target_record_id",
    "target_title",
    "source_field_tag",
    "field_occurrence",
    "source_heading",
    "source_vocab",
    "source_uri",
    "language",
    "target_field_tag",
    "target_vocab",
    "target_uri",
    "target_pref_label",
    "mapping_type",
    "action",
    "is_novel",
    "tag_changed",
]

WORK_TSV_COLUMNS = [
    "target_record_id",
    "work_type",
    "target_title",
    "target_author",
    "target_year",
    "work_blocking_key",
    "matched_work_uri",
    "wikidata_qid",
    "match_tier",
    "funnel_used",
    "confidence_score",
    "is_clustered",
    "cluster_id",
    "action",
    "record_level_work_uri",
]

EXPRESSION_TSV_COLUMNS = [
    "target_record_id",
    "work_uri",
    "expression_uri",
    "content_type",
    "language",
    "version_designation",
    "translators",
    "performers",
    "match_tier",
    "confidence_score",
    "cluster_id",
    "action",
]

SUMMARY_COLUMNS = ["section", "metric", "value", "percentage", "description"]
WEMI_SUMMARY_COLUMNS = ["section", "metric", "value", "percentage", "description"]


@dataclass
class QualityBracket:
    """Represents a score interval for quality reporting."""

    metric: str
    low: Optional[float]
    high: float
    is_top: bool
    description: str

    def matches(self, score: float) -> bool:
        if self.is_top:
            return score >= (self.low if self.low is not None else 0.0)
        if self.low is None:
            return score < self.high
        return self.low <= score < self.high
