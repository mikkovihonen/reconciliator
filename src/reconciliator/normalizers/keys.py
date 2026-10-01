"""Centralized domain blocking key extraction and entity key shaping.

Derives domain blocking keys across bibliographic formats (MARC 21, BIBFRAME 2.0)
and shapes them into persistent EntityKey rows bound to WEMI entities.
"""

from __future__ import annotations

import json
from typing import Any, Optional, Union

from reconciliator.models import (
    AgentEntity,
    EntityKey,
    IdentifierEntity,
    ManifestationEntity,
    WorkEntity,
)
from reconciliator.normalizers.blocking import (
    compute_author_key,
    compute_host_citation_key,
    compute_music_work_key,
    compute_series_key,
    compute_title_stem,
    compute_work_blocking_key,
)

MARC_NS = {"marc": "http://www.loc.gov/MARC21/slim"}


def _get_datafields(record: Any, tag: str) -> list[Any]:
    """Get all datafield elements for a specific tag from MARC record or element."""
    if hasattr(record, "get_datafields"):
        return record.get_datafields(tag)
    if hasattr(record, "findall"):
        fields = record.findall(f"marc:datafield[@tag='{tag}']", MARC_NS)
        if not fields:
            fields = record.findall(f"datafield[@tag='{tag}']")
        return list(fields)
    return []


def _get_subfields(elem: Any, code: str) -> list[str]:
    """Get all subfield values for a code from a MARC datafield."""
    if hasattr(elem, "subfields"):
        return [sf.value.strip() for sf in elem.subfields if sf.code == code and sf.value]
    if hasattr(elem, "findall"):
        subs = elem.findall(f"marc:subfield[@code='{code}']", MARC_NS)
        if not subs:
            subs = elem.findall(f"subfield[@code='{code}']")
        return [s.text.strip() for s in subs if s.text]
    return []


def _get_subfield(elem: Any, code: str) -> Optional[str]:
    """Get first subfield value for a code from a MARC datafield."""
    subs = _get_subfields(elem, code)
    return subs[0] if subs else None


def encode_analytic(title: str, creator: Optional[str]) -> str:
    """Encode a constituent-work (title, creator) pair as one key value."""
    return json.dumps([title, creator], ensure_ascii=False)


def decode_analytic(value: str) -> tuple[str, Optional[str]]:
    """Decode a key value written by `encode_analytic`."""
    try:
        title, creator = json.loads(value)
    except (ValueError, TypeError):
        return value, None
    return title, creator


def to_manifestation_entity_keys(
    manifestation: Union[ManifestationEntity, str], domain_keys: dict[str, list[str]]
) -> list[EntityKey]:
    """Shape a domain keys dictionary into EntityKey rows bound to a manifestation."""
    manifestation_uri = (
        manifestation.manifestation_uri
        if isinstance(manifestation, ManifestationEntity)
        else str(manifestation)
    )
    return [
        EntityKey(
            entity_type="manifestation",
            entity_id=0,
            key_type=key_type,
            key_value=value,
            entity_uri=manifestation_uri,
        )
        for key_type, values in domain_keys.items()
        for value in values
    ]


