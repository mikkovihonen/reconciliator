"""Vocabulary namespace detection, concept ID regex extraction, normalization, and URI templates."""

from __future__ import annotations

import re
from typing import Any, Optional

from reconciliator.config import get_bundled_vocabularies_defaults


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
        for k, pats in get_bundled_vocabularies_defaults().get("vocabulary_patterns", {}).items()
    }


def get_concept_id_rules() -> dict[str, dict[str, Any]]:
    """Retrieve concept ID normalization rules dynamically from YAML configuration defaults."""
    return {
        k.upper(): dict(v)
        for k, v in get_bundled_vocabularies_defaults().get("concept_id_rules", {}).items()
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
    candidates: list[tuple[str, str]] = [
        (pat, vocab)
        for vocab, pats in active_patterns.items()
        for pat in pats
    ]
    candidates.sort(key=lambda item: len(item[0]), reverse=True)
    for pat, vocab in candidates:
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
    template: Optional[str] = None,
) -> Optional[str]:
    """Expand internal canonical 'vocab:id' to external canonical URI via configured uri_template."""
    if not concept_id:
        return None
    raw = str(concept_id).strip()
    if not raw:
        return None

    if raw.startswith("http://") or raw.startswith("https://"):
        return raw.rstrip("/")

    if template:
        _, _, cid = raw.partition(":") if ":" in raw else ("", "", raw)
        tmpl = template.replace("{identifier}", "{id}")
        return tmpl.format(id=cid.strip())

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
