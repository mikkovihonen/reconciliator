"""WEMI and relational domain entity data models."""

from __future__ import annotations

import json
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from reconciliator.utils.curie import parse_curie, to_curie

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
    # bound_with, which is item-level. See docs/aggregates.md.
    "has_part",
    "part_of",
    "has_constituent",
    "constituent_of",
]

# The ingest source a row came from, e.g. "melinda". Whether that source is
# authoritative is a property of the source (see the `sources` table), not of
# the row, so a corpus being reconciled is simply a non-authoritative source.
RecordOrigin = str

EntityType = Literal["work", "expression", "manifestation", "item", "agent"]

ClusterMethod = Literal[
    "multiplex_graph",
    "connected_components",
    "dbscan",
    "hierarchical",
    "bigclust",
]


# ---------------------------------------------------------------------------
# WEMI Domain Entities (§4.1 & §4.2)
# ---------------------------------------------------------------------------


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
    affiliations: list[str] = Field(default_factory=list)
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
                        logging.getLogger("reconciliator.models.entities"),
                        action="swap_inverted_dates",
                        entity="agent_entity_dates",
                        source_id=str(data.get("source_id") or data.get("name") or "agent"),
                        reason=f"birth_year ({b_orig}) > death_year ({d_orig}); swapped values",
                        level="WARNING",
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
    source_agent_id: int = 0
    target_agent_id: Optional[int] = None
    source_agent_curie: Optional[str] = None
    target_agent_curie: Optional[str] = None
    target_name: Optional[str] = None
    target_uri: Optional[str] = None
    relationship_type: str  # 'affiliation', 'pseudonym_of', 'real_identity_of', 'predecessor_of', 'successor_of', etc.
    created_at: Optional[str] = None

    @model_validator(mode="before")
    @classmethod
    def _sync_agent_rel(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if "source_agent_uri" in data and not data.get("source_agent_curie"):
                data["source_agent_curie"] = data.get("source_agent_uri")
            if "target_agent_uri" in data and not data.get("target_agent_curie"):
                data["target_agent_curie"] = data.get("target_agent_uri")
            if "target_uri" in data and not data.get("target_agent_curie"):
                data["target_agent_curie"] = data.get("target_uri")
            if not data.get("target_uri") and data.get("target_agent_curie"):
                data["target_uri"] = data.get("target_agent_curie")
        return data


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
    sequence_number: Optional[int] = None
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
    constituent_cluster_ids: list[str] = Field(default_factory=list)

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
