"""CURIE (Compact URI) parsing, formatting, and normalization utilities for Reconciliator."""

from __future__ import annotations

import functools
import re
from typing import Any, Optional
from urllib.parse import quote, unquote


@functools.lru_cache(maxsize=1)
def _get_sources_registry() -> dict[str, dict[str, Any]]:
    from reconciliator.config import get_bundled_sources_defaults

    return get_bundled_sources_defaults()


@functools.lru_cache(maxsize=1)
def _get_compiled_uri_rules() -> list[tuple[re.Pattern[str], str, bool]]:
    registry = _get_sources_registry()
    rules: list[tuple[re.Pattern[str], str, bool]] = []
    for src_name, src_data in registry.items():
        if not isinstance(src_data, dict):
            continue
        patterns = src_data.get("uri_patterns") or []
        uppercase = bool(src_data.get("uppercase_identifier", False))
        for pat in patterns:
            rules.append((re.compile(pat, re.I), src_name, uppercase))
    rules.sort(key=lambda r: len(r[0].pattern), reverse=True)
    return rules


@functools.lru_cache(maxsize=1)
def _get_tag_prefix_rules() -> list[tuple[str, str]]:
    registry = _get_sources_registry()
    rules: list[tuple[str, str]] = []
    for src_name, src_data in registry.items():
        if not isinstance(src_data, dict):
            continue
        for prefix in src_data.get("tag_prefixes") or []:
            clean_p = prefix.strip().lower()
            if clean_p.startswith("(") and clean_p.endswith(")"):
                clean_p = clean_p[1:-1]
            rules.append((clean_p, src_name))
    rules.sort(key=lambda r: len(r[0]), reverse=True)
    return rules


@functools.lru_cache(maxsize=1)
def _get_bare_identifier_patterns() -> list[tuple[re.Pattern[str], str, bool]]:
    registry = _get_sources_registry()
    patterns: list[tuple[re.Pattern[str], str, bool]] = []
    for src_name, src_data in registry.items():
        if not isinstance(src_data, dict):
            continue
        pat = src_data.get("identifier_pattern")
        if pat:
            uppercase = bool(src_data.get("uppercase_identifier", False))
            patterns.append((re.compile(pat, re.I), src_name, uppercase))
    return patterns


def clear_curie_cache() -> None:
    _get_sources_registry.cache_clear()
    _get_compiled_uri_rules.cache_clear()
    _get_tag_prefix_rules.cache_clear()
    _get_bare_identifier_patterns.cache_clear()


def encode_curie_reference(reference: str) -> str:
    """Percent-encode curly braces, spaces, controls, and RFC 3986 unwise characters in a CURIE reference.

    W3C CURIE Syntax 1.0 (and RFC 3986 / RFC 3987) disallows raw curly braces ({ and }),
    spaces, and unwise characters (<, >, ", \\, ^, `, |).
    """
    if not reference:
        return ""
    safe_chars = "/:~-._@!$&'()*+,;=?"
    return quote(str(reference).strip(), safe=safe_chars)


def to_curie(source: str, source_id: str) -> str:
    """Format a source and identifier into standard CURIE format (source:source_id)."""
    src = str(source or "").strip().lower()
    sid = str(source_id or "").strip()
    if not src or src == "unknown":
        return encode_curie_reference(sid)
    clean_src = re.sub(r"[^\w\-]", "", src)
    encoded_ref = encode_curie_reference(sid)
    return f"{clean_src}:{encoded_ref}"


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

    # Configured bare identifier patterns (e.g. Q12344)
    for pat, src, uppercase in _get_bare_identifier_patterns():
        if pat.match(raw):
            return (src, raw.upper() if uppercase else raw)

    # Parenthesized MARC prefix e.g. (FI-ASTERI-N)00012345 or (FI-MELINDA)00012345
    marc_match = re.match(r"^\(([A-Za-z0-9\-]+)\)(.+)$", raw)
    if marc_match:
        tag, val = marc_match.groups()
        tag_lower = tag.lower()
        for pfx, src in _get_tag_prefix_rules():
            if pfx in tag_lower:
                return (src, val.strip())
        return (tag_lower, val.strip())

    # Check URI patterns
    for pat, src, uppercase in _get_compiled_uri_rules():
        if pat.search(raw):
            val = pat.sub("", raw).strip().rstrip("/#")
            if uppercase:
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


def format_source_uri(
    source: str,
    identifier: str,
    template: Optional[str] = None,
) -> str:
    """Format source and identifier into canonical URI using configured template."""
    clean_src = str(source or "").strip().lower()
    clean_id = str(identifier or "").strip()
    encoded_id = encode_curie_reference(clean_id)

    if not template:
        registry = _get_sources_registry()
        src_data = registry.get(clean_src)
        if not src_data:
            for k, v in registry.items():
                if isinstance(v, dict) and clean_src in [a.lower() for a in v.get("aliases", [])]:
                    clean_src = k
                    src_data = v
                    break
        if isinstance(src_data, dict):
            template = src_data.get("canonical_uri_template")
            if src_data.get("uppercase_identifier"):
                encoded_id = encoded_id.upper()

    if template:
        tmpl = template.replace("{id}", "{identifier}")
        return tmpl.format(identifier=encoded_id)

    return to_curie(clean_src, clean_id)


def to_actionable_uri(
    curie_or_uri: str,
    template: Optional[str] = None,
    default_source: str = "",
) -> str:
    """Convert any CURIE or URI to its canonical actionable URI using configured template."""
    if not curie_or_uri:
        return ""
    raw = str(curie_or_uri).strip()
    src, sid = parse_curie(raw, default_source=default_source)
    if src and src != "unknown" and sid:
        return format_source_uri(src, sid, template=template)
    return raw


def extract_source_identifier(source: str, curie_or_id: str) -> Optional[str]:
    """Extract bare identifier for a given source from a CURIE, URI, or identifier string."""
    if not curie_or_id:
        return None
    target_src = source.strip().lower()
    src, sid = parse_curie(curie_or_id)
    if src == target_src and sid:
        return sid
    registry = _get_sources_registry()
    src_data = registry.get(target_src)
    if isinstance(src_data, dict):
        pat_str = src_data.get("identifier_pattern")
        if pat_str and re.match(pat_str, curie_or_id.strip(), re.I):
            val = curie_or_id.strip()
            return val.upper() if src_data.get("uppercase_identifier") else val
    return None


def extract_wikidata_qid(curie_or_id: str) -> Optional[str]:
    """Extract bare Wikidata QID (e.g. 'Q12344') from a CURIE, URI, or QID string."""
    return extract_source_identifier("wikidata", curie_or_id)


def format_wikidata_uri(curie_or_qid: str) -> str:
    """Format Wikidata QID or CURIE into canonical Wikidata URI."""
    qid = extract_wikidata_qid(curie_or_qid)
    return format_source_uri("wikidata", qid or curie_or_qid.strip())
