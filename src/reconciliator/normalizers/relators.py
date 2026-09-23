"""Dynamic relator and contributor role registry.

Replaces hardcoded lists with an extensible, database- and SKOS-bridge-backed registry
derived from configurable bridge files (e.g. LoC MARC Relators -> target ontology).
"""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path
from typing import Any, Iterable, Optional

from reconciliator.vocab.bridges import (
    get_bridged_role_map,
    get_bundled_bridge_dir,
    parse_bridge_turtle,
)


def fold_relator_term(token: str) -> str:
    """A diacritic-insensitive lookup key for a relator term.

    Decomposes Unicode characters (NFKD) and removes combining marks,
    stripping punctuation and whitespace.
    """
    decomposed = unicodedata.normalize("NFKD", token.strip().casefold().rstrip("."))
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


class RelatorRegistry:
    """Dynamic registry for contributor roles, relator terms, and canonical URI resolution."""

    def __init__(
        self,
        vocab_store: Optional[Any] = None,
        extra_terms: Optional[Iterable[str]] = None,
        custom_relators: Optional[Iterable[str]] = None,
        bridge_file: Optional[Path | str] = None,
    ) -> None:
        self.vocab_store = vocab_store
        self.bridge_file = Path(bridge_file) if bridge_file else None
        self._custom_terms: set[str] = set()
        self._custom_mappings: dict[str, str] = {}
        self._folded_custom: dict[str, str] = {}

        # In-memory bridge tables for fallback or standalone use
        self._code_to_pref: dict[tuple[str, str], str] = {}
        self._code_to_source_uri: dict[str, str] = {}
        self._code_to_bridged_uri: dict[str, str] = {}
        self._term_to_code_map: dict[str, str] = {}

        self._load_bridge_definitions()

        terms_to_add = list(extra_terms or []) + list(custom_relators or [])
        for term in terms_to_add:
            clean = term.strip().casefold().rstrip(".")
            if clean:
                self._custom_terms.add(clean)

    def _load_bridge_definitions(self) -> None:
        """Load terms, codes, and URIs from the specified bridge file or bundled bridge."""
        b_path = self.bridge_file or (get_bundled_bridge_dir() / "loc-relators-bridge.ttl")
        if not b_path.is_file():
            return
        try:
            mappings = parse_bridge_turtle(b_path, bridge_vocab="loc-relators")
            for b in mappings:
                src = b["source_uri"]
                tgt = b["target_uri"]
                notation = b.get("notation")
                code = notation or (
                    src.rstrip("/").split("/")[-1].lower() if "relators/" in src else None
                )
                if not code:
                    continue
                code = code.lower()
                self._code_to_source_uri[code] = src
                self._code_to_bridged_uri[code] = tgt

                pref_labels = b.get("pref_labels") or {}
                for lang, lbl in pref_labels.items():
                    if lbl:
                        self._code_to_pref[(code, lang.lower())] = lbl
                        folded = fold_relator_term(lbl)
                        self._term_to_code_map.setdefault(folded, code)
                        self._term_to_code_map.setdefault(lbl.strip().casefold(), code)
                        self._term_to_code_map.setdefault(lbl.strip().casefold().rstrip("."), code)
                        if lang.lower() == "en":
                            loc_term = re.sub(
                                r"\s+agent(\s+.*)?$", r"\1", lbl.strip(), flags=re.IGNORECASE
                            ).strip()
                            if loc_term and loc_term.casefold() != lbl.strip().casefold():
                                self._term_to_code_map.setdefault(fold_relator_term(loc_term), code)
                                self._term_to_code_map.setdefault(loc_term.strip().casefold(), code)
                                self._term_to_code_map.setdefault(
                                    loc_term.strip().casefold().rstrip("."), code
                                )

                alt_labels = b.get("alt_labels") or {}
                for lang, labels in alt_labels.items():
                    for lbl in labels:
                        if lbl:
                            folded = fold_relator_term(lbl)
                            self._term_to_code_map.setdefault(folded, code)
                            self._term_to_code_map.setdefault(lbl.strip().casefold(), code)
                            self._term_to_code_map.setdefault(lbl.strip().casefold().rstrip("."), code)

                self._term_to_code_map.setdefault(code, code)
        except Exception:
            pass

    def register(self, code: str, term: str) -> None:
        """Register a custom relator term and its mapping to a MARC code."""
        clean_term = term.strip().casefold().rstrip(".")
        clean_code = code.strip().lower()
        if clean_term:
            self._custom_terms.add(clean_term)
            self._custom_mappings[clean_term] = clean_code
            self._folded_custom.setdefault(fold_relator_term(clean_term), clean_code)

    def is_relator(self, token: str) -> bool:
        """Check if a token matches a known relator term."""
        clean = token.strip().casefold().rstrip(".")
        if not clean:
            return False

        if clean in self._custom_terms or clean in self._custom_mappings:
            return True

        if fold_relator_term(clean) in self._folded_custom:
            return True

        if self.vocab_store is not None and hasattr(self.vocab_store, "is_known_relator"):
            if self.vocab_store.is_known_relator(clean):
                return True

        return (
            clean in self._term_to_code_map
            or fold_relator_term(clean) in self._term_to_code_map
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

        for cand in (clean, f"{clean}."):
            if cand in self._custom_mappings:
                return self._custom_mappings[cand]

        if self.vocab_store is not None and hasattr(self.vocab_store, "get_relator_code"):
            code = self.vocab_store.get_relator_code(clean)
            if code:
                return str(code)

        folded = fold_relator_term(clean)
        if folded in self._folded_custom:
            return self._folded_custom[folded]
        if clean in self._term_to_code_map:
            return self._term_to_code_map[clean]
        if folded in self._term_to_code_map:
            return self._term_to_code_map[folded]

        return None

    def get_loc_relator_term(self, code: str) -> Optional[str]:
        """Resolve standard MARC relator code to standard LoC MARC relator term derived from bridge label."""
        if not code:
            return None
        clean_code = code.strip().lower()
        en_label = self._code_to_pref.get((clean_code, "en"))
        if not en_label:
            return None
        loc_term = re.sub(
            r"\s+agent(\s+.*)?$", r"\1", en_label.strip(), flags=re.IGNORECASE
        ).strip()
        return loc_term or en_label

    def get_canonical_term(
        self, code: str, lang: str = "fi", role_format: str = "bridged_uri"
    ) -> Optional[str]:
        """Resolve standard MARC relator code to canonical label in target language or LoC term."""
        if not code:
            return None
        clean_code = code.strip().lower()

        if role_format in ("code", "loc_uri"):
            return self.get_loc_relator_term(clean_code)

        clean_lang = lang.strip().lower() if lang else "fi"

        if self.vocab_store is not None and hasattr(self.vocab_store, "get_canonical_relator_term"):
            term = self.vocab_store.get_canonical_relator_term(clean_code, clean_lang)
            if term:
                return term

        # In-memory bridge lookup
        if (clean_code, clean_lang) in self._code_to_pref:
            return self._code_to_pref[(clean_code, clean_lang)]
        if (clean_code, "fi") in self._code_to_pref:
            return self._code_to_pref[(clean_code, "fi")]
        if (clean_code, "en") in self._code_to_pref:
            return self._code_to_pref[(clean_code, "en")]
        for (c, _), term in self._code_to_pref.items():
            if c == clean_code:
                return term

        return None

    def resolve_relator_identifier(
        self, code: str, format_mode: str = "bridged_uri"
    ) -> Optional[str]:
        """Format relator identifier according to format_mode (bridged_uri, code, loc_uri)."""
        if not code:
            return None
        clean_code = code.strip().lower()

        if format_mode == "code":
            return clean_code

        if format_mode == "loc_uri":
            if self.vocab_store is not None and hasattr(self.vocab_store, "get_relator_source_uri"):
                uri = self.vocab_store.get_relator_source_uri(clean_code)
                if uri:
                    return uri
            if clean_code in self._code_to_source_uri:
                return self._code_to_source_uri[clean_code]
            return f"http://id.loc.gov/vocabulary/relators/{clean_code}"

        # Default format_mode: "bridged_uri"
        if self.vocab_store is not None and hasattr(self.vocab_store, "get_relator_bridged_uri"):
            uri = self.vocab_store.get_relator_bridged_uri(clean_code)
            if uri:
                return uri
        if clean_code in self._code_to_bridged_uri:
            return self._code_to_bridged_uri[clean_code]

        # Fallback to LoC URI if no bridged URI exists for this code
        return self.resolve_relator_identifier(clean_code, format_mode="loc_uri")

    def normalize_term_to_canonical(
        self, token: str, lang: str = "fi", role_format: str = "bridged_uri"
    ) -> tuple[Optional[str], Optional[str]]:
        """Normalize any role token (label, abbreviation, code, URI) to (canonical_label, code)."""
        code = self.normalize_role(token)
        if not code or len(code) != 3:
            return None, None
        canonical = self.get_canonical_term(code, lang=lang, role_format=role_format)
        return canonical, code

    def normalize_role(self, token: str) -> str:
        """Normalize a role string (code, term, abbreviation, URI) into a standard 3-letter MARC code."""
        clean = token.strip().strip(" ,;.")
        if not clean:
            return ""

        c_lower = clean.lower()
        # 1. Check URI patterns
        if "id.loc.gov/vocabulary/relators/" in c_lower:
            code = clean.rstrip("/").split("/")[-1].lower()
            if len(code) == 3 and code.isalpha():
                return code

        bridged_map = get_bridged_role_map(self.vocab_store, bridge_file=self.bridge_file)
        if c_lower in bridged_map:
            return bridged_map[c_lower]

        token_id = clean.rstrip("/").split(":")[-1].split("/")[-1].split("#")[-1].lower()
        if token_id in bridged_map:
            return bridged_map[token_id]

        # 2. Check term/abbreviation in registry / mappings
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


def normalize_role(token: str) -> str:
    """Normalize a contributor role or relator string to a canonical 3-letter MARC code."""
    return _DEFAULT_REGISTRY.normalize_role(token)


__all__ = [
    "fold_relator_term",
    "get_bridged_role_map",
    "get_default_relator_registry",
    "normalize_role",
    "RelatorRegistry",
]
