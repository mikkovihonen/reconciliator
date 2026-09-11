"""Internal unified data models and schemas for Reconciliator."""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class ClassificationItem(BaseModel):
    """Model representing an individual bibliographic classification notation."""

    model_config = ConfigDict(frozen=True)

    code: str
    scheme: str  # e.g. "ykl", "udc", "ddc", "lcc"
    is_primary: bool = False


class RecordIdentifiers(BaseModel):
    """Standard identifiers associated with a bibliographic record."""

    isbn: list[str] = Field(default_factory=list)
    issn: list[str] = Field(default_factory=list)
    urn: list[str] = Field(default_factory=list)
    other_ids: list[str] = Field(default_factory=list)


class Agent(BaseModel):
    """Normalized personal or corporate agent."""

    name: str
    surname: Optional[str] = None
    role: Optional[str] = None  # e.g. "aut", "edt", "trl"
    field_tag: Optional[str] = None  # e.g. "100", "700", "110", "710", "111", "711"
    is_personal: bool = True
    is_fictional: bool = False
    uri_subfield_0: list[str] = Field(default_factory=list)  # KANTO / ISNI / VIAF
    uri_subfield_1: list[str] = Field(default_factory=list)  # Wikidata QID


class SubjectItem(BaseModel):
    """Normalized subject concept."""

    uri: str
    pref_label: str
    vocabulary: str  # e.g. "yso", "kauno", "muso", "slm", "seko"
    source_field: Optional[str] = None
    target_field: Optional[str] = None
    source_vocab: Optional[str] = None
    source_uri: Optional[str] = None
    source_heading: Optional[str] = None
    mapping_type: Optional[str] = None
    wikidata_qid: Optional[str] = None


class SubjectsContainer(BaseModel):
    """Container for categorized subject headings."""

    topical: list[SubjectItem] = Field(default_factory=list)
    genre_form: list[SubjectItem] = Field(default_factory=list)
    geographic: list[SubjectItem] = Field(default_factory=list)
    medium_of_performance: list[SubjectItem] = Field(default_factory=list)


class BibliographicRecord(BaseModel):
    """Unified internal representation of a bibliographic entity."""

    source_id: str
    source: str
    title: str
    subtitle: Optional[str] = None
    uniform_title: Optional[str] = None
    variant_titles: list[str] = Field(default_factory=list)
    publication_year: Optional[int] = None
    publisher: Optional[str] = None
    publication_place: Optional[str] = None
    language: Optional[str] = None
    original_language: Optional[str] = None
    edition: Optional[int] = None
    extent: Optional[int] = None
    series_title: Optional[str] = None
    series_numbering: Optional[str] = None
    volume_designation: Optional[str] = None
    classifications: list[ClassificationItem] = Field(default_factory=list)
    identifiers: RecordIdentifiers = Field(default_factory=RecordIdentifiers)
    agents: list[Agent] = Field(default_factory=list)
    subjects: SubjectsContainer = Field(default_factory=SubjectsContainer)
    serialization: str = ""


class AgentURI(BaseModel):
    """An authority URI linked to an agent with provenance."""

    uri: str
    vocabulary: str
    subfield_code: str  # "0" or "1"
    source_record_id: str
    source: str
    alignment_score: float = 100.0


class VocabConcept(BaseModel):
    """Modern controlled vocabulary concept."""

    uri: str
    vocabulary: str  # e.g. "yso", "kauno", "muso", "slm", "seko", "agrovoc"
    pref_label_fi: Optional[str] = None
    pref_label_sv: Optional[str] = None
    pref_label_en: Optional[str] = None
    alt_labels_fi: list[str] = Field(default_factory=list)
    alt_labels_sv: list[str] = Field(default_factory=list)
    is_deprecated: bool = False
    target_field: Optional[str] = None  # e.g. "650", "655", "651", "382"


class ConceptMapping(BaseModel):
    """Mapping relation between legacy thesauri terms/URIs and modern ontologies."""

    legacy_uri: Optional[str] = None
    legacy_vocab: str  # e.g. "ysa", "allars", "kaunokki", "bella", "musa", "cilla"
    legacy_label_fi: Optional[str] = None
    legacy_label_sv: Optional[str] = None
    target_uri: str
    target_vocab: str  # e.g. "yso", "kauno", "muso", "slm", "seko"
    mapping_type: str  # e.g. "exact_match", "is_replaced_by"
    target_field: str  # e.g. "650", "655", "651", "382"


class ReconciliationMatch(BaseModel):
    """Record match evaluation result from the multi-tier reconciliation pipeline."""

    candidate_record_id: str
    source: str
    match_tier: str  # "tier1_exact_id", "tier2_dense", "tier3_hybrid_rerank"
    match_score: float
    dense_score: Optional[float] = None
    rerank_score: Optional[float] = None
    agent_name_score: Optional[float] = None
    volume_match: bool = True
    surname_match: bool = True
    year_diff: Optional[int] = None
    accepted: bool = True


TaskType = Literal["oai_pmh_harvest", "wikidata_ingest", "vocab_ingest", "reconciliation"]
TaskStatus = Literal["running", "completed", "failed"]


class TaskProgress(BaseModel):
    """Real-time task progress metrics stored in progress.db."""

    task_id: str
    task_type: TaskType
    status: TaskStatus
    current_stage: str
    items_processed: int = 0
    items_total: Optional[int] = None
    bytes_processed: int = 0
    bytes_total: Optional[int] = None
    rate_per_sec: float = 0.0
    percentage: float = 0.0
    start_time: str = ""
    last_heartbeat: str = ""
    error_message: Optional[str] = None
    metadata_json: Optional[str] = None
