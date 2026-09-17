"""CURIE (Compact URI) parsing, formatting, and normalization utilities for Reconciliator."""

from __future__ import annotations

import re
from typing import Optional
from urllib.parse import unquote

# Known authority prefix mappings for normalization from URIs to canonical CURIEs
_URI_PREFIX_TO_SOURCE: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"^https?://(?:www\.)?wikidata\.org/(?:wiki|entity)/", re.I), "wikidata"),
    (re.compile(r"^https?://(?:www\.)?isni\.org/isni/", re.I), "isni"),
    (re.compile(r"^https?://viaf\.org/viaf/", re.I), "viaf"),
    (re.compile(r"^https?://orcid\.org/", re.I), "orcid"),
    (re.compile(r"^https?://urn\.fi/URN:NBN:fi:au:pn:", re.I), "kanto"),
    (re.compile(r"^https?://urn\.fi/URN:NBN:fi:au:cn:", re.I), "kanto"),
    (re.compile(r"^https?://urn\.fi/URN:NBN:fi:au:slm:", re.I), "slm"),
    (re.compile(r"^https?://urn\.fi/URN:NBN:fi:au:seko:", re.I), "seko"),
    (re.compile(r"^https?://urn\.fi/URN:NBN:fi:au:mts:", re.I), "mts"),
    (re.compile(r"^https?://urn\.fi/URN:NBN:fi:", re.I), "urn"),
    (re.compile(r"^https?://finto\.fi/yso/[a-z]{2}/page/", re.I), "yso"),
    (re.compile(r"^https?://finto\.fi/kauno/[a-z]{2}/page/", re.I), "kauno"),
    (re.compile(r"^https?://finto\.fi/muso/[a-z]{2}/page/", re.I), "muso"),
    (re.compile(r"^https?://finto\.fi/slm/[a-z]{2}/page/", re.I), "slm"),
    (re.compile(r"^https?://finto\.fi/seko/[a-z]{2}/page/", re.I), "seko"),
    (re.compile(r"^http://www\.yso\.fi/onto/yso/", re.I), "yso"),
    (re.compile(r"^http://www\.yso\.fi/onto/kauno/", re.I), "kauno"),
    (re.compile(r"^http://www\.yso\.fi/onto/muso/", re.I), "muso"),
    (re.compile(r"^https?://(?:www\.)?yso\.fi/onto/ysa/", re.I), "ysa"),
    (re.compile(r"^https?://(?:www\.)?yso\.fi/onto/allars/", re.I), "allars"),
    (re.compile(r"^https?://(?:www\.)?yso\.fi/onto/kaunokki/", re.I), "kaunokki"),
    (re.compile(r"^https?://(?:www\.)?yso\.fi/onto/bella/", re.I), "bella"),
    (re.compile(r"^https?://(?:www\.)?yso\.fi/onto/musa/", re.I), "musa"),
    (re.compile(r"^https?://(?:www\.)?yso\.fi/onto/cilla/", re.I), "cilla"),
    (re.compile(r"^http://id\.loc\.gov/authorities/names/", re.I), "loc"),
    (re.compile(r"^http://id\.loc\.gov/vocabulary/relators/", re.I), "marcrelator"),
    (re.compile(r"^https?://data\.bnf\.fr/ark:/12148/cb", re.I), "bnf"),
    (re.compile(r"^https?://d-nb\.info/gnd/", re.I), "gnd"),
]

_WIKIDATA_QID_PATTERN = re.compile(r"^Q\d+$", re.I)


def to_curie(source: str, source_id: str) -> str:
    """Format a source and identifier into standard CURIE format (source:source_id)."""
    src = str(source or "").strip().lower()
    sid = str(source_id or "").strip()
    if not src or src == "unknown":
        return sid
    return f"{src}:{sid}"


def parse_curie(curie_or_uri: str, default_source: str = "") -> tuple[str, str]:
    """Parse a CURIE, URI, or bare identifier into a canonical (source, source_id) tuple.

    Examples:
        - "wikidata:Q12344" -> ("wikidata", "Q12344")
        - "http://www.wikidata.org/entity/Q12344" -> ("wikidata", "Q12344")
        - "https://isni.org/isni/0000000121484501" -> ("isni", "0000000121484501")
        - "http://urn.fi/URN:NBN:fi:au:pn:000012345" -> ("kanto", "000012345")
        - "(FI-ASTERI-N)000012345" -> ("kanto", "000012345")
        - "(FI-MELINDA)000123456" -> ("melinda", "000123456")
        - "Q12344" -> ("wikidata", "Q12344")
        - "urn:isbn:978-951-0-12345-6" -> ("isbn", "978-951-0-12345-6")
    """
    raw = unquote(str(curie_or_uri or "").strip())
    if not raw:
        return (default_source.lower(), "")

    # Bare Wikidata QID
    if _WIKIDATA_QID_PATTERN.match(raw):
        return ("wikidata", raw.upper())

    # Parenthesized MARC prefix e.g. (FI-ASTERI-N)00012345 or (FI-MELINDA)00012345
    marc_match = re.match(r"^\(([A-Za-z0-9\-]+)\)(.+)$", raw)
    if marc_match:
        tag, val = marc_match.groups()
        tag_lower = tag.lower()
        if "asteri" in tag_lower or "kanto" in tag_lower:
            return ("kanto", val.strip())
        if "melinda" in tag_lower:
            return ("melinda", val.strip())
        if "isni" in tag_lower:
            return ("isni", val.strip())
        return (tag_lower, val.strip())

    # Check URI patterns
    for pat, src in _URI_PREFIX_TO_SOURCE:
        if pat.search(raw):
            val = pat.sub("", raw).strip().rstrip("/#")
            if src == "wikidata":
                val = val.split("/")[-1].upper()
            return (src, val)

    # Standard CURIE: "prefix:reference"
    if ":" in raw and not raw.startswith(("http://", "https://")):
        prefix, ref = raw.split(":", 1)
        prefix_clean = prefix.strip().lower()
        ref_clean = ref.strip()
        # Common URN formats e.g. urn:isbn:..., urn:nbn:fi:...
        if prefix_clean == "urn":
            if ":" in ref_clean:
                urn_type, urn_val = ref_clean.split(":", 1)
                urn_type_l = urn_type.lower()
                if urn_type_l in ("isbn", "issn", "doi"):
                    return (urn_type_l, urn_val.strip())
                if urn_type_l in (
                    "work",
                    "exp",
                    "expression",
                    "manif",
                    "manifestation",
                    "agent",
                    "item",
                ):
                    return parse_curie(urn_val, default_source=default_source)
            return ("urn", ref_clean)
        return (prefix_clean, ref_clean)

    # Fallback to default_source if provided
    return (default_source.lower() if default_source else "unknown", raw)


def extract_wikidata_qid(curie_or_id: str) -> Optional[str]:
    """Extract bare Wikidata QID (e.g. 'Q12344') from a CURIE, URI, or QID string."""
    if not curie_or_id:
        return None
    source, source_id = parse_curie(curie_or_id)
    if source == "wikidata" and _WIKIDATA_QID_PATTERN.match(source_id):
        return source_id.upper()
    if _WIKIDATA_QID_PATTERN.match(curie_or_id.strip()):
        return curie_or_id.strip().upper()
    return None


def format_wikidata_uri(curie_or_qid: str) -> str:
    """Format Wikidata QID or CURIE into canonical Wikidata URI."""
    qid = extract_wikidata_qid(curie_or_qid)
    if qid:
        return f"http://www.wikidata.org/entity/{qid}"
    return f"http://www.wikidata.org/entity/{curie_or_qid.strip()}"
