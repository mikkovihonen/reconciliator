"""Authority vocabulary namespace detection, allowlist filtering, and link prioritization policies."""

from __future__ import annotations

import re
from typing import Any, Iterable, Literal, Optional

from reconciliator.config import get_bundled_vocab_defaults


def strip_vocabulary_subcode(vocab: Optional[str]) -> str:
    """Strip language or sub-scheme specifiers from a vocabulary identifier (e.g. 'yso/fin' -> 'yso')."""
    if not vocab:
        return ""
    clean = str(vocab).strip()
    if "/" in clean:
        return clean.split("/")[0].strip()
    return clean


def get_vocabulary_patterns() -> dict[str, list[str]]:
    """Retrieve vocabulary namespace patterns dynamically from YAML configuration defaults."""
    return {
        k.upper(): [p.lower() for p in pats]
        for k, pats in get_bundled_vocab_defaults().get("vocabulary_patterns", {}).items()
    }


def get_concept_id_rules() -> dict[str, dict[str, Any]]:
    """Retrieve concept ID normalization rules dynamically from YAML configuration defaults."""
    return {
        k.upper(): dict(v)
        for k, v in get_bundled_vocab_defaults().get("concept_id_rules", {}).items()
    }


def detect_vocabulary(uri: str, patterns: Optional[dict[str, list[str]]] = None) -> Optional[str]:
    """Detect canonical vocabulary code from URI namespace or identifier string."""
    if not uri:
        return None
    lower_uri = uri.lower().strip()
    active_patterns = (
        {k.upper(): [p.lower() for p in pats] for k, pats in patterns.items()}
        if patterns is not None
        else get_vocabulary_patterns()
    )
    for vocab, pats in active_patterns.items():
        for pat in pats:
            if pat in lower_uri:
                return vocab
    return None


def extract_concept_id(uri_or_id: Optional[str]) -> Optional[str]:
    """Extract canonical concept identifier from URI, URN, or control number string."""
    if not uri_or_id:
        return None
    raw = str(uri_or_id).strip()
    if not raw:
        return None

    # 1. Handle parenthetical prefix notation, e.g. (FI-YSO)p2591, (fi-asteri-n)00012345
    if raw.startswith("(") and ")" in raw:
        _, _, tail = raw.partition(")")
        clean_tail = tail.strip()
        if clean_tail:
            return clean_tail

    # 2. Fragment identifier (e.g. URI#id)
    if "#" in raw:
        frag = raw.split("#")[-1].strip()
        if frag:
            return frag

    # 3. URN notation (e.g. urn:nbn:fi:au:slm:s123, urn:nbn:fi:au:pn:00012345)
    if "urn:" in raw.lower():
        urn_tail = raw.rstrip("/").split(":")[-1].strip()
        if urn_tail:
            return urn_tail

    # 4. Standard URI path (e.g. http://www.yso.fi/onto/yso/p2591, http://viaf.org/viaf/12345)
    if "/" in raw:
        path_tail = raw.rstrip("/").split("/")[-1].strip()
        if path_tail:
            return path_tail

    # 5. Prefixed identifier notation (e.g. yso:p2591, slm:s123, wikidata:q11660)
    if ":" in raw:
        colon_tail = raw.rstrip(":").split(":")[-1].strip()
        if colon_tail:
            return colon_tail

    return raw


