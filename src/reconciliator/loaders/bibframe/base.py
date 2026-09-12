"""Common extraction logic and data model conversions for BIBFRAME / BFFI records."""

from __future__ import annotations

import re
from typing import Any, Optional

from reconciliator.constants import FICTIONAL_QUALIFIERS
from reconciliator.loaders.bibframe.profiles import BIBFRAMESchemaProfile

# Mapping of Metatietosanasto (MTS, finto.fi/mts/) role URIs/codes to MARC relator codes
MTS_ROLE_MAP: dict[str, str] = {
    "m16": "aut",  # kirjoittaja
    "m18": "trl",  # kääntäjä
    "m20": "ill",  # kuvittaja
    "m22": "cmp",  # säveltäjä
    "m24": "arr",  # sovittaja
    "m26": "edt",  # toimittaja
    "m28": "lyr",  # sanoittaja
    "m30": "pht",  # valokuvaaja
    "m32": "art",  # taiteilija
    "m71": "drt",  # ohjaaja
    "m155": "prf",  # esittäjä
    "m156": "act",  # näyttelijä
    "m157": "sng",  # laulaja
    "m158": "mus",  # muusikko
    "m493": "pbl",  # julkaisija
    "m494": "prt",  # painaja
    "m495": "dst",  # jakelija
}


def normalize_role_uri_or_label(role_str: str, label: Optional[str] = None) -> Optional[str]:
    """Normalize a role URI (LOC relator or MTS) or label into a standard role code/string."""
    clean = role_str.strip() if role_str else ""
    if not clean and label:
        return label.strip()
    if not clean:
        return None

    # 1. Check MTS URN / URL pattern
    c_lower = clean.lower()
    if "urn:nbn:fi:au:mts:" in c_lower or "finto.fi/mts" in c_lower:
        token = clean.rstrip("/").split(":")[-1].split("/")[-1].lower()
        if token in MTS_ROLE_MAP:
            return MTS_ROLE_MAP[token]

    # 2. Check LOC relators
    if "id.loc.gov/vocabulary/relators/" in clean:
        return clean.rstrip("/").split("/")[-1]

    # 3. Use label if present
    if label and label.strip():
        return label.strip()

    if "#" in clean:
        return clean.split("#")[-1]
    return clean.rstrip("/").split("/")[-1]


def extract_provenance_tag(marc_key: Optional[str]) -> Optional[str]:
    """Extract 3-digit MARC field tag from bffi:marcKey if present (e.g. '1001 $a...' -> '100')."""
    if not marc_key:
        return None
    match = re.match(r"^(\d{3})", marc_key.strip())
    return match.group(1) if match else None


def bibframe_to_raw_record(
    source_id: str,
    source: str = "target",
    title: str = "",
    subtitle: Optional[str] = None,
    uniform_title: Optional[str] = None,
    variant_titles: Optional[list[str]] = None,
    publication_year: Optional[str] = None,
    publisher: Optional[str] = None,
    publication_place: Optional[str] = None,
    language: Optional[str] = None,
    original_language: Optional[str] = None,
    edition: Optional[str] = None,
    extent: Optional[str] = None,
    series_title: Optional[str] = None,
    series_numbering: Optional[str] = None,
    volume_designation: Optional[str] = None,
    classifications: Optional[list[dict[str, Any]]] = None,
    isbns: Optional[list[str]] = None,
    issns: Optional[list[str]] = None,
    urns: Optional[list[str]] = None,
    other_ids: Optional[list[str]] = None,
    agents: Optional[list[dict[str, Any]]] = None,
    statement_of_responsibility: Optional[str] = None,
    subjects: Optional[dict[str, list[dict[str, Any]]]] = None,
    profile: Optional[BIBFRAMESchemaProfile] = None,
) -> dict[str, Any]:
    """Build a standard Reconciliator raw record dictionary from parsed BIBFRAME fields."""
    subs = subjects or {
        "topical": [],
        "genre_form": [],
        "geographic": [],
        "medium_of_performance": [],
    }

    return {
        "source_id": source_id,
        "source": source,
        "title": title,
        "subtitle": subtitle,
        "uniform_title": uniform_title,
        "variant_titles": variant_titles or [],
        "publication_year": publication_year,
        "publisher": publisher,
        "publication_place": publication_place,
        "language": language,
        "original_language": original_language,
        "edition": edition,
        "extent": extent,
        "series_title": series_title,
        "series_numbering": series_numbering,
        "volume_designation": volume_designation,
        "classifications": classifications or [],
        "identifiers": {
            "isbn": isbns or [],
            "issn": issns or [],
            "urn": urns or [],
            "other_ids": other_ids or [],
        },
        "agents": agents or [],
        "statement_of_responsibility": statement_of_responsibility,
        "subjects": subs,
    }


def parse_agent_dict(
    name: str,
    field_tag: Optional[str] = None,
    is_personal: bool = True,
    role: Optional[str] = None,
    uri_subfield_0: Optional[list[str]] = None,
    uri_subfield_1: Optional[list[str]] = None,
) -> dict[str, Any]:
    """Helper to construct an agent dict compatible with Reconciliator pipeline."""
    is_fictional = any(q in name.lower() for q in FICTIONAL_QUALIFIERS)
    u0 = [u for u in (uri_subfield_0 or []) if u]
    u1 = [u for u in (uri_subfield_1 or []) if u]

    # Clean Wikidata URIs to subfield_1
    final_u0: list[str] = []
    final_u1: list[str] = []
    for u in u0:
        if "wikidata.org" in u.lower():
            if u not in final_u1:
                final_u1.append(u)
        else:
            if u not in final_u0:
                final_u0.append(u)
    for u in u1:
        if u not in final_u1:
            final_u1.append(u)

    tag = field_tag or ("100" if is_personal else "110")

    return {
        "field_tag": tag,
        "name": name.strip(),
        "is_personal": is_personal,
        "is_fictional": is_fictional,
        "role": role,
        "uri_subfield_0": final_u0,
        "uri_subfield_1": final_u1,
    }
