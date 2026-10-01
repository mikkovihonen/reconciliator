"""Bibliographic, authority intake, and reconciliation match data models."""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from reconciliator.models.entities import IdentifierEntity
from reconciliator.utils.curie import parse_curie, to_curie


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
    doi: list[str] = Field(default_factory=list)
    other_ids: list[str] = Field(default_factory=list)
    work_identifiers: dict[str, list[str]] = Field(default_factory=dict)


class Agent(BaseModel):
    """Normalized personal or corporate agent."""

    name: str
    surname: Optional[str] = None
    dates: Optional[str] = None  # MARC $d (e.g. "1908-1979", "1948-")
    fuller_name: Optional[str] = None  # MARC $q (e.g. "Mika Toimi")
    birth_year: Optional[int] = None
    death_year: Optional[int] = None
    field_tag: Optional[str] = None  # e.g. "100", "700", "110", "710", "111", "711"
    is_personal: bool = True
    is_fictional: bool = False
    agent_uri: Optional[str] = None
    uri_subfield_0: list[str] = Field(default_factory=list)  # KANTO / ISNI / VIAF
    uri_subfield_1: list[str] = Field(default_factory=list)  # Wikidata QID
    variant_names: list[str] = Field(default_factory=list)  # Pseudonyms / alternative names
    qualifiers: list[str] = Field(default_factory=list)
    affiliations: list[str] = Field(default_factory=list)
    relationships: list[dict[str, Any]] = Field(default_factory=list)
    identifiers: list[IdentifierEntity] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _sync_dates(cls, data: Any) -> Any:
        if isinstance(data, dict):
            dates = data.get("dates")
            birth_year = data.get("birth_year")
            death_year = data.get("death_year")
            # If dates supplied without birth/death year: parse to populate them
            if dates and (birth_year is None or death_year is None):
                from reconciliator.normalizers.agent import parse_agent_dates

                b, d = parse_agent_dates(dates)
                if birth_year is None:
                    data["birth_year"] = b
                if death_year is None:
                    data["death_year"] = d
            # If birth/death year supplied without dates: synthesize
            if not dates and (birth_year is not None or death_year is not None):
                from reconciliator.normalizers.agent import format_agent_dates

                data["dates"] = format_agent_dates(birth_year, death_year)
            # Assert birth_year <= death_year, swapping if inverted
            if birth_year is not None and death_year is not None:
                if birth_year > death_year:
                    b_orig, d_orig = birth_year, death_year
                    birth_year, death_year = d_orig, b_orig
                    data["birth_year"] = birth_year
                    data["death_year"] = death_year
                    import logging

                    from reconciliator.logging import log_sanitization

                    log_sanitization(
                        logging.getLogger("reconciliator.models.records"),
                        action="swap_inverted_dates",
                        entity="agent_dates",
                        source_id=str(data.get("source_id") or data.get("name") or "agent"),
                        reason=f"birth_year ({b_orig}) > death_year ({d_orig}); swapped values",
                        level="WARNING",
                    )
        return data


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


class SubjectsContainer(BaseModel):
    """Container for categorized subject headings."""

    topical: list[SubjectItem] = Field(default_factory=list)
    genre_form: list[SubjectItem] = Field(default_factory=list)
    geographic: list[SubjectItem] = Field(default_factory=list)
    chronological: list[SubjectItem] = Field(default_factory=list)
    medium_of_performance: list[SubjectItem] = Field(default_factory=list)

    def all_items(self) -> list[tuple[str, SubjectItem]]:
        """Return list of (default_marc_tag, SubjectItem) pairs across all categories."""
        from reconciliator.config import get_category_default_field_map

        field_map = get_category_default_field_map()
        items: list[tuple[str, SubjectItem]] = []
        for cat in self.__class__.model_fields:
            if cat in field_map:
                tag = field_map[cat]
                items.extend((tag, s) for s in getattr(self, cat))
        return items

    def all_subjects(self) -> list[SubjectItem]:
        """Return a flat list of all SubjectItem instances across all categories."""
        return (
            list(self.topical)
            + list(self.genre_form)
            + list(self.geographic)
            + list(self.chronological)
            + list(self.medium_of_performance)
        )


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
    work_identifiers: dict[str, list[str]] = Field(default_factory=dict)
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

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    vocabulary: str  # e.g. "yso", "kauno", "muso", "slm", "seko", "agrovoc"
    concept_id: str = ""
    pref_labels: dict[str, str] = Field(default_factory=dict)
    alt_labels: dict[str, list[str]] = Field(default_factory=dict)
    is_deprecated: bool = False
    target_field: Optional[str] = None  # e.g. "650", "655", "651", "382"

    @model_validator(mode="before")
    @classmethod
    def _sync_concept_before(cls, data: Any) -> Any:
        if isinstance(data, dict):
            raw_uri = data.get("uri") or data.get("concept_uri") or data.get("curie")
            if raw_uri and not data.get("concept_id"):
                src, sid = parse_curie(str(raw_uri), default_source=data.get("vocabulary", ""))
                if not data.get("vocabulary") and src and src != "unknown":
                    data["vocabulary"] = src
                if not sid or sid.startswith(("http://", "https://", "urn:")):
                    from reconciliator.utils.curie import format_source_uri
                    from reconciliator.vocab.detection import detect_vocabulary, extract_concept_id

                    voc = data.get("vocabulary", "")
                    if detect_vocabulary(str(raw_uri)) or (
                        voc and format_source_uri(voc, "test") != f"{voc}:test"
                    ):
                        extracted = extract_concept_id(str(raw_uri))
                        sid = extracted or sid
                data["concept_id"] = sid
        return data

    @property
    def curie(self) -> str:
        return to_curie(self.vocabulary, self.concept_id)

    @property
    def uri(self) -> str:
        if self.concept_id and self.concept_id.startswith(("http://", "https://", "urn:")):
            return self.concept_id
        from reconciliator.utils.curie import format_source_uri

        u = format_source_uri(self.vocabulary, self.concept_id)
        if u and not u.startswith(f"{self.vocabulary}:"):
            return u
        from reconciliator.vocab import concept_id_to_uri

        u_legacy = concept_id_to_uri(self.curie)
        return u_legacy if u_legacy else self.curie


