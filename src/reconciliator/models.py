"""Internal unified data models and schemas for Reconciliator."""

from __future__ import annotations

import json
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

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
    uri_subfield_0: list[str] = Field(default_factory=list)  # KANTO / ISNI / VIAF
    uri_subfield_1: list[str] = Field(default_factory=list)  # Wikidata QID
    variant_names: list[str] = Field(default_factory=list)  # Pseudonyms / alternative names
    qualifiers: list[str] = Field(default_factory=list)
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
            # Assert birth_year <= death_year
            if birth_year is not None and death_year is not None:
                if birth_year > death_year:
                    raise ValueError(
                        f"birth_year ({birth_year}) must be <= death_year ({death_year})"
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
                data["legacy_id"] = sid
            raw_t = data.get("target_uri") or data.get("target_curie")
            if raw_t and not data.get("target_id"):
                src, sid = parse_curie(str(raw_t), default_source=data.get("target_vocab", ""))
                if not data.get("target_vocab") and src and src != "unknown":
                    data["target_vocab"] = src
                data["target_id"] = sid
        return data

    @property
    def legacy_curie(self) -> str:
        return to_curie(self.legacy_vocab, self.legacy_id)

    @property
    def legacy_uri(self) -> str:
        if self.legacy_id and self.legacy_id.startswith(("http://", "https://", "urn:")):
            return self.legacy_id
        from reconciliator.vocab import concept_id_to_uri

        u = concept_id_to_uri(self.legacy_curie)
        return u if u else self.legacy_curie

    @property
    def target_curie(self) -> str:
        return to_curie(self.target_vocab, self.target_id)

    @property
    def target_uri(self) -> str:
        if self.target_id and self.target_id.startswith(("http://", "https://", "urn:")):
            return self.target_id
        from reconciliator.vocab import concept_id_to_uri

        u = concept_id_to_uri(self.target_curie)
        return u if u else self.target_curie


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


TaskType = Literal["oai_pmh_harvest", "wikidata_ingest", "vocab_ingest", "reconciliation", "system"]
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
    error_count: int = 0
    metadata_json: Optional[str] = None



# ---------------------------------------------------------------------------
# WEMI Controlled Vocabularies & Enumerations (§2.2.4)
# ---------------------------------------------------------------------------

WorkType = Literal[
    "text",
    "thesis",
    "manuscript",
    "serial",
    "integrating",
    "music",
    "moving_image",
    "cartography",
    "audio_nonmusic",
    "comic",
    "still_image",
    "dataset",
    "interactive_game",
    "board_game",
    "standard",
    "legal",
    "patent",
    "archive_fonds",
    "event",
    "realia",
    "early_print",
    "tactile",
    "software",
    "ai_model",
    "drama",
    "choreography",
    "podcast",
    "web_harvest",
    "oral_history",
    "broadcast_program",
    "codex_fragment",
    "ephemera",
    "exhibition",
    "critical_edition",
    "geodata_service",
    "numismatic",
    "heritage_model",
    "multimedia",
    "hub",
    "anthology",
    "conference",
    "government",
    "preservation_surrogate",
]

ExpressionContentType = Literal[
    "text",
    "notated_music",
    "audio_music",
    "spoken_word",
    "sounds",
    "moving_image",
    "three_dimensional_moving_image",
    "still_image",
    "cartographic_image",
    "cartographic_dataset",
    "tactile_cartographic",
    "dataset",
    "computer_program",
    "three_dimensional",
    "tactile_text",
    "tactile_graphic",
    "tactile_notated_music",
    "software_code",
    "model_weights",
    "choreographic_movement",
    "web_archive_snapshot",
    "interview_audio",
    "broadcast_stream",
    "manuscript_fragment",
    "spatial_vector",
    "spatial_raster",
    "numismatic_object",
    "three_dimensional_model",
]

CarrierType = Literal[
    "volume",
    "sheet",
    "audio_disc",
    "audiocassette",
    "videodisc",
    "videocassette",
    "computer_disc",
    "online_resource",
    "memory_card",
    "microfilm_reel",
    "microfiche",
    "braille_volume",
    "object",
]

ComponentType = Literal[
    "track",
    "article",
    "chapter",
    "paper",
    "sheet",
    "part",
    "fonds",
    "series",
    "file",
    "item_part",
    "iteration",
    "episode",
    "braille_volume",
    "code_release",
    "crawl_job",
    "broadcast_segment",
    "interview_take",
    "fragment_leaf",
    "exhibited_item",
    "critical_apparatus",
    "geodata_layer",
    "stamp_plate",
    "coin_die",
    "mesh_component",
]

WorkRelationshipType = Literal[
    "continues",
    "continued_by",
    "supersedes",
    "superseded_by",
    "series_member_of",
    "has_series_member",
    "amends",
    "amended_by",
    "draft_of",
    "published_version_of",
    "patent_family_member",
    "platform_variant_of",
    "expansion_of",
    "has_expansion",
    "adaptation_of",
    "translation_of",
    "accessible_reproduction_of",
    "has_accessible_reproduction",
    "fine_tuned_from",
    "implements_algorithm",
    "staging_of",
    "choreography_for",
    "bound_with",
    "harvest_of",
    "reproduction_of",
    "has_reproduction",
    "parallel_expression_in",
    "broadcast_of",
    "fragment_of_codex",
    "exhibits_work",
    "critical_edition_of",
    "spatial_layer_of",
    "digital_twin_of",
    # Cross-source identity: two work rows describing the same intellectual
    # creation, typically one per contributing source. Symmetric.
    "same_work_as",
    # Part/whole within one source: an album and its tracks, an anthology and its
    # stories, a journal issue and its articles. Distinct from
    # series_member_of/has_series_member, which is serial hierarchy, and from
    # bound_with, which is item-level. See plans/aggregates.md.
    "has_constituent",
    "constituent_of",
]

# The ingest source a row came from, e.g. "melinda". Whether that source is
# authoritative is a property of the source (see the `sources` table), not of
# the row, so a corpus being reconciled is simply a non-authoritative source.
RecordOrigin = str

EntityType = Literal["work", "expression", "manifestation", "item", "agent"]

ClusterMethod = Literal["multiplex_graph", "connected_components", "dbscan", "hierarchical"]


# ---------------------------------------------------------------------------
# WEMI Domain Entities (§4.1 & §4.2)
# ---------------------------------------------------------------------------


class AgentEntity(BaseModel):
    """Normalized Agent (creator, contributor, realizer, publisher) in authority.db."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    id: Optional[int] = None
    source: str
    source_id: str
    record_origin: RecordOrigin = "unknown"
    name: str
    name_normalized: str
    surname: Optional[str] = None
    dates: Optional[str] = None
    birth_year: Optional[int] = None
    death_year: Optional[int] = None
    fuller_name: Optional[str] = None
    is_personal: bool = True
    is_fictional: bool = False
    variant_names: list[str] = Field(default_factory=list)
    qualifiers: list[str] = Field(default_factory=list)
    identifiers: list[IdentifierEntity] = Field(default_factory=list)


    @model_validator(mode="before")
    @classmethod
    def _sync_agent_dates(cls, data: Any) -> Any:
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
            # Assert birth_year <= death_year
            if birth_year is not None and death_year is not None:
                if birth_year > death_year:
                    raise ValueError(
                        f"birth_year ({birth_year}) must be <= death_year ({death_year})"
                    )
        return data

    @model_validator(mode="before")
    @classmethod
    def _sync_agent_fields(cls, data: Any) -> Any:
        if isinstance(data, dict):
            uri = data.get("agent_uri")
            if uri:
                parsed_src, parsed_id = parse_curie(str(uri), default_source=data.get("source", ""))
                if parsed_src and parsed_src != "unknown":
                    data["source"] = parsed_src
                if parsed_id:
                    data["source_id"] = parsed_id
        return data

    @property
    def curie(self) -> str:
        return to_curie(self.source, self.source_id)

    @property
    def agent_uri(self) -> str:
        return self.curie


class AgentRelationshipEntity(BaseModel):
    """Relationship between agents (e.g. affiliation, pseudonym, corporate hierarchy)."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    id: Optional[int] = None
    source_agent_id: int
    target_agent_id: Optional[int] = None
    target_name: Optional[str] = None
    target_uri: Optional[str] = None
    relationship_type: str  # 'affiliation', 'pseudonym_of', 'real_identity_of'


class WorkEntity(BaseModel):

    """Abstract intellectual creation (Work tier) in authority.db."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    id: Optional[int] = None
    source: str
    source_id: str
    record_origin: RecordOrigin = "unknown"
    title: str
    title_normalized: str
    original_title: Optional[str] = None
    original_language: Optional[str] = None
    primary_agent_id: Optional[int] = None
    classification: Optional[str] = None
    classification_scheme: Optional[str] = None
    work_type: WorkType = "text"
    serialized_text: Optional[str] = None
    created_at: Optional[str] = None
    wikidata_qid: Optional[str] = None

    @model_validator(mode="before")
    @classmethod
    def _sync_work_fields(cls, data: Any) -> Any:
        if isinstance(data, dict):
            uri = data.get("work_uri")
            if uri and not data.get("source_id"):
                parsed_src, parsed_id = parse_curie(str(uri), default_source=data.get("source", ""))
                if parsed_src and not data.get("source"):
                    data["source"] = parsed_src
                data["source_id"] = parsed_id or str(uri)
            if not data.get("wikidata_qid"):
                src = (data.get("source") or "").lower()
                if src == "wikidata" and data.get("source_id"):
                    data["wikidata_qid"] = data["source_id"]
        return data

    @property
    def curie(self) -> str:
        return to_curie(self.source, self.source_id)

    @property
    def work_uri(self) -> str:
        return self.curie


class ExpressionEntity(BaseModel):
    """Specific intellectual realization (Expression tier) in authority.db."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    id: Optional[int] = None
    work_id: int
    work_curie: Optional[str] = None
    source: str
    source_id: str
    record_origin: RecordOrigin = "unknown"
    language: str
    content_type: ExpressionContentType = "text"
    title: Optional[str] = None
    version_designation: Optional[str] = None
    arrangement_note: Optional[str] = None
    medium_of_performance: Optional[str] = None
    serialized_text: Optional[str] = None
    created_at: Optional[str] = None

    @model_validator(mode="before")
    @classmethod
    def _sync_expression_fields(cls, data: Any) -> Any:
        if isinstance(data, dict):
            uri = data.get("expression_uri")
            if uri and not data.get("source_id"):
                parsed_src, parsed_id = parse_curie(str(uri), default_source=data.get("source", ""))
                if parsed_src and not data.get("source"):
                    data["source"] = parsed_src
                data["source_id"] = parsed_id or str(uri)
            if "work_uri" in data and not data.get("work_curie"):
                data["work_curie"] = data.get("work_uri")
        return data

    @property
    def curie(self) -> str:
        return to_curie(self.source, self.source_id)

    @property
    def expression_uri(self) -> str:
        return self.curie

    @property
    def work_uri(self) -> Optional[str]:
        return self.work_curie


class ManifestationEntity(BaseModel):
    """Physical or digital publication / carrier (Manifestation tier) in authority.db."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    id: Optional[int] = None
    work_id: Optional[int] = None
    work_curie: Optional[str] = None
    container_id: Optional[int] = None
    container_curie: Optional[str] = None
    is_component: bool = False
    component_type: Optional[ComponentType] = None
    source: str
    source_id: str
    record_origin: RecordOrigin = "unknown"
    title: str
    title_normalized: str
    subtitle: Optional[str] = None
    publication_year: Optional[int] = None
    publisher: Optional[str] = None
    publication_place: Optional[str] = None
    edition_statement: Optional[str] = None
    edition_number: Optional[int] = None
    series_title: Optional[str] = None
    series_numbering: Optional[str] = None
    volume_designation: Optional[str] = None
    issue_enumeration: Optional[str] = None
    pagination: Optional[str] = None
    extent: Optional[str] = None
    carrier_type: CarrierType = "volume"
    host_title: Optional[str] = None
    serialized_text: Optional[str] = None
    created_at: Optional[str] = None

    @model_validator(mode="before")
    @classmethod
    def _sync_manifestation_fields(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if "work_uri" in data and not data.get("work_curie"):
                data["work_curie"] = data.get("work_uri")
            if "container_uri" in data and not data.get("container_curie"):
                data["container_curie"] = data.get("container_uri")
            uri = data.get("manifestation_uri")
            if uri and not data.get("source_id"):
                parsed_src, parsed_id = parse_curie(str(uri), default_source=data.get("source", ""))
                if parsed_src and not data.get("source"):
                    data["source"] = parsed_src
                data["source_id"] = parsed_id or str(uri)
        return data

    @property
    def curie(self) -> str:
        return to_curie(self.source, self.source_id)

    @property
    def manifestation_uri(self) -> str:
        return self.curie

    @property
    def work_uri(self) -> Optional[str]:
        return self.work_curie

    @property
    def container_uri(self) -> Optional[str]:
        return self.container_curie

    @container_uri.setter
    def container_uri(self, val: Optional[str]) -> None:
        self.container_curie = val


class ManifestationExpressionAssociation(BaseModel):
    """Junction mapping between Manifestations and Expressions."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    manifestation_id: int
    expression_id: int
    manifestation_curie: Optional[str] = None
    expression_curie: Optional[str] = None
    track_number: Optional[int] = None
    disc_number: int = 1
    duration: Optional[str] = None
    is_primary: bool = True

    @model_validator(mode="before")
    @classmethod
    def _sync_assoc_fields(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if "manifestation_uri" in data and not data.get("manifestation_curie"):
                data["manifestation_curie"] = data.get("manifestation_uri")
            if "expression_uri" in data and not data.get("expression_curie"):
                data["expression_curie"] = data.get("expression_uri")
        return data

    @property
    def manifestation_uri(self) -> Optional[str]:
        return self.manifestation_curie

    @property
    def expression_uri(self) -> Optional[str]:
        return self.expression_curie


class ItemEntity(BaseModel):
    """Physical exemplar or concrete digital file holding in authority.db."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    id: Optional[int] = None
    manifestation_id: int
    source: str
    source_id: str
    record_origin: RecordOrigin = "unknown"
    holding_organization: str
    shelf_mark: Optional[str] = None
    barcode: Optional[str] = None
    custodial_history: Optional[str] = None
    item_condition: Optional[str] = None
    created_at: Optional[str] = None

    @model_validator(mode="before")
    @classmethod
    def _sync_item_fields(cls, data: Any) -> Any:
        if isinstance(data, dict):
            uri = data.get("item_uri")
            if uri and not data.get("source_id"):
                parsed_src, parsed_id = parse_curie(str(uri), default_source=data.get("source", ""))
                if parsed_src and not data.get("source"):
                    data["source"] = parsed_src
                data["source_id"] = parsed_id or str(uri)
        return data

    @property
    def curie(self) -> str:
        return to_curie(self.source, self.source_id)

    @property
    def item_uri(self) -> str:
        return self.curie


class IdentifierEntity(BaseModel):
    """Polymorphic identifier across Work, Expression, Manifestation, Item, or Agent."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    id: Optional[int] = None
    entity_type: EntityType
    entity_id: int = 0
    source: str = ""
    source_id: str = ""
    source_id_normalized: str = ""
    # Transient binding hint: the owning entity's CURIE, used by insert_wemi_batch to
    # resolve entity_id when the decomposer cannot know the assigned row id yet.
    # Never persisted to the identifiers table.
    entity_curie: Optional[str] = None

    @model_validator(mode="before")
    @classmethod
    def _sync_identifier_fields(cls, data: Any) -> Any:
        if isinstance(data, dict):
            src = str(data.get("source") or data.get("scheme") or "").strip().lower()
            if src == "wikidata_qid":
                src = "wikidata"
            val = str(data.get("source_id") or data.get("value") or "").strip()
            if (not src or not val) and data.get("curie"):
                s, sid = parse_curie(str(data["curie"]))
                src = src or s
                val = val or sid
            # If value looks like a URI or CURIE with a recognized prefix
            if val and ("://" in val or (":" in val and not val.startswith("urn:"))):
                parsed_src, parsed_val = parse_curie(val, default_source=src)
                if parsed_src and parsed_src != "unknown":
                    src = parsed_src
                    val = parsed_val
            norm = str(
                data.get("source_id_normalized")
                or data.get("value_normalized")
                or val.strip().lower()
            )
            ec = data.get("entity_curie") or data.get("entity_uri")
            data["source"] = src
            data["source_id"] = val
            data["source_id_normalized"] = norm
            data["entity_curie"] = ec
        return data

    @property
    def curie(self) -> str:
        return to_curie(self.source, self.source_id)

    @property
    def scheme(self) -> str:
        return self.source

    @property
    def value(self) -> str:
        return self.source_id

    @property
    def value_normalized(self) -> str:
        return self.source_id_normalized

    @property
    def entity_uri(self) -> Optional[str]:
        return self.entity_curie

    @entity_uri.setter
    def entity_uri(self, val: Optional[str]) -> None:
        self.entity_curie = val


class EntityKey(BaseModel):
    """Domain blocking key attached to a WEMI entity."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    id: Optional[int] = None
    entity_type: EntityType
    entity_id: int
    key_type: str
    key_value: str
    entity_curie: Optional[str] = None

    @model_validator(mode="before")
    @classmethod
    def _sync_entity_key(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if "entity_uri" in data and not data.get("entity_curie"):
                data["entity_curie"] = data.get("entity_uri")
        return data

    @property
    def entity_uri(self) -> Optional[str]:
        return self.entity_curie

    @entity_uri.setter
    def entity_uri(self, val: Optional[str]) -> None:
        self.entity_curie = val


class ContributionEntity(BaseModel):
    """Agent contribution with relator role across WEMI or Item tier."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    id: Optional[int] = None
    entity_type: Literal["work", "expression", "manifestation", "item"]
    entity_id: int
    agent_id: int
    role: str
    is_primary: bool = False
    field_tag: Optional[str] = None
    entity_curie: Optional[str] = None
    agent_curie: Optional[str] = None

    @model_validator(mode="before")
    @classmethod
    def _sync_contribution(cls, data: Any) -> Any:
        if isinstance(data, dict):
            ec = data.get("entity_curie") or data.get("entity_uri")
            if ec:
                s, sid = parse_curie(str(ec))
                data["entity_curie"] = to_curie(s, sid) if s and s != "unknown" else str(ec)
            ac = data.get("agent_curie") or data.get("agent_uri")
            if ac:
                s, sid = parse_curie(str(ac))
                data["agent_curie"] = to_curie(s, sid) if s and s != "unknown" else str(ac)
        return data

    @property
    def entity_uri(self) -> Optional[str]:
        return self.entity_curie

    @entity_uri.setter
    def entity_uri(self, val: Optional[str]) -> None:
        self.entity_curie = val

    @property
    def agent_uri(self) -> Optional[str]:
        return self.agent_curie

    @agent_uri.setter
    def agent_uri(self, val: Optional[str]) -> None:
        self.agent_curie = val


class WorkRelationship(BaseModel):
    """Inter-work relationship or historical lineage graph edge."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    id: Optional[int] = None
    source_work_id: int
    target_work_id: int
    source_work_curie: Optional[str] = None
    target_work_curie: Optional[str] = None
    relationship_type: WorkRelationshipType
    created_at: Optional[str] = None

    @model_validator(mode="before")
    @classmethod
    def _sync_work_rel(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if "source_work_uri" in data and not data.get("source_work_curie"):
                data["source_work_curie"] = data.get("source_work_uri")
            if "target_work_uri" in data and not data.get("target_work_curie"):
                data["target_work_curie"] = data.get("target_work_uri")
        return data

    @property
    def source_work_uri(self) -> Optional[str]:
        return self.source_work_curie

    @property
    def target_work_uri(self) -> Optional[str]:
        return self.target_work_curie


class WorkConsolidation(BaseModel):
    """Record of an elected canonical work for a consolidated work row."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    work_id: int
    canonical_work_id: int
    evidence: Optional[str] = None
    created_at: Optional[str] = None



class HarvestState(BaseModel):
    """Persistent harvest checkpoint for date-sliced OAI-PMH / ResourceSync."""

    source_id: str
    source_type: str
    metadata_prefix: str
    set_spec: Optional[str] = None
    last_harvest_from: Optional[str] = None
    last_harvest_until: Optional[str] = None
    resumption_token: Optional[str] = None
    records_harvested: int = 0
    status: str = "in_progress"
    # Date slices already fully written, for resuming a partitioned harvest.
    # A single-stream harvest leaves this empty and resumes via resumption_token.
    completed_slices: list[tuple[Optional[str], Optional[str]]] = Field(default_factory=list)
    updated_at: Optional[str] = None

    @field_validator("completed_slices", mode="before")
    @classmethod
    def parse_completed_slices(cls, v: Any) -> list[tuple[Optional[str], Optional[str]]]:
        if isinstance(v, str):
            try:
                parsed = json.loads(v)
            except Exception:
                return []
            v = parsed if isinstance(parsed, list) else []
        if not isinstance(v, list):
            return []
        slices: list[tuple[Optional[str], Optional[str]]] = []
        for item in v:
            if isinstance(item, (list, tuple)) and len(item) == 2:
                slices.append((item[0], item[1]))
        return slices


class MatchRecord(BaseModel):
    """Record-level view over WEMI rows, as the matcher and clusterer consume it.

    Manifestation-centric: one ingested record is one manifestation. Resolves the
    Expression and Work above it so the funnels can match at all three tiers.
    """

    model_config = ConfigDict(extra="ignore")

    record_id: str
    format: str
    work_type: WorkType = "text"
    title: str
    uniform_title: Optional[str] = None
    language: Optional[str] = None
    author_key: Optional[str] = None
    title_stem: str
    year: Optional[int] = None
    volume_designation: Optional[str] = None
    is_component: bool = False
    keys: dict[str, list[str]] = Field(default_factory=dict)
    identifiers: dict[str, list[str]] = Field(default_factory=dict)
    content_type: Optional[str] = None
    work_curie: Optional[str] = None
    expression_curie: Optional[str] = None
    manifestation_curie: Optional[str] = None
    serialized_work: Optional[str] = None
    serialized_expression: Optional[str] = None
    serialized_manifestation: Optional[str] = None
    raw_payload: bytes = b""

    @model_validator(mode="before")
    @classmethod
    def _normalize_keys(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        if "work_uri" in data and not data.get("work_curie"):
            data["work_curie"] = data.get("work_uri")
        if "expression_uri" in data and not data.get("expression_curie"):
            data["expression_curie"] = data.get("expression_uri")
        if "manifestation_uri" in data and not data.get("manifestation_curie"):
            data["manifestation_curie"] = data.get("manifestation_uri")
        keys = {
            k: list(v) if isinstance(v, list) else [str(v)]
            for k, v in (data.get("keys") or {}).items()
        }
        # Map known individual key attributes if passed as kwargs/dict keys
        known_key_fields = {
            "work_blocking_key": "work",
            "music_work_key": "music_work",
            "series_key": "series",
            "standard_key": "standard",
            "patent_key": "patent",
            "statute_key": "statute",
            "game_key": "game",
            "board_game_key": "board_game",
            "archival_fonds_key": "archival_fonds",
            "fingerprint_key": "fingerprint",
            "swhid_key": "swhid",
            "podcast_key": "podcast",
            "sammelband_item_id": "sammelband",
            "web_seed_key": "web_seed",
            "broadcast_timecode_key": "broadcast",
            "oral_history_key": "oral_history",
            "fragment_codex_key": "fragment_codex",
            "ephemera_corp_key": "ephemera_corp",
            "exhibition_event_key": "exhibition_event",
            "critical_edition_key": "critical_edition",
            "geodata_spatial_key": "geodata_spatial",
            "numismatic_issue_key": "numismatic_issue",
            "heritage_model_key": "heritage_model",
            "host_citation_key": "citation",
            "translators_signature": "translators_signature",
        }
        for field, key_type in known_key_fields.items():
            val = data.get(field)
            if val and isinstance(val, str) and val.strip():
                if key_type not in keys:
                    keys[key_type] = []
                clean = val.strip()
                if clean not in keys[key_type]:
                    keys[key_type].append(clean)
        data["keys"] = keys
        return data

    @property
    def work_uri(self) -> Optional[str]:
        return self.work_curie

    @property
    def expression_uri(self) -> Optional[str]:
        return self.expression_curie

    @property
    def manifestation_uri(self) -> Optional[str]:
        return self.manifestation_curie

    def get_first_key(self, key_type: str) -> Optional[str]:
        """Get the first value for key_type, or None."""
        vals = self.keys.get(key_type)
        return vals[0] if vals else None

    def get_keys(self, key_type: str) -> list[str]:
        """Get all values for key_type."""
        return self.keys.get(key_type, [])

    def get_identifiers(self, scheme: str) -> list[str]:
        """Get all identifier values for a scheme."""
        return self.identifiers.get(scheme.lower().strip(), [])

    # Accessors for common key types
    @property
    def work_blocking_key(self) -> str:
        return self.get_first_key("work") or self.get_first_key("work_blocking") or ""


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
    members: list[AgentClusterMember] = Field(default_factory=list)
    attributes: list[AgentClusterAttribute] = Field(default_factory=list)


class AgentClusterResult(BaseModel):
    """Summary result of an agent clustering run."""

    total_agents: int = 0
    total_clusters: int = 0
    singletons: int = 0
    multi_member_clusters: int = 0
    max_cluster_size: int = 0
    link_method_counts: dict[str, int] = Field(default_factory=dict)


class EntitySubject(BaseModel):

    """Polymorphic link between an entity (work, expression, manifestation, agent) and a subject concept."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    id: Optional[int] = None
    entity_type: Literal["work", "expression", "manifestation", "agent"]
    entity_id: int = 0
    source: str = "unknown"
    source_record_id: Optional[str] = None
    relation_type: str = "topic"  # topic, genre_form, geographic, temporal, occupation, field_of_activity, associated_place, medium_of_performance
    vocabulary: Optional[str] = None
    concept_id: Optional[str] = None
    raw_label: Optional[str] = None
    is_resolved: bool = False
    is_primary: bool = False
    entity_curie: Optional[str] = None

    @model_validator(mode="before")
    @classmethod
    def _sync_entity_subject(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if "entity_uri" in data and not data.get("entity_curie"):
                data["entity_curie"] = data.get("entity_uri")
        return data

    @property
    def entity_uri(self) -> Optional[str]:
        return self.entity_curie

    @entity_uri.setter
    def entity_uri(self, val: Optional[str]) -> None:
        self.entity_curie = val
