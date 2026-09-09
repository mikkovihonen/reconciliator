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
        self.agent_priority = [v.upper() for v in (agent_priority or ["KANTO", "ISNI", "VIAF", "WIKIDATA"])]
        self.subject_priority = [v.upper() for v in (subject_priority or ["YSO", "KAUNO", "MUSO", "SLM", "SEKO"])]
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

        # priority_fallback policy
        # Group by detected vocabulary
        by_vocab: dict[str, list[str]] = {}
        for u in allowed:
            v = detect_vocabulary(u) or "OTHER"
            by_vocab.setdefault(v, []).append(u)

        selected_uris: list[str] = []
        suppressed_uris: list[str] = []

        # Find the highest priority vocabulary that is present
        chosen_vocab: Optional[str] = None
        for p_vocab in self.agent_priority:
            if p_vocab in by_vocab:
                chosen_vocab = p_vocab
                break

        if chosen_vocab:
            selected_uris.extend(by_vocab[chosen_vocab])
            for v, u_list in by_vocab.items():
                if v != chosen_vocab:
                    suppressed_uris.extend(u_list)
        else:
            # None of the priority vocabularies matched; select first available
            first_key = next(iter(by_vocab))
            selected_uris.extend(by_vocab[first_key])
            for v, u_list in by_vocab.items():
                if v != first_key:
                    suppressed_uris.extend(u_list)

        return selected_uris, suppressed_uris

