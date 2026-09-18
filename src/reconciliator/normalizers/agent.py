"""Agent name normalization, date stripping, and surname extraction."""

from __future__ import annotations

import re
from typing import Container, Iterable, Optional

from reconciliator.config import (
    DEFAULT_RESPONSIBILITY_CONJUNCTIONS,
    DEFAULT_RESPONSIBILITY_PREFIXES,
)
from reconciliator.normalizers.linguistic import fold_diacritics, strip_biographical_dates
from reconciliator.normalizers.relators import RelatorRegistry, get_default_relator_registry

_YEAR_RANGE_RE = re.compile(
    r"(?:ca\.?|noin|n\.?)?\s*(\d{4})\s*[-–—]\s*(?:ca\.?|noin|n\.?)?\s*(\d{4})"
)
_BIRTH_ONLY_RE = re.compile(r"(?:ca\.?|noin|n\.?)?\s*(\d{4})\s*[-–—]\s*$")
_DEATH_ONLY_RE = re.compile(r"^[-–—]\s*(?:ca\.?|noin|n\.?)?\s*(\d{4})")
_PREFIX_BIRTH_RE = re.compile(r"(?:s\.|f\.|b\.|synt\.)\s*(\d{4})", re.IGNORECASE)
_PREFIX_DEATH_RE = re.compile(r"(?:k\.|d\.|kuol\.)\s*(\d{4})", re.IGNORECASE)


def parse_agent_dates(dates_str: Optional[str]) -> tuple[Optional[int], Optional[int]]:
    """Extract 4-digit birth and death years from agent date string (e.g. MARC $d)."""
    if not dates_str:
        return None, None
    clean = dates_str.strip().rstrip(".,;")
    # 1. Full range: e.g. "1908-1979", "1850?-1910?" or "ca. 1850 - 1910"
    m = _YEAR_RANGE_RE.search(clean)
    if m:
        return int(m.group(1)), int(m.group(2))
    # 2. Birth with hyphen: "1948-"
    m = _BIRTH_ONLY_RE.search(clean)
    if m:
        return int(m.group(1)), None
    # 3. Death with hyphen: "-1920"
    m = _DEATH_ONLY_RE.search(clean)
    if m:
        return None, int(m.group(1))
    # 4. Prefix birth: "s. 1950"
    m = _PREFIX_BIRTH_RE.search(clean)
    if m:
        return int(m.group(1)), None
    # 5. Prefix death: "k. 2012"
    m = _PREFIX_DEATH_RE.search(clean)
    if m:
        return None, int(m.group(1))
    # 6. Fallback finding 4-digit years
    if not any(w in clean.lower() for w in ("luku", "century", "tal")):
        years = re.findall(r"\b(\d{4})\b", clean)
        if len(years) >= 2:
            return int(years[0]), int(years[1])
        elif len(years) == 1 and any(sep in clean for sep in ("-", "–", "—")):
            y = int(years[0])
            hyphen_idx = min(
                (clean.find(sep) for sep in ("-", "–", "—") if sep in clean),
                default=-1,
            )
            idx = clean.find(years[0])
            if idx < hyphen_idx:
                return y, None
            else:
                return None, y
    return None, None


def format_agent_dates(birth_year: Optional[int], death_year: Optional[int]) -> Optional[str]:
    """Format birth and death years into a standardized date range string."""
    if birth_year is not None and death_year is not None:
        return f"{birth_year}-{death_year}"
    if birth_year is not None:
        return f"{birth_year}-"
    if death_year is not None:
        return f"-{death_year}"
    return None


def strip_agent_dates(name: str, date_pattern: Optional[re.Pattern[str]] = None) -> str:
    """Remove birth/death years and biographical lifespans from name string."""
    return strip_biographical_dates(name, date_pattern=date_pattern)


def strip_agent_relators(name: str, relator_registry: Optional[RelatorRegistry] = None) -> str:
    """Remove trailing relator terms using dynamic registry."""
    registry = relator_registry or get_default_relator_registry()
    return registry.strip_relator(name)


