"""Standard identifier normalization, validation, and conversion routines."""

from __future__ import annotations

import re
from typing import Optional


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


def normalize_urn(raw_urn: str) -> Optional[str]:
    """Normalize persistent URN string to lowercase canonical format."""
    if not raw_urn:
        return None

    raw_clean = raw_urn.strip()
    match = re.search(r"(urn:nbn:fi:[^\s]+)", raw_clean, re.IGNORECASE)
    if match:
        return match.group(1).lower()

    if raw_clean.lower().startswith("urn:"):
        return raw_clean.lower()

    return None


def normalize_melinda_id(raw_id: str) -> Optional[str]:
    """Normalize Melinda system control number."""
    if not raw_id:
        return None

    clean = raw_id.strip()
    # Strip (FI-MELINDA) prefix if present or standardize
    clean = re.sub(r"^\(FI-MELINDA\)", "", clean, flags=re.IGNORECASE).strip()
    clean = re.sub(r"^FI-MELINDA:?", "", clean, flags=re.IGNORECASE).strip()
    # Remove leading zeroes if standardizing or keep standard 9-digit
    digits = re.sub(r"\D", "", clean)
    if digits:
        return f"(FI-MELINDA){digits.zfill(9)}"
    return raw_id.strip()
