"""Dynamic relator and contributor role registry.

Replaces hardcoded lists with an extensible, database-backed registry
derived from Finto MTS (Metatietosanasto / RDA-FI) and LoC MARC Relators.
"""

from __future__ import annotations

import unicodedata
from typing import Any, Iterable, Optional

from reconciliator.config import DEFAULT_RELATOR_MAPPINGS
from reconciliator.vocab.bridges import get_mts_role_map


def fold_relator_term(token: str) -> str:
    """A diacritic-insensitive lookup key for a relator term.

    Exports disagree about Finnish diacritics, and an unfolded term is not a
    cosmetic problem. `säveltäjä` resolves to `cmp`; the ASCII-folded
    `saveltaja` resolved to nothing, and every link in what followed was silent:

    1. a role that is not a work role files the contribution against the
       *expression* rather than the work;
    2. `WorkConsolidator.load_signatures` reads
       `contributions WHERE entity_type = 'work'`, so the work has no creators;
    3. the metadata score drops from clearing the 0.93 threshold to 0.90, and
       **nothing consolidates** - including records whose titles and creators are
       identical across two sources.

    Found while measuring stage 9 of plans/aggregates.md, on a fixture that
    happened to write the folded spelling.

    Full decomposition rather than `fold_label`, which deliberately *preserves*
    Finnish diacritics because it folds names, where å and a are different
    people. A relator term is a closed vocabulary, so collapsing them is safe
    here and nowhere near names.
    """
    decomposed = unicodedata.normalize("NFKD", token.strip().casefold().rstrip("."))
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


#: Folded view of the configured terms, consulted only when an exact lookup
#: fails, so a term that resolves today resolves to the same code.
_FOLDED_DEFAULTS: dict[str, str] = {}
for _term, _code in DEFAULT_RELATOR_MAPPINGS.items():
    _FOLDED_DEFAULTS.setdefault(fold_relator_term(_term), _code)

# Dynamically constructed from loc-relators-bridge.ttl; preserved for module-level compatibility
MTS_ROLE_MAP: dict[str, str] = get_mts_role_map()


class RelatorRegistry:
    """Dynamic registry for contributor roles and relator terms."""

    def __init__(
        self,
        vocab_store: Optional[Any] = None,
        extra_terms: Optional[Iterable[str]] = None,
        custom_relators: Optional[Iterable[str]] = None,
        relator_mappings: Optional[dict[str, str]] = None,
    ) -> None:
        self.vocab_store = vocab_store
        self._custom_terms: set[str] = set()
        self._custom_mappings: dict[str, str] = {}
        self._folded_custom: dict[str, str] = {}
        if relator_mappings is not None:
            for term, code in relator_mappings.items():
                self.register(code, term)
        terms_to_add = list(extra_terms or []) + list(custom_relators or [])
        for term in terms_to_add:
            clean = term.strip().casefold().rstrip(".")
            if clean:
                self._custom_terms.add(clean)


    def register(self, code: str, term: str) -> None:
        """Register a custom relator term and its mapping to a MARC code."""
        clean_term = term.strip().casefold().rstrip(".")
        clean_code = code.strip().lower()
        if clean_term:
            self._custom_terms.add(clean_term)
            if not hasattr(self, "_custom_mappings"):
                self._custom_mappings = {}
            self._custom_mappings[clean_term] = clean_code
            self._folded_custom.setdefault(fold_relator_term(clean_term), clean_code)

    def is_relator(self, token: str) -> bool:
        """Check if a token matches a known relator term."""
        clean = token.strip().casefold().rstrip(".")
        if not clean:
            return False

        if clean in self._custom_terms:
            return True

        if self.vocab_store is not None and hasattr(self.vocab_store, "is_known_relator"):
            if self.vocab_store.is_known_relator(clean):
                return True

        return (
            clean in DEFAULT_RELATOR_MAPPINGS
            or fold_relator_term(clean) in _FOLDED_DEFAULTS
        )

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

        # Check both stripped token and token with dot
        for cand in (clean, f"{clean}."):
            if hasattr(self, "_custom_mappings") and cand in self._custom_mappings:
                return self._custom_mappings[cand]
            if cand in DEFAULT_RELATOR_MAPPINGS:
                return DEFAULT_RELATOR_MAPPINGS[cand]

        if self.vocab_store is not None and hasattr(self.vocab_store, "get_relator_code"):
            code = self.vocab_store.get_relator_code(clean)
            if code:
                return str(code)

        # Last: the same terms with their diacritics folded away.
        folded = fold_relator_term(clean)
        if folded in getattr(self, "_folded_custom", {}):
            return self._folded_custom[folded]
        if folded in _FOLDED_DEFAULTS:
            return _FOLDED_DEFAULTS[folded]

        return None

    def normalize_role(self, token: str) -> str:
        """Normalize a role string (code, term, abbreviation, URI) into a standard MARC relator code."""
        clean = token.strip().strip(" ,;.")
        if not clean:
            return ""

        c_lower = clean.lower()
        # 1. Check URI patterns
        if "id.loc.gov/vocabulary/relators/" in c_lower:
            code = clean.rstrip("/").split("/")[-1].lower()
            if len(code) == 3 and code.isalpha():
                return code
        if "urn:nbn:fi:au:mts:" in c_lower or "finto.fi/mts" in c_lower:
            token_id = clean.rstrip("/").split(":")[-1].split("/")[-1].lower()
            mts_map = get_mts_role_map(self.vocab_store)
            if token_id in mts_map:
                return mts_map[token_id]

        # 2. Check term/abbreviation in registry / mappings FIRST
        mapped = self.term_to_code(clean)
        if mapped:
            return mapped

        # 3. Check direct 3-letter MARC code
        clean_folded = clean.casefold().rstrip(".")
        if len(clean_folded) == 3 and clean_folded.isalpha():
            return clean_folded

        return clean_folded


_DEFAULT_REGISTRY = RelatorRegistry()


def get_default_relator_registry() -> RelatorRegistry:
    """Get the global default RelatorRegistry instance."""
    return _DEFAULT_REGISTRY


def normalize_role(
    token: str, relator_mappings: Optional[dict[str, str]] = None
) -> str:
    """Normalize a contributor role or relator string to a canonical 3-letter MARC code."""
    if relator_mappings is not None:
        registry = RelatorRegistry(relator_mappings=relator_mappings)
        return registry.normalize_role(token)
    return _DEFAULT_REGISTRY.normalize_role(token)


__all__ = [
    "MTS_ROLE_MAP",
    "fold_relator_term",
    "RelatorRegistry",
    "get_default_relator_registry",
    "get_mts_role_map",
    "normalize_role",
]
