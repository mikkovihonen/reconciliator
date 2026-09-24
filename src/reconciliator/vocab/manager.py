"""Authority vocabulary management, allowlists, and link prioritization policies."""

from __future__ import annotations

from typing import Any, Iterable, Literal, Optional

from reconciliator.config import get_bundled_vocabularies_defaults
from reconciliator.vocab.detection import (
    concept_id_to_uri,
    detect_vocabulary,
    extract_concept_id,
    format_wikidata_uri,
    normalize_concept_id,
    strip_vocabulary_subcode,
)


def resolve_subject_concept_id(
    heading: str,
    vocab: str,
    vocab_store: Optional[Any] = None,
    language: Optional[str] = None,
) -> Optional[str]:
    """Resolve a subject heading label to its canonical concept identifier via vocab_store."""
    if not heading or vocab_store is None:
        return None
    clean = heading.strip()
    if not clean:
        return None

    # 1. Preferred label lookup across configured preferred languages
    if hasattr(vocab_store, "find_concept_by_label"):
        langs_to_try: list[Optional[str]] = [None]
        if language and language.lower() not in ("und", "unknown"):
            langs_to_try.append(language.lower())
        store_cfg = getattr(vocab_store, "config", None)
        pref_langs = getattr(
            store_cfg, "preferred_label_languages", None
        ) or get_bundled_vocabularies_defaults().get("preferred_label_languages", [])
        for pl in pref_langs:
            if pl and pl.lower() not in langs_to_try:
                langs_to_try.append(pl.lower())

        for l_try in langs_to_try:
            try:
                c = vocab_store.find_concept_by_label(clean, vocabulary=vocab, lang=l_try)
            except TypeError:
                c = vocab_store.find_concept_by_label(clean, vocabulary=vocab)
            if c and getattr(c, "uri", None):
                return extract_concept_id(c.uri)

    # 2. Normalized alias lookup (multilingual, alternate labels, folded forms)
    if hasattr(vocab_store, "find_concept_by_alias"):
        c, _ = vocab_store.find_concept_by_alias(clean, vocabulary=vocab)
        if c and getattr(c, "uri", None):
            return extract_concept_id(c.uri)

    # 3. Legacy thesauri mapping (e.g. YSA/Allärs/Kaunokki/MUSA label -> modern URI)
    if hasattr(vocab_store, "find_legacy_mapping_by_label"):
        m = vocab_store.find_legacy_mapping_by_label(clean, legacy_vocab=vocab)
        if m and getattr(m, "target_uri", None):
            return extract_concept_id(m.target_uri)

    return None


def normalize_subject_vocabulary(
    vocab: Optional[str],
    uri: Optional[str] = None,
    supported_vocabularies: Optional[Iterable[str]] = None,
    legacy_to_modern: Optional[dict[str, str]] = None,
    vocabulary_patterns: Optional[dict[str, list[str]]] = None,
) -> Optional[str]:
    """Normalize and validate subject vocabulary against configured supported ontologies.

    Loads supported vocabularies and legacy mappings from YAML configuration defaults if not explicitly provided.
    Returns canonical lowercase vocabulary code (e.g. 'yso', 'slm', 'wikidata') if supported, or None if unsupported.
    """
    defaults: dict[str, Any] = {}
    if supported_vocabularies is None or legacy_to_modern is None:
        defaults = get_bundled_vocabularies_defaults()

    supp_set: set[str]
    if supported_vocabularies is not None:
        supp_set = {v.strip().lower() for v in supported_vocabularies if v}
    else:
        supp_set = {
            v.strip().lower() for v in defaults.get("supported_subject_vocabularies", []) if v
        }

    leg_map: dict[str, str]
    if legacy_to_modern is not None:
        leg_map = {k.strip().lower(): v.strip().lower() for k, v in legacy_to_modern.items()}
    else:
        leg_map = {
            k.strip().lower(): v.strip().lower()
            for k, v in defaults.get("legacy_to_modern", {}).items()
        }

    raw_v = (vocab or "").strip().lower()

    # If vocab missing, try detecting from URI
    if not raw_v and uri:
        detected = detect_vocabulary(uri, patterns=vocabulary_patterns)
        if detected:
            raw_v = detected.lower()

    if not raw_v:
        return None

    # Strip language subcodes (e.g. yso/fin -> yso, slm/swe -> slm)
    base_v = strip_vocabulary_subcode(raw_v)

    # Map legacy thesauri if configured
    canonical_v = leg_map.get(base_v, base_v)

    if canonical_v in supp_set:
        return canonical_v

    return None


