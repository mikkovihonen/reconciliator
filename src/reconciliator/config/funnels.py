"""Domain funnels configuration models."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from reconciliator.config.defaults import get_bundled_funnels_defaults
from reconciliator.funnels.spec import FunnelSpec

_FUNNELS_DEFAULTS = get_bundled_funnels_defaults()


class AudiobookFunnelConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    default_language: str = "fin"
    title_suffix: str = " (äänikirja)"
    undefined_language: str = "und"
    source_id_suffix: str = "_audio"
    expression_uri_prefix: str = "#spoken_word_"
    content_type: str = "spoken_word"


class RecordedSoundFunnelConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    jaccard_threshold: float = 0.75
    max_candidates: int = 50
    match_tier: str = "tier3_tracklist_jaccard"


class NotatedMusicFunnelConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fallback_match_tier: str = "tier2_domain_key"


class FunnelPatternsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    notated_music_title: str = "(?i)score|partituuri|nuotti|stemma"
    podcast_title: str = "podcast"
    parallel_translators_signature: str = "(?i)parallel"
    government_valto_urn: str = "(?i)valto"
    exhibition_title: str = "näyttely"
    legal_statute_title: str = "(?i)laki|asetus|säädös"


class FunnelsConfig(BaseModel):
    """Configuration surface for funnel thresholds, heuristics, and pattern defaults."""

    model_config = ConfigDict(extra="forbid")
    default_match_tier: str = "tier2_domain_key"
    default_confidence: float = 0.90
    audiobook: AudiobookFunnelConfig = Field(
        default_factory=lambda: AudiobookFunnelConfig(
            **dict(_FUNNELS_DEFAULTS.get("audiobook") or {})
        )
    )
    recorded_sound: RecordedSoundFunnelConfig = Field(
        default_factory=lambda: RecordedSoundFunnelConfig(
            **dict(_FUNNELS_DEFAULTS.get("recorded_sound") or {})
        )
    )
    notated_music: NotatedMusicFunnelConfig = Field(
        default_factory=lambda: NotatedMusicFunnelConfig(
            **dict(_FUNNELS_DEFAULTS.get("notated_music") or {})
        )
    )
    patterns: FunnelPatternsConfig = Field(
        default_factory=lambda: FunnelPatternsConfig(
            **dict(_FUNNELS_DEFAULTS.get("patterns") or {})
        )
    )


__all__ = [
    "AudiobookFunnelConfig",
    "FunnelPatternsConfig",
    "FunnelSpec",
    "FunnelsConfig",
    "NotatedMusicFunnelConfig",
    "RecordedSoundFunnelConfig",
]