class ConceptMapping(BaseModel):
    """Mapping relation between legacy thesauri terms/URIs and modern ontologies."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    legacy_vocab: str  # e.g. "ysa", "allars", "kaunokki", "bella", "musa", "cilla"
    legacy_id: str = ""
    target_vocab: str  # e.g. "yso", "kauno", "muso", "slm", "seko"
    target_id: str = ""
    mapping_type: str = "exact_match"  # e.g. "exact_match", "is_replaced_by"
    target_field: str = ""  # e.g. "650", "655", "651", "382"
    legacy_labels: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _sync_mapping_before(cls, data: Any) -> Any:
        if isinstance(data, dict):
            raw_l = data.get("legacy_uri") or data.get("legacy_curie")
            if raw_l and not data.get("legacy_id"):
                src, sid = parse_curie(str(raw_l), default_source=data.get("legacy_vocab", ""))
                if not data.get("legacy_vocab") and src and src != "unknown":
                    data["legacy_vocab"] = src
                if not sid or sid.startswith(("http://", "https://", "urn:")):
                    from reconciliator.utils.curie import format_source_uri
                    from reconciliator.vocab.detection import detect_vocabulary, extract_concept_id

                    l_voc = data.get("legacy_vocab", "")
                    if detect_vocabulary(str(raw_l)) or (
                        l_voc and format_source_uri(l_voc, "test") != f"{l_voc}:test"
                    ):
                        extracted = extract_concept_id(str(raw_l))
                        sid = extracted or sid
                data["legacy_id"] = sid
            raw_t = data.get("target_uri") or data.get("target_curie")
            if raw_t and not data.get("target_id"):
                src, sid = parse_curie(str(raw_t), default_source=data.get("target_vocab", ""))
                if not data.get("target_vocab") and src and src != "unknown":
                    data["target_vocab"] = src
                if not sid or sid.startswith(("http://", "https://", "urn:")):
                    from reconciliator.utils.curie import format_source_uri
                    from reconciliator.vocab.detection import detect_vocabulary, extract_concept_id

                    t_voc = data.get("target_vocab", "")
                    if detect_vocabulary(str(raw_t)) or (
                        t_voc and format_source_uri(t_voc, "test") != f"{t_voc}:test"
                    ):
                        extracted = extract_concept_id(str(raw_t))
                        sid = extracted or sid
                data["target_id"] = sid
        return data

    @property
    def legacy_curie(self) -> str:
        return to_curie(self.legacy_vocab, self.legacy_id)

    @property
    def legacy_uri(self) -> str:
        if self.legacy_id and self.legacy_id.startswith(("http://", "https://", "urn:")):
            return self.legacy_id
        from reconciliator.utils.curie import format_source_uri

        u = format_source_uri(self.legacy_vocab, self.legacy_id)
        if u and not u.startswith(f"{self.legacy_vocab}:"):
            return u
        from reconciliator.vocab import concept_id_to_uri

        u_legacy = concept_id_to_uri(self.legacy_curie)
        return u_legacy if u_legacy else self.legacy_curie

    @property
    def target_curie(self) -> str:
        return to_curie(self.target_vocab, self.target_id)

    @property
    def target_uri(self) -> str:
        if self.target_id and self.target_id.startswith(("http://", "https://", "urn:")):
            return self.target_id
        from reconciliator.utils.curie import format_source_uri

        u = format_source_uri(self.target_vocab, self.target_id)
        if u and not u.startswith(f"{self.target_vocab}:"):
            return u
        from reconciliator.vocab import concept_id_to_uri

        u_legacy = concept_id_to_uri(self.target_curie)
        return u_legacy if u_legacy else self.target_curie


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
    rationale: Optional[str] = None


TaskType = Literal["oai_pmh_harvest", "wikidata_ingest", "vocab_ingest", "reconciliation", "system"]
TaskStatus = Literal["running", "completed", "failed", "interrupted"]


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
    error_count: int = 0
    metadata_json: Optional[str] = None
