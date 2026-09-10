"""Centralized constants for Reconciliator.

Contains shared fictional qualifiers across Finnish, Swedish, and English,
and vocabulary source token definitions.
"""

from __future__ import annotations

from typing import Final

#: Fictional qualifiers in Finnish, Swedish, and English.
#: Cataloguer literals tagged with these markers represent fictitious entities
#: (e.g. "Bond, James (fiktiivinen hahmo)") and must NOT be linked to real living
#: or historical person authorities in KANTO / Asteri.
FICTIONAL_QUALIFIERS: Final[tuple[str, ...]] = (
    # Characters (Person / Agent)
    "(fiktiivinen hahmo)",
    "(fiktiv gestalt)",
    "(fictitious character)",
    # Places / Geographic
    "(fiktiivinen paikka)",
    "(fiktiv plats)",
    "(fictitious place)",
    # Organizations / Corporate Bodies
    "(fiktiivinen yhteisö)",
    "(fiktiv organisation)",
    "(fictitious organization)",
    # Objects / Vessels
    "(fiktiivinen esine)",
    "(fiktivt föremål)",
    "(fiktiivinen alus)",
)

__all__ = [
    "FICTIONAL_QUALIFIERS",
]

