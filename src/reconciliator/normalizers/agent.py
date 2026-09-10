"""Agent name normalization, date stripping, and surname extraction."""

from __future__ import annotations

import re
from typing import Optional

from reconciliator.normalizers.linguistic import strip_biographical_dates
from reconciliator.normalizers.relators import RelatorRegistry, get_default_relator_registry

COMMON_RELATOR_WORDS = {
    "kirjoittaja",
    "toimittaja",
    "kuvittaja",
    "suomentaja",
    "kääntäjä",
    "säveltäjä",
    "sovittaja",
    "sanoittaja",
    "esiintyjä",
    "ohjaaja",
    "tuottaja",
    "author",
    "editor",
    "illustrator",
    "translator",
    "composer",
    "författare",
    "redaktör",
    "illustratör",
    "översättare",
}


def strip_agent_dates(name: str) -> str:
    """Remove birth/death years from name string."""
    # Pattern matching: , 1908-1979 or , 1908- or , s. 1950 or , 1908
    cleaned = re.sub(r",?\s*\b(?:s\.|d\.)?\s*\d{4}\s*-\s*(?:\d{4})?\b", "", name)
    cleaned = re.sub(r",?\s*\b(1[7-9]\d{2}|20\d{2})\b", "", cleaned)
    return cleaned.strip().rstrip(",")
    """Remove birth/death years and biographical lifespans from name string."""
    return strip_biographical_dates(name)


def strip_agent_relators(name: str) -> str:
    """Remove trailing relator terms."""
    parts = [p.strip() for p in name.split(",")]
    if len(parts) > 1:
        last = parts[-1].lower().rstrip(".")
        if last in COMMON_RELATOR_WORDS:
            return ", ".join(parts[:-1]).strip()
    return name.strip()
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


def normalize_agent_name(raw_name: str, is_personal: bool = True) -> tuple[str, Optional[str]]:
def normalize_agent_name(
    raw_name: str,
    is_personal: bool = True,
    relator_registry: Optional[RelatorRegistry] = None,
) -> tuple[str, Optional[str]]:
    """
    Normalize agent name into canonical representation.
    For personal names: canonical inverted order "Surname, Forename".
    Returns (canonical_name, surname).
    """
    if not raw_name:
        return "", None

    cleaned = strip_agent_dates(raw_name)
    cleaned = strip_agent_relators(cleaned)
    cleaned = strip_agent_relators(cleaned, relator_registry=relator_registry)
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


def parse_245c_agents(statement_of_responsibility: str) -> list[str]:
    """Fallback extraction of personal author names from MARC 245 $c."""
    if not statement_of_responsibility:
        return []

    # Clean lead-in words
    text = statement_of_responsibility.strip().rstrip(".")
    # Remove phrases like "kirjoittanut", "av", "by", "toimittanut"
    clean_text = re.sub(
        r"\b(kirjoittanut|toimittanut|suomentanut|kuvittanut|av|redigerad av|by|edited by)\b:?",
        "",
        text,
        flags=re.IGNORECASE,
    )

    names: list[str] = []
    # Split by semicolon, comma, or "ja" / "och" / "and"
    chunks = re.split(r";|\b(?:ja|och|and|&)\b", clean_text, flags=re.IGNORECASE)
    for chunk in chunks:
        candidate = chunk.strip().rstrip(",")
        if candidate and len(candidate) > 2:
            norm_name, _ = normalize_agent_name(candidate, is_personal=True)
            if norm_name:
                names.append(norm_name)
    return names
