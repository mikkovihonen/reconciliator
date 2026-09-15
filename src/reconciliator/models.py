"""Internal unified data models and schemas for Reconciliator."""

from __future__ import annotations

import json
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


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
    orcid: Optional[str] = None  # Persistent identifier (e.g. "https://orcid.org/...")
    variant_names: list[str] = Field(default_factory=list)  # Pseudonyms / alternative names
    metadata_json: Optional[str] = None

    # Pure empirical frequency count distributions
    qualifier_counts: dict[str, int] = Field(default_factory=dict)
    affiliation_counts: dict[str, int] = Field(default_factory=dict)
    role_counts: dict[str, int] = Field(default_factory=dict)
    language_counts: dict[str, int] = Field(default_factory=dict)
    subject_counts: dict[str, int] = Field(default_factory=dict)


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

    uri: str
    vocabulary: str  # e.g. "yso", "kauno", "muso", "slm", "seko", "agrovoc"
    pref_labels: dict[str, str] = Field(default_factory=dict)
    alt_labels: dict[str, list[str]] = Field(default_factory=dict)
    is_deprecated: bool = False
    target_field: Optional[str] = None  # e.g. "650", "655", "651", "382"

    # Typed legacy field parameters for backwards compatibility and static checkers
    pref_label_fi: Optional[str] = Field(default=None, repr=False)
    pref_label_sv: Optional[str] = Field(default=None, repr=False)
    pref_label_en: Optional[str] = Field(default=None, repr=False)
    alt_labels_fi: list[str] = Field(default_factory=list, repr=False)
    alt_labels_sv: list[str] = Field(default_factory=list, repr=False)

    @model_validator(mode="after")
    def _sync_labels(self) -> VocabConcept:
        for lang in ("fi", "sv", "en"):
            attr_name = f"pref_label_{lang}"
            val = getattr(self, attr_name, None)
            if val is not None and lang not in self.pref_labels:
                self.pref_labels[lang] = val
            elif lang in self.pref_labels:
                object.__setattr__(self, attr_name, self.pref_labels[lang])

        for lang in ("fi", "sv"):
            attr_name = f"alt_labels_{lang}"
            alts = getattr(self, attr_name, None)
            if alts and lang not in self.alt_labels:
                self.alt_labels[lang] = list(alts)
            elif lang in self.alt_labels:
                object.__setattr__(self, attr_name, self.alt_labels[lang])

        return self


