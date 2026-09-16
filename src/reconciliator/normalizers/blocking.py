"""Domain blocking key generators for multi-tier candidate filtering and clustering."""

from __future__ import annotations

import hashlib
import re
from typing import Optional

from reconciliator.normalizers.agent import extract_surname, normalize_agent_name
from reconciliator.normalizers.record import clean_title


def compute_title_stem(title: str, lang: Optional[str] = None) -> str:
    """Extract a normalized title stem stripped of punctuation, non-filing articles, and accents."""
    if not title:
        return ""
    cleaned = clean_title(title, strip_articles=True, lang=lang)
    # Lowercase and replace non-alphanumeric with spaces
    stem = re.sub(r"[^\w\s]", " ", cleaned.lower(), flags=re.UNICODE)
    # Collapse multiple whitespace
    stem = re.sub(r"\s+", " ", stem).strip()
    return stem


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
    cleaned = re.sub(r"[^\w]", "", sur.lower(), flags=re.UNICODE)
    return cleaned


def deterministic_key_hash(key_content: str) -> str:
    """Generate deterministic 32-character SHA-256 hex digest prefix for blocking keys."""
    return hashlib.sha256(key_content.encode("utf-8")).hexdigest()[:32]


def compute_work_blocking_key(author_key: str, title_stem: str) -> str:
    """Compute primary work blocking key: hash(author_key + '::' + title_stem)."""
    content = f"{author_key.strip().lower()}::{title_stem.strip().lower()}"
    return deterministic_key_hash(content)


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
    if not series_title and not series_issn:
        return None
    s_title = compute_title_stem(series_title or "")
    s_issn = (series_issn or "").strip().lower().replace("-", "")
    content = f"{s_title}::{s_issn}"
    return deterministic_key_hash(content)


def compute_host_citation_key(
    issn: Optional[str],
    year: Optional[int | str],
    start_page: Optional[str],
    author_surname: Optional[str],
) -> Optional[str]:
    """Compute host citation key for journal articles: ISSN::Year::StartPage::AuthorSurname."""
    if not (issn and year):
        return None
    clean_issn = str(issn).strip().lower().replace("-", "")
    clean_year = str(year).strip()
    clean_page = str(start_page or "").strip()
    clean_author = str(author_surname or "").strip().lower()
    return f"{clean_issn}::{clean_year}::{clean_page}::{clean_author}"