def derive_domain_keys(
    record_elem: Any,
    parsed: dict[str, Any],
    work_type: str,
    author_key: Optional[str],
    title_stem: Optional[str],
) -> dict[str, list[str]]:
    """Derive every domain blocking key for one MARC record.

    Single definition of the key bag, shared by the staged `MatchRecord` and by
    the `entity_keys` rows written at ingest, so the two cannot drift apart.
    """
    keys: dict[str, list[str]] = {}

    def _add(key_type: str, val: Optional[str]) -> None:
        if val and str(val).strip():
            clean = str(val).strip()
            bucket = keys.setdefault(key_type, [])
            if clean not in bucket:
                bucket.append(clean)

    idents = parsed.get("identifiers") or {}
    first_issn = (idents.get("issn") or [None])[0]
    first_isbn = (idents.get("isbn") or [None])[0]

    uniform_title = parsed.get("uniform_title")
    title = parsed.get("title")
    agents = parsed.get("agents") or []
    author_surname = agents[0].get("surname") if agents else None

    # Persisted so a MatchRecord can be projected back out of WEMI rows exactly,
    # rather than re-derived from stored titles and hoped to agree.
    _add("author", author_key)
    _add("title_stem", title_stem)
    # Only when the record carries a 240/130. The work row stores the preferred
    # title, which falls back to the 245, so projecting uniform_title from it
    # would invent one for every record that has none.
    _add("uniform_title", parsed.get("uniform_title"))

    _add("work", compute_work_blocking_key(author_key, title_stem))

    if work_type == "music":
        _add("music_work", compute_music_work_key(author_key, uniform_title or title))

    _add("series", compute_series_key(parsed.get("series_title"), first_issn))
    _add("standard", first_isbn or first_issn)

    pub_year = None
    py_raw = parsed.get("publication_year") or ""
    if str(py_raw).isdigit():
        pub_year = int(py_raw)

    start_page = None
    for f in _get_datafields(record_elem, "773"):
        q = _get_subfield(f, "q")
        if q:
            start_page = q
            break
    _add("citation", compute_host_citation_key(first_issn, pub_year, start_page, author_surname))

    # Translators, from the same relator test the serialization engine already uses.
    translator_names = [
        a.get("name")
        for a in agents
        if "trl" in (a.get("role_counts") or {}) or a.get("role") == "trl"
    ]
    if translator_names:
        _add(
            "translators_signature",
            " ".join(sorted(compute_author_key(n, None) for n in translator_names if n)).strip(),
        )

    # Bound-with (sammelband) notes
    for f in _get_datafields(record_elem, "501"):
        note = _get_subfield(f, "a")
        if note:
            _add("sammelband", note)
        for linked in _get_subfields(f, "5"):
            _add("sammelband_holder", linked)
    for f in _get_datafields(record_elem, "014"):
        for val in _get_subfields(f, "a"):
            _add("sammelband", val)

    _add("patent", (idents.get("patent_number") or [None])[0])
    _add("fingerprint", (idents.get("fingerprint") or [None])[0])
    _add("geodata_spatial", (parsed.get("cartographic_coordinates") or [None])[0])
    _add("heritage_model", (parsed.get("digital_file_formats") or [None])[0])

    try:
        from reconciliator.loaders.marcxml import extract_marc_analytics

        for a_title, a_creator in extract_marc_analytics(record_elem):
            _add("analytic", encode_analytic(a_title, a_creator))
            _add("analytic_title", a_title)
    except Exception:
        pass

    return keys


def derive_bibframe_keys(
    work: WorkEntity,
    agents: list[AgentEntity],
    identifiers: list[IdentifierEntity],
    author_key: Optional[str] = None,
    title_stem: Optional[str] = None,
    constituents: Optional[list[Any]] = None,
) -> dict[str, list[str]]:
    """Derive domain blocking keys for a BIBFRAME record."""
    if author_key is None:
        author_key = compute_author_key(agents[0].name if agents else None)
    if title_stem is None:
        title_stem = compute_title_stem(work.title, lang=work.original_language)

    keys: dict[str, list[str]] = {}

    def _add(key_type: str, val: Optional[str]) -> None:
        if val and str(val).strip():
            clean = str(val).strip()
            bucket = keys.setdefault(key_type, [])
            if clean not in bucket:
                bucket.append(clean)

    _add("author", author_key)
    _add("title_stem", title_stem)
    _add("work", compute_work_blocking_key(author_key, title_stem))
    if work.work_type == "music":
        _add("music_work", compute_music_work_key(author_key, work.title))

    first_isbn = next((i.value for i in identifiers if i.scheme == "isbn"), None)
    first_issn = next((i.value for i in identifiers if i.scheme == "issn"), None)
    _add("standard", first_isbn or first_issn)

    if constituents:
        for c in constituents:
            c_title = getattr(c, "title", None)
            if c_title:
                c_creator = getattr(c, "creator", None)
                _add("analytic", encode_analytic(c_title, c_creator))
                _add("analytic_title", c_title)

    return keys


def marc_entity_keys(
    manifestation: Union[ManifestationEntity, str],
    record_elem: Any,
    parsed: dict[str, Any],
    work_type: str,
    author_key: Optional[str],
    title_stem: Optional[str],
) -> list[EntityKey]:
    """Shape MARC domain keys directly into EntityKey rows bound to a manifestation."""
    return to_manifestation_entity_keys(
        manifestation,
        derive_domain_keys(record_elem, parsed, work_type, author_key, title_stem),
    )


def bibframe_entity_keys(
    manifestation: Union[ManifestationEntity, str],
    work: WorkEntity,
    agents: list[AgentEntity],
    identifiers: list[IdentifierEntity],
    author_key: Optional[str] = None,
    title_stem: Optional[str] = None,
    constituents: Optional[list[Any]] = None,
) -> list[EntityKey]:
    """Shape BIBFRAME domain keys directly into EntityKey rows bound to a manifestation."""
    return to_manifestation_entity_keys(
        manifestation,
        derive_bibframe_keys(
            work,
            agents,
            identifiers,
            author_key=author_key,
            title_stem=title_stem,
            constituents=constituents,
        ),
    )


__all__ = [
    "encode_analytic",
    "decode_analytic",
    "to_manifestation_entity_keys",
    "derive_domain_keys",
    "derive_bibframe_keys",
    "marc_entity_keys",
    "bibframe_entity_keys",
]