class ConceptMapping(BaseModel):
    """Mapping relation between legacy thesauri terms/URIs and modern ontologies."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    legacy_uri: Optional[str] = None
    legacy_vocab: str  # e.g. "ysa", "allars", "kaunokki", "bella", "musa", "cilla"
    legacy_labels: dict[str, str] = Field(default_factory=dict)
    target_uri: str
    target_vocab: str  # e.g. "yso", "kauno", "muso", "slm", "seko"
    mapping_type: str  # e.g. "exact_match", "is_replaced_by"
    target_field: str  # e.g. "650", "655", "651", "382"

    # Typed legacy field parameters for backwards compatibility and static checkers
    legacy_label_fi: Optional[str] = Field(default=None, repr=False)
    legacy_label_sv: Optional[str] = Field(default=None, repr=False)
    legacy_label_en: Optional[str] = Field(default=None, repr=False)

    @model_validator(mode="after")
    def _sync_labels(self) -> ConceptMapping:
        for lang in ("fi", "sv", "en"):
            attr_name = f"legacy_label_{lang}"
            val = getattr(self, attr_name, None)
            if val is not None and lang not in self.legacy_labels:
                self.legacy_labels[lang] = val
            elif lang in self.legacy_labels:
                object.__setattr__(self, attr_name, self.legacy_labels[lang])
        return self


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


TaskType = Literal[
    "oai_pmh_harvest", "wikidata_ingest", "vocab_ingest", "reconciliation", "system"
]
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

    id: Optional[int] = None
    agent_uri: str
    source: str
    record_origin: RecordOrigin = "unknown"
    wikidata_qid: Optional[str] = None
    name: str
    name_normalized: str
    surname: Optional[str] = None
    dates: Optional[str] = None
    birth_year: Optional[int] = None
    death_year: Optional[int] = None
    fuller_name: Optional[str] = None
    is_personal: bool = True
    is_fictional: bool = False
    orcid: Optional[str] = None
    variant_names: list[str] = Field(default_factory=list)
    metadata_json: Optional[str] = None

    # Pure empirical frequency count distributions
    qualifier_counts: dict[str, int] = Field(default_factory=dict)
    affiliation_counts: dict[str, int] = Field(default_factory=dict)
    role_counts: dict[str, int] = Field(default_factory=dict)
    language_counts: dict[str, int] = Field(default_factory=dict)
    subject_counts: dict[str, int] = Field(default_factory=dict)


class WorkEntity(BaseModel):
    """Abstract intellectual creation (Work tier) in authority.db."""

    id: Optional[int] = None
    work_uri: str
    source: str
    source_id: str
    record_origin: RecordOrigin = "unknown"
    wikidata_qid: Optional[str] = None
    title: str
    title_normalized: str
    original_title: Optional[str] = None
    original_language: Optional[str] = None
    primary_agent_id: Optional[int] = None
    ykl_class: Optional[str] = None
    work_type: WorkType = "text"
    subjects_json: list[str] = Field(default_factory=list)
    serialized_text: Optional[str] = None
    # Representative of this work's cross-source equivalence group, set by
    # WorkConsolidator. None means the work is its own canonical.
    canonical_work_id: Optional[int] = None
    created_at: Optional[str] = None

    @field_validator("subjects_json", mode="before")
    @classmethod
    def parse_subjects_json(cls, v: Any) -> list[str]:
        if isinstance(v, str):
            try:
                parsed = json.loads(v)
                if isinstance(parsed, list):
                    return [str(item) for item in parsed]
            except Exception:
                return [v] if v else []
        elif isinstance(v, list):
            return [str(item) for item in v]
        return []


class ExpressionEntity(BaseModel):
    """Specific intellectual realization (Expression tier) in authority.db."""

    id: Optional[int] = None
    work_id: int
    expression_uri: str
    source: str
    source_id: str
    record_origin: RecordOrigin = "unknown"
    wikidata_qid: Optional[str] = None
    language: str
    content_type: ExpressionContentType = "text"
    title: Optional[str] = None
    version_designation: Optional[str] = None
    arrangement_note: Optional[str] = None
    medium_of_performance: Optional[str] = None
    serialized_text: Optional[str] = None
    created_at: Optional[str] = None


class ManifestationEntity(BaseModel):
    """Physical or digital publication / carrier (Manifestation tier) in authority.db."""

    id: Optional[int] = None
    work_id: Optional[int] = None
    container_id: Optional[int] = None
    is_component: bool = False
    component_type: Optional[ComponentType] = None
    manifestation_uri: Optional[str] = None
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


class ManifestationExpressionAssociation(BaseModel):
    """Junction mapping between Manifestations and Expressions."""

    manifestation_id: int
    expression_id: int
    track_number: Optional[int] = None
    disc_number: int = 1
    duration: Optional[str] = None
    is_primary: bool = True


class ItemEntity(BaseModel):
    """Physical exemplar or concrete digital file holding in authority.db."""

    id: Optional[int] = None
    manifestation_id: int
    item_uri: Optional[str] = None
    source: str
    source_id: str
    record_origin: RecordOrigin = "unknown"
    holding_organization: str
    shelf_mark: Optional[str] = None
    barcode: Optional[str] = None
    custodial_history: Optional[str] = None
    item_condition: Optional[str] = None
    created_at: Optional[str] = None


class IdentifierEntity(BaseModel):
    """Polymorphic identifier across Work, Expression, Manifestation, Item, or Agent."""

    id: Optional[int] = None
    entity_type: EntityType
    entity_id: int
    scheme: str
    value: str
    value_normalized: str
    # Transient binding hint: the owning entity's URI, used by insert_wemi_batch to
    # resolve entity_id when the decomposer cannot know the assigned row id yet.
    # Never persisted to the identifiers table.
    entity_uri: Optional[str] = None


class EntityKey(BaseModel):
    """Domain blocking key attached to a WEMI entity.

    Sits beside `identifiers`: that table holds externally-meaningful standard
    numbers, this one holds the derived keys the clusterer blocks on and the
    funnels dispatch on (work stems, music work keys, host citations, MARC
    analytics). Both are computed once at ingest, where the record is already
    parsed, rather than by re-parsing the payload at match time.
    """

    id: Optional[int] = None
    entity_type: EntityType
    entity_id: int
    key_type: str
    key_value: str
    # Transient binding hint, mirroring IdentifierEntity.entity_uri: the owning
    # entity's URI, resolved to entity_id by insert_wemi_batch. Never persisted.
    entity_uri: Optional[str] = None


class ContributionEntity(BaseModel):
    """Agent contribution with relator role across WEMI or Item tier."""

    id: Optional[int] = None
    entity_type: Literal["work", "expression", "manifestation", "item"]
    entity_id: int
    agent_id: int
    role: str
    is_primary: bool = False
    field_tag: Optional[str] = None
    # Transient binding hints used by insert_wemi_batch to resolve entity_id /
    # agent_id when the decomposer cannot know the assigned row ids yet. Without
    # them a multi-record or multi-agent batch collapses onto the first row.
    # Never persisted to the contributions table.
    entity_uri: Optional[str] = None
    agent_uri: Optional[str] = None


class WorkRelationship(BaseModel):
    """Inter-work relationship or historical lineage graph edge."""

    id: Optional[int] = None
    source_work_id: int
    target_work_id: int
    relationship_type: WorkRelationshipType
    created_at: Optional[str] = None


class MediumOfPerformance(BaseModel):
    """Medium of performance instrumentation for musical works."""

    work_id: int
    medium_uri: str
    label: str
    player_count: Optional[int] = None
    is_soloist: bool = False


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
    # Standard identifiers, extracted once at ingest. Carried on the record rather
    # than re-derived from raw_payload at match time: the old payload parse was a
    # second full parse per match attempt, and it silently returned nothing on the
    # reconcile path, where the payload was JSON mislabelled as marcxml.
    identifiers: dict[str, list[str]] = Field(default_factory=dict)
    # Expression and Work context above the manifestation, so the funnels can
    # align at all three tiers without a repository round trip.
    content_type: Optional[str] = None
    work_uri: Optional[str] = None
    expression_uri: Optional[str] = None
    manifestation_uri: Optional[str] = None
    serialized_work: Optional[str] = None
    serialized_expression: Optional[str] = None
    serialized_manifestation: Optional[str] = None
    raw_payload: bytes = b""

    @model_validator(mode="before")
    @classmethod
    def _normalize_keys(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        keys = {k: list(v) if isinstance(v, list) else [str(v)] for k, v in (data.get("keys") or {}).items()}
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

    cluster_id: str
    work_type: WorkType = "text"
    canonical_work_uri: Optional[str] = None
    wikidata_qid: Optional[str] = None
    canonical_title: str
    canonical_author: Optional[str] = None


class ExpressionCluster(BaseModel):
    """Discovered Expression realization cluster in reconcile_corpus.db."""

    cluster_id: str
    work_cluster_id: str
    canonical_expression_uri: Optional[str] = None
    content_type: ExpressionContentType = "text"
    language: str
    version_designation: Optional[str] = None
    translators_json: list[str] = Field(default_factory=list)
    performers_json: list[str] = Field(default_factory=list)


class CorpusLinkage(BaseModel):
    """Final record-to-cluster resolution mapping in reconcile_corpus.db."""

    record_id: str
    manifestation_cluster_id: Optional[str] = None
    work_cluster_id: str
    expression_cluster_id: str
    final_work_uri: str
    final_expression_uri: str
    sibling_manifestation_ids_json: Optional[str] = None


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