def normalize_concept_id(
    identifier: Optional[str],
    rules: Optional[dict[str, Any]] = None,
    patterns: Optional[dict[str, list[str]]] = None,
) -> Optional[str]:
    """Normalize any URI, control number, or prefixed string to internal canonical 'vocab:id' form.

    Examples:
      'http://www.yso.fi/onto/yso/p2591' -> 'yso:p2591'
      'https://yso.fi/onto/yso/p2591/'  -> 'yso:p2591'
      '(FI-YSO)p2591'                   -> 'yso:p2591'
      'yso:p2591'                       -> 'yso:p2591'
      'https://urn.fi/URN:NBN:fi:au:slm:s123' -> 'slm:s123'
      'http://www.wikidata.org/entity/Q42'    -> 'wikidata:Q42'
    """
    if not identifier:
        return None
    raw = str(identifier).strip()
    if not raw:
        return None

    active_rules = (
        {k.upper(): dict(v) for k, v in rules.items()}
        if rules is not None
        else get_concept_id_rules()
    )

    vocab = detect_vocabulary(raw, patterns=patterns)
    rule = active_rules.get(vocab.upper()) if vocab else None
    if not rule and ":" in raw and "://" not in raw:
        candidate_prefix, _, _ = raw.partition(":")
        cand = candidate_prefix.strip().upper()
        for k, r in active_rules.items():
            if k == cand or r.get("prefix", "").upper() == cand:
                rule = r
                vocab = k
                break

    local_id: Optional[str] = None
    if rule:
        id_patterns = rule.get("id_patterns")
        if id_patterns:
            if isinstance(id_patterns, str):
                id_patterns = [id_patterns]
            for pat in id_patterns:
                m = re.search(pat, raw)
                if m and m.groups():
                    local_id = m.group(1).strip()
                    break

    # Fallback to general concept id extraction if rule regex didn't match
    if not local_id:
        local_id = extract_concept_id(raw)

    if not local_id:
        return None

    # Case normalization
    case_mode = rule.get("canonical_id_case", "preserve") if rule else "preserve"
    if case_mode == "lower":
        local_id = local_id.lower()
    elif case_mode == "upper":
        local_id = local_id.upper()

    prefix = rule.get("prefix") if rule else (vocab.lower() if vocab else None)
    if prefix:
        return f"{prefix}:{local_id}"
    return local_id


def concept_id_to_uri(
    concept_id: Optional[str],
    rules: Optional[dict[str, Any]] = None,
) -> Optional[str]:
    """Expand internal canonical 'vocab:id' to external canonical URI via configured uri_template."""
    if not concept_id:
        return None
    raw = str(concept_id).strip()
    if not raw:
        return None

    # If it's already an absolute URI, return stripped form
    if raw.startswith("http://") or raw.startswith("https://") or raw.startswith("urn:"):
        return raw.rstrip("/")

    active_rules = (
        {k.upper(): dict(v) for k, v in rules.items()}
        if rules is not None
        else get_concept_id_rules()
    )

    vocab_key: Optional[str] = None
    cid: str = raw
    if ":" in raw:
        prefix, _, tail = raw.partition(":")
        clean_prefix = prefix.strip().upper()
        # Find matching rule by prefix or key
        for k, r in active_rules.items():
            if k == clean_prefix or r.get("prefix", "").upper() == clean_prefix:
                vocab_key = k
                cid = tail.strip()
                break

    if not vocab_key:
        vocab_key = detect_vocabulary(raw)

    if vocab_key and vocab_key in active_rules:
        tmpl = active_rules[vocab_key].get("uri_template")
        if tmpl and "{id}" in tmpl:
            return tmpl.format(id=cid)

    return raw


def format_wikidata_uri(
    qid: Optional[str],
    rules: Optional[dict[str, Any]] = None,
) -> Optional[str]:
    """Format a Wikidata entity URI from a QID or concept identifier using configured template.

    Accepts 'Q123', 'wikidata:Q123', 'http://www.wikidata.org/entity/Q123', etc.
    """
    if not qid:
        return None
    raw = str(qid).strip()
    if not raw:
        return None
    if raw.startswith("http://") or raw.startswith("https://"):
        return raw.rstrip("/")

    norm = normalize_concept_id(raw, rules=rules)
    if norm and norm.startswith("wikidata:"):
        return concept_id_to_uri(norm, rules=rules)

    clean_qid = raw.partition(":")[-1] if ":" in raw else raw
    return concept_id_to_uri(f"wikidata:{clean_qid.upper()}", rules=rules)


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
        ) or get_bundled_vocab_defaults().get("preferred_label_languages", [])
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
        defaults = get_bundled_vocab_defaults()

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
            defaults = get_bundled_vocab_defaults()

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
        return vocab.upper() in self.allowed_vocabularies

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
