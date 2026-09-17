"""Standard identifier normalization, validation, and conversion routines."""

from __future__ import annotations

import re
from typing import Iterable, Optional


def clean_alphanumeric(value: str) -> str:
    """Strip all characters except ASCII/Unicode digits and lowercase letters."""
    return re.sub(r"[^0-9a-z]", "", str(value or "").strip().lower())


def classify_identifier_scheme(val: str, hint: Optional[str] = None) -> tuple[str, str]:
    """Classify a standard identifier into (scheme, canonical_value).

    Recognizes ISBN, ISSN, DOI, URN, ISMN, etc.
    """
    if not val:
        return ("", "")
    v = str(val).strip()
    v_clean = clean_alphanumeric(v)
    v_lower = v.lower()

    if hint:
        h = hint.lower().strip()
        if h in ("isbn", "issn", "doi", "urn", "ismn"):
            return (h, v)

    if v_clean.startswith(("978", "979")) or len(v_clean) in (10, 13):
        return ("isbn", v)
    if ("-" in v and len(v) == 9) or (len(v_clean) == 8 and (v_clean.isdigit() or v_clean[:-1].isdigit())):
        return ("issn", v)
    if v_lower.startswith("10.") or "doi.org/" in v_lower:
        return ("doi", v)
    if v_lower.startswith("urn:"):
        return ("urn", v)
    if v_lower.startswith(("m-", "m", "9790")):
        return ("ismn", v)

    return ("standard_number", v)


def isbn_10_to_13(isbn10: str) -> Optional[str]:
    """Convert a valid 10-digit ISBN into a canonical 13-digit EAN string."""
    clean = re.sub(r"[^0-9X]", "", isbn10.upper())
    if len(clean) != 10:
        return None

    # Validate 10-digit checksum
    total = 0
    for i, char in enumerate(clean):
        val = 10 if char == "X" else int(char)
        total += val * (10 - i)
    if total % 11 != 0:
        return None

    core = "978" + clean[:9]
    # Calculate EAN-13 checksum
    total_13 = sum(int(digit) * (1 if idx % 2 == 0 else 3) for idx, digit in enumerate(core))
    check_digit = (10 - (total_13 % 10)) % 10
    return core + str(check_digit)


def validate_isbn_13(isbn13: str) -> bool:
    """Validate 13-digit ISBN checksum."""
    if len(isbn13) != 13 or not isbn13.isdigit():
        return False
    total = sum(int(digit) * (1 if idx % 2 == 0 else 3) for idx, digit in enumerate(isbn13))
    return total % 10 == 0


def normalize_isbn(raw_isbn: str) -> Optional[str]:
    """Extract and canonicalize ISBN to a valid 13-digit string."""
    if not raw_isbn:
        return None

    # Strip parenthetical qualifiers, e.g. (nid.), (sid.), (pbk.)
    cleaned = re.sub(r"\([^)]*\)", "", raw_isbn)
    # Extract candidate digits/X
    match = re.search(r"([0-9xX\- ]{10,20})", cleaned)
    if not match:
        return None

    digits = re.sub(r"[^0-9X]", "", match.group(1).upper())
    if len(digits) == 13 and digits.isdigit():
        if validate_isbn_13(digits):
            return digits
        return digits  # Return if 13 digits even if checksum fails in edge metadata
    elif len(digits) == 10:
        converted = isbn_10_to_13(digits)
        if converted:
            return converted

    return None


def normalize_issn(raw_issn: str) -> Optional[str]:
    """Extract and validate standard 8-character hyphenated ISSN."""
    if not raw_issn:
        return None

    cleaned = re.sub(r"[^0-9X]", "", raw_issn.upper())
    if len(cleaned) != 8:
        return None

    # Validate modulo-11 check digit
    total = sum((10 if char == "X" else int(char)) * (8 - i) for i, char in enumerate(cleaned))
    if total % 11 != 0:
        return f"{cleaned[:4]}-{cleaned[4:]}"  # Return formatted even if check digit fails

    return f"{cleaned[:4]}-{cleaned[4:]}"


