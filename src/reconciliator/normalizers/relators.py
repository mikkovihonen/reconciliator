"""Dynamic relator and contributor role registry.

Replaces hardcoded lists with an extensible, database-backed registry
derived from Finto MTS (Metatietosanasto / RDA-FI) and LoC MARC Relators.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Container, Iterable, Optional

# Baseline seed terms (Finnish, Swedish, English) to ensure safe fallback
# even before full MTS or LoC databases are populated.
_BASELINE_RELATOR_TERMS: frozenset[str] = frozenset(
    {
        # Finnish (RDA-FI)
        "ohjaaja",
        "elokuvaohjaaja",
        "teatteriohjaaja",
        "näyttelijä",
        "näyttelijät",
        "kuvaaja",
        "käsikirjoittaja",
        "säveltäjä",
        "sovittaja",
        "sanoittaja",
        "kääntäjä",
        "suomentaja",
        "toimittaja",
        "päätoimittaja",
        "kirjoittaja",
        "tuottaja",
        "esittäjä",
        "laulaja",
        "muusikko",
        "kuvittaja",
        "taiteilija",
        "äänittäjä",
        "tutkija",
        # Swedish (RDA-SV)
        "regissör",
        "skådespelare",
        "fotograf",
        "manusförfattare",
        "kompositör",
        "arrangör",
        "översättare",
        "redaktör",
        "författare",
        "producent",
        "dirigent",
        "illustratör",
        "konstnär",
        "sångare",
        "musiker",
        "forskare",
        # English (MARC / RDA)
        "author",
        "editor",
        "illustrator",
        "translator",
        "composer",
        "director",
        "producer",
        "actor",
        "performer",
        "singer",
        "musician",
        "photographer",
        "screenwriter",
        "researcher",
    }
)


class RelatorRegistry:
    """Dynamic registry for contributor roles and relator terms."""

    def __init__(
        self,
        finto_store: Optional[Any] = None,
        extra_terms: Optional[Iterable[str]] = None,
        custom_relators: Optional[Iterable[str]] = None,
    ) -> None:
        self.finto_store = finto_store
        self._custom_terms: set[str] = set()
        terms_to_add = list(extra_terms or []) + list(custom_relators or [])
        for term in terms_to_add:
            clean = term.strip().casefold().rstrip(".")
            if clean:
                self._custom_terms.add(clean)

    def load_custom_terms(self, source: Container[str] | Path | str) -> None:
        """Load additional custom relator terms from file or container."""
        if isinstance(source, (Path, str)):
            path = Path(source)
            if path.is_file():
                if path.suffix == ".json":
                    data = json.loads(path.read_text(encoding="utf-8"))
                    if isinstance(data, list):
                        for t in data:
                            self._custom_terms.add(str(t).strip().casefold().rstrip("."))
                    elif isinstance(data, dict):
                        for t in data.keys():
                            self._custom_terms.add(str(t).strip().casefold().rstrip("."))
                else:
                    for line in path.read_text(encoding="utf-8").splitlines():
                        t = line.strip().casefold().rstrip(".")
                        if t and not t.startswith("#"):
                            self._custom_terms.add(t)
        elif isinstance(source, Iterable):
            for t in source:
                clean = str(t).strip().casefold().rstrip(".")
                if clean:
                    self._custom_terms.add(clean)

    def is_relator(self, token: str) -> bool:
        """Check if a token matches a known relator term."""
        clean = token.strip().casefold().rstrip(".")
        if not clean:
            return False

        if clean in self._custom_terms:
            return True

        if self.finto_store is not None and hasattr(self.finto_store, "is_known_relator"):
            if self.finto_store.is_known_relator(clean):
                return True

        return clean in _BASELINE_RELATOR_TERMS

    def strip_relator(self, name: str) -> str:
        """Strip trailing role markers from name literals."""
        cleaned = name.strip()
        if "," in cleaned:
            parts = [p.strip() for p in cleaned.split(",")]
            if len(parts) > 1:
                last = parts[-1].casefold().rstrip(".")
                if self.is_relator(last):
                    cleaned = ", ".join(parts[:-1]).strip()

        # Also strip parenthetical role marker, e.g. "Jean Sibelius (säveltäjä)"
        if "(" in cleaned and cleaned.endswith(")"):
            head, _, paren = cleaned.rpartition("(")
            token = paren.rstrip(")").strip().casefold()
            if self.is_relator(token):
                cleaned = head.strip()

        return cleaned

    def term_to_code(self, token: str) -> Optional[str]:
        """Resolve a natural-language role term to a standard 3-letter MARC relator code."""
        clean = token.strip().casefold().rstrip(".")
        if not clean:
            return None

        if self.finto_store is not None and hasattr(self.finto_store, "get_relator_code"):
            code = self.finto_store.get_relator_code(clean)
            if code:
                return str(code)

        return _BASELINE_TERM_TO_CODE.get(clean)


_BASELINE_TERM_TO_CODE: dict[str, str] = {
    # Finnish (RDA-FI)
    "kirjoittaja": "aut",
    "tekijä": "aut",
    "toimittaja": "edt",
    "päätoimittaja": "edt",
    "kääntäjä": "trl",
    "suomentaja": "trl",
    "kuvittaja": "ill",
    "säveltäjä": "cmp",
    "sovittaja": "arr",
    "sanoittaja": "lyr",
    "ohjaaja": "drt",
    "elokuvaohjaaja": "drt",
    "teatteriohjaaja": "drt",
    "näyttelijä": "act",
    "näyttelijät": "act",
    "kuvaaja": "cng",
    "käsikirjoittaja": "aus",
    "tuottaja": "pro",
    "esittäjä": "prf",
    "laulaja": "sng",
    "muusikko": "mus",
    "taiteilija": "art",
    "äänittäjä": "rcd",
    "tutkija": "res",
    "haastattelija": "ivr",
    "haastateltava": "ive",
    "kertoja": "nrt",
    "valokuvaaja": "pht",
    "kustantaja": "pbl",
    "julkaisija": "pbl",
    # Swedish (RDA-SV)
    "författare": "aut",
    "redaktör": "edt",
    "huvudredaktör": "edt",
    "översättare": "trl",
    "illustratör": "ill",
    "kompositör": "cmp",
    "arrangör": "arr",
    "textförfattare": "lyr",
    "regissör": "drt",
    "skådespelare": "act",
    "fotograf": "pht",
    "manusförfattare": "aus",
    "producent": "pro",
    "dirigent": "cnd",
    "konstnär": "art",
    "sångare": "sng",
    "musiker": "mus",
    "forskare": "res",
    "utgivare": "pbl",
    # English (MARC / RDA)
    "author": "aut",
    "editor": "edt",
    "illustrator": "ill",
    "translator": "trl",
    "composer": "cmp",
    "arranger": "arr",
    "lyricist": "lyr",
    "director": "drt",
    "producer": "pro",
    "actor": "act",
    "performer": "prf",
    "singer": "sng",
    "musician": "mus",
    "photographer": "pht",
    "screenwriter": "aus",
    "researcher": "res",
    "publisher": "pbl",
    "interviewer": "ivr",
    "interviewee": "ive",
    "narrator": "nrt",
}


_DEFAULT_REGISTRY = RelatorRegistry()


def get_default_relator_registry() -> RelatorRegistry:
    """Get the global default RelatorRegistry instance."""
    return _DEFAULT_REGISTRY


__all__ = [
    "RelatorRegistry",
    "get_default_relator_registry",
]
