"""Linguistic rules and biographical dates configuration models."""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field

from reconciliator.config.defaults import get_bundled_linguistic_defaults

_LING_DEFAULTS = get_bundled_linguistic_defaults()

DEFAULT_LANGUAGE: str = str(_LING_DEFAULTS.get("default_language") or "")
DEFAULT_PRESERVED_DIACRITICS: str = str(_LING_DEFAULTS.get("preserved_diacritics") or "")
DEFAULT_BIOGRAPHICAL_DATES: dict[str, Any] = dict(_LING_DEFAULTS.get("biographical_dates") or {})
DEFAULT_BIOGRAPHICAL_DATE_PREFIXES: list[str] = list(
    DEFAULT_BIOGRAPHICAL_DATES.get("prefixes") or []
)
DEFAULT_BIOGRAPHICAL_ERA_BC: list[str] = list(DEFAULT_BIOGRAPHICAL_DATES.get("era_bc") or [])
DEFAULT_BIOGRAPHICAL_ERA_AD: list[str] = list(DEFAULT_BIOGRAPHICAL_DATES.get("era_ad") or [])
DEFAULT_BIOGRAPHICAL_CENTURY_SUFFIXES: list[str] = list(
    DEFAULT_BIOGRAPHICAL_DATES.get("century_suffixes") or []
)


class BiographicalDatesConfig(BaseModel):
    """Rules for parsing and stripping biographical birth/death date qualifiers."""

    prefixes: list[str] = Field(default_factory=lambda: list(DEFAULT_BIOGRAPHICAL_DATE_PREFIXES))
    era_bc: list[str] = Field(default_factory=lambda: list(DEFAULT_BIOGRAPHICAL_ERA_BC))
    era_ad: list[str] = Field(default_factory=lambda: list(DEFAULT_BIOGRAPHICAL_ERA_AD))
    century_suffixes: list[str] = Field(
        default_factory=lambda: list(DEFAULT_BIOGRAPHICAL_CENTURY_SUFFIXES)
    )
    pattern: Optional[str] = None


class LinguisticConfig(BaseModel):
    """Linguistic, diacritic, and biographical date normalization conventions."""

    default_language: str = DEFAULT_LANGUAGE
    preserved_diacritics: str = DEFAULT_PRESERVED_DIACRITICS
    biographical_dates: BiographicalDatesConfig = Field(default_factory=BiographicalDatesConfig)