def extract_surname(name: str) -> Optional[str]:
    """Extract primary family name / surname token for hard discriminator."""
    if not name:
        return None
    # If inverted "Surname, Forename"
    if "," in name:
        surname_part = name.split(",")[0].strip()
        tokens = surname_part.split()
        return tokens[0] if tokens else surname_part
    # Direct order: last token
    tokens = name.split()
    return tokens[-1] if tokens else None


def normalize_agent_name(
    raw_name: str,
    is_personal: bool = True,
    relator_registry: Optional[RelatorRegistry] = None,
    preserved_diacritics: Optional[Container[str] | str] = None,
    date_pattern: Optional[re.Pattern[str]] = None,
) -> tuple[str, Optional[str]]:
    """
    Normalize agent name into canonical representation.
    For personal names: canonical inverted order "Surname, Forename".
    Returns (canonical_name, surname).
    """
    if not raw_name:
        return "", None

    cleaned = strip_agent_dates(raw_name, date_pattern=date_pattern)
    cleaned = strip_agent_relators(cleaned, relator_registry=relator_registry)
    if preserved_diacritics is not None:
        cleaned = fold_diacritics(cleaned, preserved_diacritics=preserved_diacritics)
    cleaned = re.sub(r"\s+", " ", cleaned).strip().rstrip(" ,;/")

    if not is_personal:
        return cleaned, None

    if "," in cleaned:
        # Already inverted
        parts = [p.strip() for p in cleaned.split(",", 1)]
        surname = parts[0]
        forename = parts[1] if len(parts) > 1 else ""
        canonical = f"{surname}, {forename}".rstrip(", ")
        return canonical, extract_surname(canonical)

    # Direct order "Forename ... Surname" -> invert
    tokens = cleaned.split()
    if len(tokens) <= 1:
        return cleaned, cleaned

    surname = tokens[-1]
    forenames = " ".join(tokens[:-1])
    canonical = f"{surname}, {forenames}"
    return canonical, surname


def parse_245c_agents(
    statement_of_responsibility: str,
    prefixes: Optional[Iterable[str]] = None,
    conjunctions: Optional[Iterable[str]] = None,
    relator_registry: Optional[RelatorRegistry] = None,
    preserved_diacritics: Optional[Container[str] | str] = None,
    date_pattern: Optional[re.Pattern[str]] = None,
) -> list[str]:
    """Fallback extraction of personal author names from MARC 245 $c."""
    if not statement_of_responsibility:
        return []

    # Clean lead-in words
    text = statement_of_responsibility.strip().rstrip(".")
    prefix_list = prefixes if prefixes is not None else DEFAULT_RESPONSIBILITY_PREFIXES
    escaped_prefixes = [re.escape(p.strip()) for p in prefix_list if p.strip()]
    prefix_pattern = r"\b(?:" + "|".join(escaped_prefixes) + r")\b:?" if escaped_prefixes else ""

    if prefix_pattern:
        clean_text = re.sub(prefix_pattern, "", text, flags=re.IGNORECASE)
    else:
        clean_text = text

    conj_list = conjunctions if conjunctions is not None else DEFAULT_RESPONSIBILITY_CONJUNCTIONS
    escaped_conj = [re.escape(c.strip()) for c in conj_list if c.strip()]
    if escaped_conj:
        split_pattern = r";|&|\b(?:" + "|".join(escaped_conj) + r")\b"
    else:
        split_pattern = r";|&"

    names: list[str] = []
    chunks = re.split(split_pattern, clean_text, flags=re.IGNORECASE)
    for chunk in chunks:
        candidate = chunk.strip().rstrip(",")
        if prefix_pattern:
            candidate = re.sub(prefix_pattern, "", candidate, flags=re.IGNORECASE).strip()
        if candidate and len(candidate) > 2:
            norm_name, _ = normalize_agent_name(
                candidate,
                is_personal=True,
                relator_registry=relator_registry,
                preserved_diacritics=preserved_diacritics,
                date_pattern=date_pattern,
            )
            if norm_name:
                names.append(norm_name)
    return names