class VocabularyManager:
    """Manages vocabulary allowlists and link selection prioritization policies."""

    def __init__(
        self,
        allowed_vocabularies: Optional[list[str]] = None,
        agent_priority: Optional[list[str]] = None,
        subject_priority: Optional[list[str]] = None,
        link_policy: Literal["priority_fallback", "all"] = "priority_fallback",
        vocabulary_patterns: Optional[dict[str, list[str]]] = None,
        concept_id_rules: Optional[dict[str, dict[str, Any]]] = None,
        entity_vocabularies: Optional[list[str] | set[str]] = None,
        supported_subject_vocabularies: Optional[Iterable[str]] = None,
        legacy_to_modern: Optional[dict[str, str]] = None,
    ) -> None:
        defaults: dict[str, Any] = {}
        if (
            allowed_vocabularies is None
            or agent_priority is None
            or subject_priority is None
            or vocabulary_patterns is None
            or concept_id_rules is None
            or entity_vocabularies is None
            or supported_subject_vocabularies is None
            or legacy_to_modern is None
        ):
            defaults = get_bundled_vocabularies_defaults()

        raw_allowed = (
            allowed_vocabularies
            if allowed_vocabularies is not None
            else defaults.get("vocabularies", [])
        )
        self.allowed_vocabularies = {v.upper() for v in raw_allowed}

        raw_agent = (
            agent_priority if agent_priority is not None else defaults.get("agent_priority", [])
        )
        self.agent_priority = [v.upper() for v in raw_agent]

        raw_subj = (
            subject_priority
            if subject_priority is not None
            else defaults.get("subject_priority", [])
        )
        self.subject_priority = [v.upper() for v in raw_subj]

        self.link_policy = link_policy

        raw_patterns = (
            vocabulary_patterns
            if vocabulary_patterns is not None
            else defaults.get("vocabulary_patterns", {})
        )
        self.vocabulary_patterns = {
            k.upper(): [p.lower() for p in pats] for k, pats in raw_patterns.items()
        }

        raw_entities = (
            entity_vocabularies
            if entity_vocabularies is not None
            else defaults.get("entity_vocabularies", [])
        )
        self.entity_vocabularies = {v.upper() for v in raw_entities}

        raw_concept_rules = (
            concept_id_rules
            if concept_id_rules is not None
            else defaults.get("concept_id_rules", {})
        )
        self.concept_id_rules = {k.upper(): dict(v) for k, v in raw_concept_rules.items()}

        if supported_subject_vocabularies is not None:
            self.supported_subject_vocabularies = {
                v.strip().lower() for v in supported_subject_vocabularies if v
            }
        else:
            self.supported_subject_vocabularies = {
                v.strip().lower() for v in defaults.get("supported_subject_vocabularies", []) if v
            }

        if legacy_to_modern is not None:
            self.legacy_to_modern = {
                k.strip().lower(): v.strip().lower() for k, v in legacy_to_modern.items()
            }
        else:
            self.legacy_to_modern = {
                k.strip().lower(): v.strip().lower()
                for k, v in defaults.get("legacy_to_modern", {}).items()
            }

    def normalize_concept_id(self, identifier: Optional[str]) -> Optional[str]:
        """Normalize concept identifier using instance-configured rules and patterns."""
        return normalize_concept_id(
            identifier, rules=self.concept_id_rules, patterns=self.vocabulary_patterns
        )

    def concept_id_to_uri(self, concept_id: Optional[str]) -> Optional[str]:
        """Expand concept identifier to URI using instance-configured rules."""
        return concept_id_to_uri(concept_id, rules=self.concept_id_rules)

    def format_wikidata_uri(self, qid: Optional[str]) -> Optional[str]:
        """Format a Wikidata entity URI using instance-configured rules."""
        return format_wikidata_uri(qid, rules=self.concept_id_rules)

    def is_allowed(self, uri: str) -> bool:
        """Check if URI belongs to an allowed vocabulary."""
        vocab = detect_vocabulary(uri, patterns=self.vocabulary_patterns)
        if not vocab:
            return False
        v_upper = vocab.upper()
        if v_upper in self.allowed_vocabularies:
            return True
        for allowed in self.allowed_vocabularies:
            if v_upper.startswith(f"{allowed}_") or allowed.startswith(f"{v_upper}_"):
                return True
        return False

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

        uris_subfield_0: list[str] = []
        uris_subfield_1: list[str] = []
        for u in allowed:
            v = detect_vocabulary(u, patterns=self.vocabulary_patterns) or "OTHER"
            if v in self.entity_vocabularies or any(
                ent.lower() in u.lower() for ent in self.entity_vocabularies
            ):
                uris_subfield_1.append(u)
            else:
                uris_subfield_0.append(u)

        selected_uris: list[str] = []
        suppressed_uris: list[str] = []

        # 1. Process $0 authorities with agent_priority fallback
        if uris_subfield_0:
            by_vocab_0: dict[str, list[str]] = {}
            for u in uris_subfield_0:
                v = detect_vocabulary(u, patterns=self.vocabulary_patterns) or "OTHER"
                by_vocab_0.setdefault(v, []).append(u)

            chosen_vocab_0: Optional[str] = None
            for p_vocab in self.agent_priority:
                if p_vocab in self.entity_vocabularies:
                    continue
                matched_v = next(
                    (
                        v
                        for v in by_vocab_0
                        if v.upper() == p_vocab
                        or v.upper().startswith(f"{p_vocab}_")
                        or p_vocab.startswith(f"{v.upper()}_")
                    ),
                    None,
                )
                if matched_v:
                    chosen_vocab_0 = matched_v
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

    def select_subject_uris(self, uris: list[str]) -> tuple[list[str], list[str]]:
        """
        Apply link policy to subject URIs using subject_priority.
        Returns (selected_uris, suppressed_uris).
        """
        allowed = self.filter_allowed(uris)
        if not allowed:
            return [], []

        if self.link_policy == "all":
            unique_selected: list[str] = []
            for u in allowed:
                if u not in unique_selected:
                    unique_selected.append(u)
            return unique_selected, []

        uris_subfield_0: list[str] = []
        uris_subfield_1: list[str] = []
        for u in allowed:
            v = detect_vocabulary(u, patterns=self.vocabulary_patterns) or "OTHER"
            if v in self.entity_vocabularies or any(
                ent.lower() in u.lower() for ent in self.entity_vocabularies
            ):
                uris_subfield_1.append(u)
            else:
                uris_subfield_0.append(u)

        selected_uris: list[str] = []
        suppressed_uris: list[str] = []

        # 1. Process $0 subjects with subject_priority fallback
        if uris_subfield_0:
            by_vocab_0: dict[str, list[str]] = {}
            for u in uris_subfield_0:
                v = detect_vocabulary(u, patterns=self.vocabulary_patterns) or "OTHER"
                by_vocab_0.setdefault(v, []).append(u)

            chosen_vocab_0: Optional[str] = None
            for p_vocab in self.subject_priority:
                if p_vocab not in self.entity_vocabularies and p_vocab in by_vocab_0:
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
