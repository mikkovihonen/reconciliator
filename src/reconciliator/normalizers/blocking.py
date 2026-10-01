"""Domain blocking key generators for multi-tier candidate filtering and clustering."""

from __future__ import annotations

import re
from typing import Optional

from reconciliator._core.normalizers import (
    py_compute_host_citation_key as _rust_compute_host_citation_key,
)
from reconciliator._core.normalizers import (
    py_compute_series_key as _rust_compute_series_key,
)
from reconciliator._core.normalizers import (
    py_compute_work_blocking_key as _rust_compute_work_blocking_key,
)
from reconciliator._core.normalizers import (
    py_deterministic_key_hash as _rust_deterministic_key_hash,
)
from reconciliator.normalizers.agent import extract_surname, normalize_agent_name
from reconciliator.normalizers.record import clean_title


_RE_NON_ALPHANUM_WS = re.compile(r"[^\w\s]", re.UNICODE)
_RE_WHITESPACE = re.compile(r"\s+")
_RE_NON_ALPHANUM = re.compile(r"[^\w]", re.UNICODE)


def compute_title_stem(title: str, lang: Optional[str] = None) -> str:
    """Extract a normalized title stem stripped of punctuation, non-filing articles, and accents."""
    if not title:
        return ""
    cleaned = clean_title(title, strip_articles=True, lang=lang)
    # Lowercase and replace non-alphanumeric with spaces
    stem = _RE_NON_ALPHANUM_WS.sub(" ", cleaned.lower())
    # Collapse multiple whitespace
    return _RE_WHITESPACE.sub(" ", stem).strip()


def compute_author_key(name: Optional[str], surname: Optional[str] = None) -> str:
    """Extract a normalized surname/author slug for work-level blocking."""
    if not name and not surname:
        return ""
    sur = surname or extract_surname(name or "")
    if not sur:
        canonical_name, norm_sur = normalize_agent_name(name or "")
        if norm_sur:
            sur = norm_sur
        elif canonical_name:
            parts = canonical_name.split()
            sur = parts[0] if parts else ""
        else:
            sur = ""
    # Clean non-alphanumeric
    return _RE_NON_ALPHANUM.sub("", sur.lower())


def deterministic_key_hash(key_content: str) -> str:
    """Generate deterministic 32-character SHA-256 hex digest prefix for blocking keys."""
    return _rust_deterministic_key_hash(key_content)


def compute_work_blocking_key(author_key: str, title_stem: str) -> str:
    """Compute primary work blocking key: hash(author_key + '::' + title_stem)."""
    return _rust_compute_work_blocking_key(author_key, title_stem)


def compute_music_work_key(
    composer_key: Optional[str],
    uniform_title: Optional[str],
    opus_number: Optional[str] = None,
) -> Optional[str]:
    """Compute music work blocking key: hash(composer + '::' + uniform_title + '::' + opus)."""
    if not composer_key and not uniform_title:
        return None
    c_part = (composer_key or "").strip().lower()
    t_part = compute_title_stem(uniform_title or "")
    o_part = (opus_number or "").strip().lower()
    content = f"{c_part}::{t_part}::{o_part}"
    return deterministic_key_hash(content)


def compute_series_key(
    series_title: Optional[str],
    series_issn: Optional[str] = None,
) -> Optional[str]:
    """Compute series blocking key: hash(series_title + '::' + series_issn)."""
    stem = compute_title_stem(series_title or "") if series_title else None
    return _rust_compute_series_key(stem, series_issn)


def compute_host_citation_key(
    issn: Optional[str],
    year: Optional[int | str],
    start_page: Optional[str],
    author_surname: Optional[str],
) -> Optional[str]:
    """Compute host citation key for journal articles: ISSN::Year::StartPage::AuthorSurname."""
    year_str = str(year) if year is not None else None
    return _rust_compute_host_citation_key(issn, year_str, start_page, author_surname)
