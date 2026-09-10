"""Unicode and diacritic normalisation utilities for Finnish/Swedish cataloguer literals.

Selective combining-mark folding that preserves native Finnish / Swedish åäöÅÄÖ
(both cases) but folds every other Latin diacritic (é, ï, ñ, ü, ç, ...).
Also provides label decoration stripping (trailing parenthetical dates,
biographical dates, and MARC $e role markers).
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any, Final, Optional

#: Native Finnish / Swedish diacritics. Preserved by :func:`fold_diacritics`
#: because their presence carries lexemic meaning (``Häme`` vs ``hame``).
#: KANTO, YSO, and other Finto authorities use proper orthography for these
#: characters, so cataloguer-supplied input matches authority labels
#: only when åäö are kept.
_NATIVE_DIACRITICS: Final[frozenset[str]] = frozenset("åäöÅÄÖ")

#: Trailing biographical-date pattern (parenthetical or comma-separated).
#: Matches:
#: ``"Tolkien, J. R. R. (1892-1973)"`` → ``"Tolkien, J. R. R."``;
#: ``"Fleming, Ian, 1908-1964"`` → ``"Fleming, Ian"``;
#: ``"Laine, Tarja, 1946-"`` → ``"Laine, Tarja"``;
#: ``"Virtanen, Matti, s. 1950"`` → ``"Virtanen, Matti"``;
#: ``"Lindqvist, Johan, 1800-luku"`` → ``"Lindqvist, Johan"``;
#: ``"Bond, James (fiktiivinen hahmo)"`` → unchanged (no date).
_BIOGRAPHICAL_DATE_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"""
    (?:
        # Parenthetical date qualifier
        \s*\(\s*(?:(?:ca\.|circa|n\.|s\.|f\.|b\.|k\.|d\.|fl\.|floruit|born|died)\s*)?
        \b(?:\d{1,4}(?:\s*(?:eaa\.|ekr\.|f\.kr\.|bc|bce))?\s*[-\u2013]\s*\d{0,4}(?:\s*(?:eaa\.|ekr\.|f\.kr\.|bc|bce|jaa\.|jkr\.|ad)\.?)?|\d{1,4}(?:\s*(?:eaa\.|ekr\.|f\.kr\.|bc|bce))?|\d{1,2}(?:th|st|nd|rd)\s+cent(?:\.|ury)?|\d{3,4}-(?:luku|talet|l\.|t\.))\s*\)\s*$
        |
        # Comma-separated date qualifier
        ,\s*(?:(?:ca\.|circa|n\.|s\.|f\.|b\.|k\.|d\.|fl\.|floruit|born|died|död|syntynyt|kuollut)\s*)?
        \b(?:\d{1,4}(?:\s*(?:eaa\.|ekr\.|f\.kr\.|bc|bce))?\s*[-\u2013]\s*\d{0,4}(?:\s*(?:eaa\.|ekr\.|f\.kr\.|bc|bce|jaa\.|jkr\.|ad)\.?)?|\d{1,4}(?:\s*(?:eaa\.|ekr\.|f\.kr\.|bc|bce))?|\d{1,2}(?:th|st|nd|rd)\s+cent(?:\.|ury)?|\d{3,4}-(?:luku|talet|l\.|t\.))\s*\.?$
        |
        # Comma-separated open death date (e.g. , -1985)
        ,\s*[-\u2013]\s*\d{1,4}\s*\.?$
    )
    """,
    re.VERBOSE | re.IGNORECASE,
)


def fold_diacritics(s: str) -> str:
    """Selectively fold Latin diacritics; preserve Finnish / Swedish åäöÅÄÖ.

    NFC-normalises the input, then walks character-by-character: native
    åäö (case-insensitive) pass through untouched; every other character
    is NFKD-decomposed and its combining marks dropped. Examples:

    - ``Häme`` → ``Häme`` (``ä`` protected — distinct from ``hame``)
    - ``Hämeenlinna`` → ``Hämeenlinna`` (both ``ä`` protected)
    - ``Ångström`` → ``Ångström`` (``Å`` and ``ö`` protected)
    - ``Tolstoï`` → ``Tolstoi`` (``ï`` not native; combining diaeresis dropped)
    - ``Müller`` → ``Muller`` (``ü`` not native; folded)
    - ``LINDGRÉN`` → ``LINDGREN`` (``É`` not native; folded)
    """
    nfc = unicodedata.normalize("NFC", s)
    out: list[str] = []
    for ch in nfc:
        if ch in _NATIVE_DIACRITICS:
            out.append(ch)
            continue
        decomposed = unicodedata.normalize("NFKD", ch)
        out.append("".join(c for c in decomposed if not unicodedata.combining(c)))
    return "".join(out)


def has_biographical_dates(s: str) -> bool:
    """Return True if ``s`` contains trailing biographical date qualifiers."""
    return bool(_BIOGRAPHICAL_DATE_PATTERN.search(s.strip()))


def strip_biographical_dates(s: str) -> str:
    """Strip trailing parenthetical or comma-separated biographical dates.

    Preserves base name literals (e.g. ``"Fleming, Ian"`` from
    ``"Fleming, Ian, 1908-1964"``) while leaving non-date qualifiers
    (e.g. ``"Bond, James (fiktiivinen hahmo)"``) intact.
    """
    cleaned = s
    while True:
        new_cleaned = _BIOGRAPHICAL_DATE_PATTERN.sub("", cleaned).rstrip()
        if new_cleaned == cleaned:
            break
        cleaned = new_cleaned
    return cleaned.rstrip(",:;/ \t\r\n")


def strip_label_decoration(s: str, relator_registry: Optional[Any] = None) -> str:
    """Strip trailing parenthetical/comma dates + MARC $e role markers + ISBD punctuation.

    If ``relator_registry`` is provided (or has ``is_relator(token)`` method),
    trailing comma-separated segments matching a known role term will be stripped.
    """
    out = _BIOGRAPHICAL_DATE_PATTERN.sub("", s).rstrip()
    if "," in out:
        head, _, tail = out.rpartition(",")
        token = tail.strip().casefold().rstrip(".")
        if relator_registry is not None:
            if hasattr(relator_registry, "is_relator") and relator_registry.is_relator(token):
                out = head.rstrip()
            elif isinstance(relator_registry, (set, frozenset, list, tuple)) and token in relator_registry:
                out = head.rstrip()
    return out.rstrip(".,:;/ \t\r\n")


def fold_label(s: str, relator_registry: Optional[Any] = None) -> str:
    """Canonical label fold for normalised lookup.

    Composes the steps that align cataloguer-supplied literals
    with authority labels at the lexical level:

    1. :func:`strip_label_decoration` — drop trailing biographical
       dates, MARC role markers, and trailing ISBD punctuation.
    2. NFKC Unicode normalisation — collapse compatibility variants.
    3. :func:`fold_diacritics` — fold foreign Latin diacritics,
       preserve Finnish / Swedish ``åäö``.
    4. ``str.casefold`` — lower-case for case-insensitive match.
    5. Whitespace collapse — ``" ".join(s.split())``.
    """
    stripped = strip_label_decoration(s, relator_registry=relator_registry)
    nfkc = unicodedata.normalize("NFKC", stripped)
    folded = fold_diacritics(nfkc).casefold()
    clean = " ".join(folded.split())
    return clean.rstrip(".,:;/")


__all__ = [
    "_BIOGRAPHICAL_DATE_PATTERN",
    "_NATIVE_DIACRITICS",
    "fold_diacritics",
    "fold_label",
    "has_biographical_dates",
    "strip_biographical_dates",
    "strip_label_decoration",
]
