"""String manipulation and text cleansing utilities."""

from __future__ import annotations

import re
from typing import Optional

# Disallowed XML 1.0 / PostgreSQL control characters:
# Preserves \t (0x09), \n (0x0a), \r (0x0d)
_XML_CONTROL_CHARS_PATTERN = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_ALL_CONTROL_CHARS_PATTERN = re.compile(r"[\x00-\x1f\x7f]")


from reconciliator._core.normalizers import py_clean_alphanumeric


def clean_alphanumeric(value: str) -> str:
    """Strip all characters except ASCII/Unicode digits and lowercase letters."""
    return py_clean_alphanumeric(str(value or ""))


def sanitize_control_characters(
    text: Optional[str], allow_newlines_tabs: bool = True
) -> Optional[str]:
    """Strip NUL bytes and illegal XML 1.0 / ASCII control characters while preserving valid Unicode."""
    if text is None:
        return None
    s = str(text)
    pattern = _XML_CONTROL_CHARS_PATTERN if allow_newlines_tabs else _ALL_CONTROL_CHARS_PATTERN
    return pattern.sub("", s)
