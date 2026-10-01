"""Unicode and diacritic normalisation utilities for Finnish/Swedish cataloguer literals.

Selective combining-mark folding that preserves native Finnish / Swedish åäöÅÄÖ
(both cases) but folds every other Latin diacritic (é, ï, ñ, ü, ç, ...).
Also provides label decoration stripping (trailing parenthetical dates,
biographical dates, and MARC $e role markers).
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any, Container, Final, Iterable, Optional

from reconciliator._core.normalizers import (
    py_fold_diacritics as _rust_fold_diacritics,
)
from reconciliator._core.normalizers import (
    py_fold_label as _rust_fold_label,
)
from reconciliator.config import (
    DEFAULT_BIOGRAPHICAL_CENTURY_SUFFIXES,
    DEFAULT_BIOGRAPHICAL_DATE_PREFIXES,
    DEFAULT_BIOGRAPHICAL_ERA_AD,
    DEFAULT_BIOGRAPHICAL_ERA_BC,
)


def build_biographical_date_pattern(
    prefixes: Optional[Iterable[str]] = None,
    era_bc: Optional[Iterable[str]] = None,
    era_ad: Optional[Iterable[str]] = None,
    century_suffixes: Optional[Iterable[str]] = None,
    custom_pattern: Optional[str] = None,
) -> re.Pattern[str]:
    """Compile biographical date stripping pattern from modular qualifier components or regex."""
    if custom_pattern:
        return re.compile(custom_pattern, re.VERBOSE | re.IGNORECASE)

    pref_list = sorted(
        (
            p.strip()
            for p in (prefixes if prefixes is not None else DEFAULT_BIOGRAPHICAL_DATE_PREFIXES)
            if p.strip()
        ),
        key=len,
        reverse=True,
    )
    bc_list = sorted(
        (
            b.strip()
            for b in (era_bc if era_bc is not None else DEFAULT_BIOGRAPHICAL_ERA_BC)
            if b.strip()
        ),
        key=len,
        reverse=True,
    )
    ad_list = sorted(
        (
            a.strip()
            for a in (era_ad if era_ad is not None else DEFAULT_BIOGRAPHICAL_ERA_AD)
            if a.strip()
        ),
        key=len,
        reverse=True,
    )
    cent_list = sorted(
        (
            c.strip()
            for c in (
                century_suffixes
                if century_suffixes is not None
                else DEFAULT_BIOGRAPHICAL_CENTURY_SUFFIXES
            )
            if c.strip()
        ),
        key=len,
        reverse=True,
    )

    all_era = sorted(set(bc_list) | set(ad_list), key=len, reverse=True)

    pref_re = r"(?:(?:" + "|".join(re.escape(p) for p in pref_list) + r")\s*)?" if pref_list else ""
    bc_re = r"(?:\s*(?:" + "|".join(re.escape(b) for b in bc_list) + r"))?" if bc_list else ""
    all_era_re = (
        r"(?:\s*(?:" + "|".join(re.escape(e) for e in all_era) + r")\.?)?" if all_era else ""
    )
    cent_re = (
        r"(?:-(?:" + "|".join(re.escape(c) for c in cent_list) + r"))"
        if cent_list
        else r"(?:-(?:luku|talet|l\.|t\.))"
    )

    core_date = (
        rf"(?:\d{{1,4}}{bc_re}\s*[-\u2013]\s*\d{{0,4}}{all_era_re}"
        rf"|\d{{1,4}}{bc_re}"
        r"|\d{1,2}(?:th|st|nd|rd)\s+cent(?:\.|ury)?"
        rf"|\d{{3,4}}{cent_re})"
    )

    pattern_str = rf"""(?:
        \s*\(\s*{pref_re}\b{core_date}\s*\)\s*$
        |
        ,\s*{pref_re}\b{core_date}\s*\.?$
        |
        ,\s*[-\u2013]\s*\d{{1,4}}\s*\.?$
    )"""

    return re.compile(pattern_str, re.VERBOSE | re.IGNORECASE)


#: Trailing biographical-date pattern (parenthetical or comma-separated).
_BIOGRAPHICAL_DATE_PATTERN: Final[re.Pattern[str]] = build_biographical_date_pattern()


def fold_diacritics(s: str, preserved_diacritics: Optional[Container[str] | str] = None) -> str:
    """Selectively fold Latin diacritics; preserve specified diacritics (default: Finnish / Swedish åäöÅÄÖ).

    NFC-normalises the input, then walks character-by-character: preserved
    characters pass through untouched; every other character
    is NFKD-decomposed and its combining marks dropped.
    """
    if preserved_diacritics is None or isinstance(preserved_diacritics, str):
        return _rust_fold_diacritics(s, preserved_diacritics)
    return _rust_fold_diacritics(s, "".join(preserved_diacritics))


def has_biographical_dates(s: str, date_pattern: Optional[re.Pattern[str]] = None) -> bool:
    """Return True if ``s`` contains trailing biographical date qualifiers."""
    pattern = date_pattern or _BIOGRAPHICAL_DATE_PATTERN
    return bool(pattern.search(s.strip()))


def strip_biographical_dates(s: str, date_pattern: Optional[re.Pattern[str]] = None) -> str:
    """Strip trailing parenthetical or comma-separated biographical dates.

    Preserves base name literals (e.g. ``"Fleming, Ian"`` from
    ``"Fleming, Ian, 1908-1964"``) while leaving non-date qualifiers
    (e.g. ``"Bond, James (fiktiivinen hahmo)"``) intact.
    """
    pattern = date_pattern or _BIOGRAPHICAL_DATE_PATTERN
    cleaned = s
    while True:
        new_cleaned = pattern.sub("", cleaned).rstrip()
        if new_cleaned == cleaned:
            break
        cleaned = new_cleaned
    return cleaned.rstrip(",:;/ \t\r\n")


def strip_label_decoration(
    s: str,
    relator_registry: Optional[Any] = None,
    date_pattern: Optional[re.Pattern[str]] = None,
) -> str:
    """Strip trailing parenthetical/comma dates + MARC $e role markers + ISBD punctuation.

    If ``relator_registry`` is provided (or has ``is_relator(token)`` method),
    trailing comma-separated segments matching a known role term will be stripped.
    """
    pattern = date_pattern or _BIOGRAPHICAL_DATE_PATTERN
    out = pattern.sub("", s).rstrip()
    if "," in out:
        head, _, tail = out.rpartition(",")
        token = tail.strip().casefold().rstrip(".")
        if relator_registry is not None:
            if hasattr(relator_registry, "is_relator") and relator_registry.is_relator(token):
                out = head.rstrip()
            elif (
                isinstance(relator_registry, (set, frozenset, list, tuple))
                and token in relator_registry
            ):
                out = head.rstrip()
    return out.rstrip(".,:;/ \t\r\n")


def fold_label(
    s: str,
    relator_registry: Optional[Any] = None,
    preserved_diacritics: Optional[Container[str] | str] = None,
    date_pattern: Optional[re.Pattern[str]] = None,
) -> str:
    """Canonical label fold for normalised lookup.

    Composes the steps that align cataloguer-supplied literals
    with authority labels at the lexical level:

    1. :func:`strip_label_decoration` — drop trailing biographical
       dates, MARC role markers, and trailing ISBD punctuation.
    2. NFKC Unicode normalisation — collapse compatibility variants.
    3. :func:`fold_diacritics` — fold foreign Latin diacritics,
       preserve specified diacritics (default: Finnish / Swedish ``åäö``).
    4. ``str.casefold`` — lower-case for case-insensitive match.
    5. Whitespace collapse — ``" ".join(s.split())``.
    """
    if relator_registry is None and date_pattern is None:
        pres = (
            preserved_diacritics
            if (preserved_diacritics is None or isinstance(preserved_diacritics, str))
            else "".join(preserved_diacritics)
        )
        return _rust_fold_label(s, pres)

    stripped = strip_label_decoration(
        s, relator_registry=relator_registry, date_pattern=date_pattern
    )
    nfkc = unicodedata.normalize("NFKC", stripped)
    folded = fold_diacritics(nfkc, preserved_diacritics=preserved_diacritics).casefold()
    clean = " ".join(folded.split())
    return clean.rstrip(".,:;/")


__all__ = [
    "_BIOGRAPHICAL_DATE_PATTERN",
    "build_biographical_date_pattern",
    "fold_diacritics",
    "fold_label",
    "has_biographical_dates",
    "strip_biographical_dates",
    "strip_label_decoration",
]
