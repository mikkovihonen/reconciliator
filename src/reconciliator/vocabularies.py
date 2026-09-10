"""Authority vocabulary namespace detection, allowlist filtering, and link prioritization policies."""

from __future__ import annotations

from typing import Literal, Optional

VOCABULARY_PATTERNS = {
    "KANTO": ["urn.fi/urn:nbn:fi:au:pn:", "(fi-asteri-n)", "(fi-kanto)", "asteri"],
    "YSO": ["yso.fi/onto/yso/", "(fi-yso)"],
    "MUSO": ["yso.fi/onto/muso/", "(fi-muso)"],
    "KAUNO": ["yso.fi/onto/kauno/"],
    "SLM": ["urn.fi/urn:nbn:fi:au:slm:"],
    "SEKO": ["urn.fi/urn:nbn:fi:au:seko:"],
    "VIAF": ["viaf.org/viaf/", "(viaf)"],
    "ISNI": ["isni.org/isni/", "(isni)"],
    "WIKIDATA": ["wikidata.org/entity/", "wikidata.org/wiki/"],
}


def detect_vocabulary(uri: str) -> Optional[str]:
    """Detect canonical vocabulary code from URI namespace or identifier string."""
    if not uri:
        return None
    lower_uri = uri.lower().strip()
    for vocab, patterns in VOCABULARY_PATTERNS.items():
        for pat in patterns:
            if pat in lower_uri:
                return vocab
    return None


class VocabularyManager:
    """Manages vocabulary allowlists and link selection prioritization policies."""

    def __init__(
        self,
        allowed_vocabularies: list[str],
        agent_priority: Optional[list[str]] = None,
        subject_priority: Optional[list[str]] = None,
        link_policy: Literal["priority_fallback", "all"] = "priority_fallback",
    ) -> None:
        self.allowed_vocabularies = {v.upper() for v in allowed_vocabularies}
        self.agent_priority = [
            v.upper() for v in (agent_priority or ["KANTO", "ISNI", "VIAF", "WIKIDATA"])
        ]
        self.subject_priority = [
            v.upper() for v in (subject_priority or ["YSO", "KAUNO", "MUSO", "SLM", "SEKO"])
        ]
        self.link_policy = link_policy

    def is_allowed(self, uri: str) -> bool:
        """Check if URI belongs to an allowed vocabulary."""
        vocab = detect_vocabulary(uri)
        if not vocab:
            return False
        return vocab in self.allowed_vocabularies

    def filter_allowed(self, uris: list[str]) -> list[str]:
        """Filter a list of URIs to only those matching allowed vocabularies."""
        return [u for u in uris if self.is_allowed(u)]

    def select_agent_uris(self, uris: list[str]) -> tuple[list[str], list[str]]:
        """
        Apply link policy to agent URIs.
        Returns (selected_uris, suppressed_uris).
        """
        allowed = self.filter_allowed(uris)
        if not allowed:
            return [], []

        if self.link_policy == "all":
            # Return all unique allowed
            unique_selected: list[str] = []
            for u in allowed:
                if u not in unique_selected:
                    unique_selected.append(u)
            return unique_selected, []

        # priority_fallback policy:
        # $0 (authorities: KANTO, ISNI, VIAF, etc.) and $1 (entities: WIKIDATA) are orthogonal domains.
        # Priority fallback evaluates within $0 authorities, while $1 entity links operate complementarily.
        uris_subfield_0: list[str] = []
        uris_subfield_1: list[str] = []
        for u in allowed:
            v = detect_vocabulary(u) or "OTHER"
            if v == "WIKIDATA" or "wikidata.org" in u.lower():
                uris_subfield_1.append(u)
            else:
                uris_subfield_0.append(u)

        selected_uris: list[str] = []
        suppressed_uris: list[str] = []

        # 1. Process $0 authorities with agent_priority fallback
        if uris_subfield_0:
            by_vocab_0: dict[str, list[str]] = {}
            for u in uris_subfield_0:
                v = detect_vocabulary(u) or "OTHER"
                by_vocab_0.setdefault(v, []).append(u)

            chosen_vocab_0: Optional[str] = None
            for p_vocab in self.agent_priority:
                if p_vocab != "WIKIDATA" and p_vocab in by_vocab_0:
                    chosen_vocab_0 = p_vocab
                    break

            if chosen_vocab_0:
                selected_uris.extend(by_vocab_0[chosen_vocab_0])
                for v, u_list in by_vocab_0.items():
                    if v != chosen_vocab_0:
                        suppressed_uris.extend(u_list)
            else:
                first_key = next(iter(by_vocab_0))
                selected_uris.extend(by_vocab_0[first_key])
                for v, u_list in by_vocab_0.items():
                    if v != first_key:
                        suppressed_uris.extend(u_list)

        # 2. Process $1 entities (Wikidata QIDs)
        for u in uris_subfield_1:
            if u not in selected_uris:
                selected_uris.append(u)

        return selected_uris, suppressed_uris