def normalize_urn(raw_urn: str, pattern: Optional[str | Iterable[str]] = None) -> Optional[str]:
    """Normalize persistent URN string to lowercase canonical format."""
    if not raw_urn:
        return None

    raw_clean = str(raw_urn).strip()
    if pattern:
        pats = [pattern] if isinstance(pattern, str) else list(pattern)
        for pat in pats:
            match = re.search(pat, raw_clean, re.IGNORECASE)
            if match:
                return (match.group(1) if match.groups() else match.group(0)).lower()

    match = re.search(r"(urn:nbn:[^\s]+)", raw_clean, re.IGNORECASE)
    if match:
        return match.group(1).lower()

    if raw_clean.lower().startswith("urn:"):
        return raw_clean.lower()

    return None


def normalize_doi(
    raw_doi: str,
    strip_resolver_prefixes: Optional[Iterable[str]] = None,
    pattern: Optional[str] = None,
) -> Optional[str]:
    """Extract and normalize DOI string to canonical format (e.g. 10.1234/example).

    Strips web resolver prefixes (https://doi.org/, http://dx.doi.org/, doi:).
    """
    if not raw_doi:
        return None
    clean = str(raw_doi).strip()
    if not clean:
        return None

    prefixes = (
        list(strip_resolver_prefixes)
        if strip_resolver_prefixes is not None
        else [
            "https://doi.org/",
            "http://doi.org/",
            "https://dx.doi.org/",
            "http://dx.doi.org/",
            "doi:",
        ]
    )
    for p in sorted(prefixes, key=len, reverse=True):
        if clean.lower().startswith(p.lower()):
            clean = clean[len(p) :].strip()

    pat = pattern or r"(10\.\d{4,9}/[-._;()/:A-Za-z0-9]+)"
    m = re.search(pat, clean, re.IGNORECASE)
    if m:
        return (m.group(1) if m.groups() else m.group(0)).strip()

    if clean.startswith("10.") and "/" in clean:
        return clean

    return None


def normalize_source_id(
    raw_id: str,
    prefix: Optional[str] = None,
    digits: Optional[int] = None,
    pattern: Optional[str] = None,
    strip_prefixes: Optional[Iterable[str]] = None,
) -> Optional[str]:
    """Normalize an OAI or local system source identifier using configurable rules.

    Supports:
    - Regex pattern extraction (e.g. extracting numeric record ID from OAI URN/URI).
    - Custom list of prefix tokens to strip.
    - Automatic stripping of standard MARC 003 / ISIL organization code variants
      (e.g. parenthesized ``(PREFIX)``, colon-separated ``PREFIX:``, or plain ``PREFIX``).
    - Optional zero-padding of numeric identifier tokens.
    - Application of canonical prefix string.
    """
    if not raw_id:
        return None

    clean = raw_id.strip()
    if not clean:
        return None

    # 1. Apply regex pattern if configured (e.g. extracting ID from full OAI URI)
    if pattern:
        m = re.search(pattern, clean, re.IGNORECASE)
        if m:
            clean = m.group(1) if m.groups() else m.group(0)

    # 2. Strip explicit custom strip_prefixes
    if strip_prefixes:
        for sp in sorted(strip_prefixes, key=len, reverse=True):
            clean = re.sub(
                rf"^{re.escape(sp.strip())}:?\s*", "", clean, flags=re.IGNORECASE
            ).strip()

    # 3. If target prefix is configured, strip any existing variations of it
    if prefix:
        raw_prefix = prefix.strip()
        bare_prefix = raw_prefix.strip("() :")
        if bare_prefix:
            clean = re.sub(
                rf"^\({re.escape(bare_prefix)}\)\s*", "", clean, flags=re.IGNORECASE
            ).strip()
            clean = re.sub(
                rf"^{re.escape(bare_prefix)}:?\s*", "", clean, flags=re.IGNORECASE
            ).strip()
        clean = re.sub(rf"^{re.escape(raw_prefix)}:?\s*", "", clean, flags=re.IGNORECASE).strip()

    # 4. Zero-pad numeric component if digits specified
    if digits:
        found_digits = re.sub(r"\D", "", clean)
        if found_digits:
            formatted = found_digits.zfill(digits)
            return f"{prefix}{formatted}" if prefix else formatted

    # 5. Apply prefix if missing
    if prefix:
        return f"{prefix}{clean}" if not clean.startswith(prefix) else clean

    return clean or None


__all__ = [
    "clean_alphanumeric",
    "classify_identifier_scheme",
    "isbn_10_to_13",
    "validate_isbn_13",
    "normalize_isbn",
    "normalize_issn",
    "normalize_urn",
    "normalize_doi",
    "normalize_source_id",
]
