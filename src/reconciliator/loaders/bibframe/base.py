"""Common extraction logic and data model conversions for BIBFRAME / BFFI records."""

from __future__ import annotations

import re
from typing import Any, Optional

from reconciliator.config import (
    DEFAULT_FICTIONAL_QUALIFIERS,
    get_subject_field_category_map,
)
from reconciliator.loaders.bibframe.profiles import BIBFRAMESchemaProfile, get_profile
from reconciliator.vocab.bridges import get_mts_role_map
from reconciliator.vocabularies import get_vocabulary_patterns


def normalize_role_uri_or_label(
    role_str: str,
    label: Optional[str] = None,
    profile: Optional[BIBFRAMESchemaProfile] = None,
    relator_mappings: Optional[dict[str, str]] = None,
) -> Optional[str]:
    """Normalize a role URI or label into a MARC relator code.

    The profile is consulted first, so a deployment can override any role, and
    whatever it does not resolve goes to the shared relator registry rather than
    being returned as it stands. That last step is the point: the registry is
    where Finnish relator terms live, and BFFI - the Finnish BIBFRAME profile -
    writes exactly those as `rdfs:label`. Before it, "kääntäjä" stayed
    "kääntäjä" here while the MARC path turned it into "trl", so one agent's
    `role_counts` merged across a Melinda source and a BFFI source read
    `{"trl": 2, "kääntäjä": 3}` - the same role counted twice under two names,
    on the field the agent footprint compares.
    """
    clean = role_str.strip() if role_str else ""
    lbl = label.strip() if label else ""

    p = profile or get_profile()

    # 1. Match against configured label mappings if label provided
    if lbl and lbl.lower() in p.role_label_mappings:
        return p.role_label_mappings[lbl.lower()]

    if not clean:
        return _shared_role(lbl, relator_mappings) if lbl else None

    c_lower = clean.lower()

    # 2. Check configured role URI prefixes
    for prefix in p.role_uri_prefixes:
        pref_lower = prefix.lower()
        if pref_lower in c_lower:
            token = (
                clean[c_lower.find(pref_lower) + len(pref_lower) :]
                .strip("/")
                .split(":")[-1]
                .split("/")[-1]
                .lower()
            )
            mts_map = get_mts_role_map()
            if token in mts_map:
                return mts_map[token]
            if token in p.role_label_mappings:
                return p.role_label_mappings[token]
            return _shared_role(token, relator_mappings)

    # 3. If label exists and not mapped yet
    if lbl:
        return _shared_role(lbl, relator_mappings)

    # 4. Fall back to URI fragment or tail
    if "#" in clean:
        tail = clean.split("#")[-1].lower()
    else:
        tail = clean.rstrip("/").split("/")[-1].lower()

    if tail in p.role_label_mappings:
        return p.role_label_mappings[tail]
    return _shared_role(tail, relator_mappings)


def _shared_role(token: str, relator_mappings: Optional[dict[str, str]]) -> str:
    """The MARC path's normaliser, applied to whatever the profile left alone.

    Returns the token casefolded when nothing matches, which is what this
    function used to return for every unrecognised role.
    """
    from reconciliator.normalizers.relators import normalize_role

    return normalize_role(token, relator_mappings)


def extract_provenance_tag(
    marc_key: Optional[str],
    profile: Optional[BIBFRAMESchemaProfile] = None,
) -> Optional[str]:
    """Extract 3-digit MARC field tag from provenance literal string using configured regex."""
    if not marc_key:
        return None
    p = profile or get_profile()
    match = re.match(p.provenance_regex, marc_key.strip())
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
    cat_keys = list(get_subject_field_category_map().values())
    subs: dict[str, list[dict[str, Any]]] = {cat: [] for cat in cat_keys}
    if subjects:
        for k, v in subjects.items():
            if k in subs:
                subs[k].extend(v)
            else:
                subs[k] = list(v)

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
    profile: Optional[BIBFRAMESchemaProfile] = None,
) -> dict[str, Any]:
    """Helper to construct an agent dict compatible with Reconciliator pipeline."""
    is_fictional = any(q in name.lower() for q in DEFAULT_FICTIONAL_QUALIFIERS)
    u0 = [u for u in (uri_subfield_0 or []) if u]
    u1 = [u for u in (uri_subfield_1 or []) if u]

    p = profile or get_profile()
    vocab_pats = get_vocabulary_patterns()
    wikidata_patterns = vocab_pats.get("WIKIDATA", ["wikidata.org"])

    final_u0: list[str] = []
    final_u1: list[str] = []
    for u in u0:
        u_low = u.lower()
        if any(pat in u_low for pat in wikidata_patterns):
            if u not in final_u1:
                final_u1.append(u)
        else:
            if u not in final_u0:
                final_u0.append(u)
    for u in u1:
        if u not in final_u1:
            final_u1.append(u)

    tag = field_tag or (
        p.agent_default_tags.get("personal")
        if is_personal
        else p.agent_default_tags.get("corporate")
    )

    return {
        "field_tag": tag,
        "name": name.strip(),
        "is_personal": is_personal,
        "is_fictional": is_fictional,
        "role": role,
        "uri_subfield_0": final_u0,
        "uri_subfield_1": final_u1,
    }
