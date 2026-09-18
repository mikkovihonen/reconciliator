"""String manipulation and text cleansing utilities."""

from __future__ import annotations

import re


def clean_alphanumeric(value: str) -> str:
    """Strip all characters except ASCII/Unicode digits and lowercase letters."""
    return re.sub(r"[^0-9a-z]", "", str(value or "").strip().lower())
