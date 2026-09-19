"""Subject heading extraction, normalization, and footprint folding."""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Optional

from reconciliator.vocab.detection import extract_concept_id
from reconciliator.vocab.manager import (
    normalize_subject_vocabulary,
    resolve_subject_concept_id,
)


@dataclass(frozen=True)
class FoldedSubject:
    """One subject heading, reduced to what can be compared across catalogues."""

    heading: str
    field: Optional[str] = None
    vocabulary: Optional[str] = None
    uri: Optional[str] = None
    concept_id: Optional[str] = None

    @property
    def key(self) -> Optional[str]:
        """`vocab:concept_id`, the form the agent footprint counts.

        None when the heading could not be resolved: an unresolved heading is
        not evidence of anything, and counting the raw string would make two
        catalogues' spellings look like two different subjects.
        """
        if not self.vocabulary or not self.concept_id:
            return None
        return f"{self.vocabulary}:{self.concept_id.lower()}"

    def as_dict(self) -> dict[str, Optional[str]]:
        """The stored form."""
        return {
            "heading": self.heading,
            "pref_label": self.heading,
            "field": self.field,
            "vocabulary": self.vocabulary,
            "uri": self.uri,
            "code": self.concept_id,
            "scheme": self.vocabulary,
        }


def vocabulary_hint_from_uri(uri: Optional[str]) -> Optional[str]:
    """The vocabulary a URI names in its own path or CURIE prefix."""
    if not uri:
        return None
    from reconciliator.utils.curie import parse_curie

    src, _ = parse_curie(uri)
    if src and src != "unknown":
        return src.lower()

    onto_m = re.search(r"/(?:onto|vocabulary|vocab|concept)/([a-z0-9-]+)/", uri, re.IGNORECASE)
    if onto_m:
        return onto_m.group(1).lower()
    urn_m = re.search(r"urn:[a-z0-9-]+(?::[a-z0-9-]+)*:([a-z0-9-]+):", uri, re.IGNORECASE)
    if urn_m:
        return urn_m.group(1).lower()
    return None


def fold_subjects(
    raw_subjects: Iterable[Mapping[str, Any]],
    *,
    supported_vocabularies: Optional[Iterable[str]] = None,
    legacy_to_modern: Optional[dict[str, str]] = None,
    vocab_store: Optional[Any] = None,
    language: Optional[str] = None,
) -> list[FoldedSubject]:
    """Fold extracted subjects to vocabulary + concept id.

    Each input carries whatever the format provided: `heading`, and optionally
    `vocabulary` (MARC $2, or a BIBFRAME source node's label), `uri` (MARC $0/$1,
    or the subject node's own URI) and `field` (the MARC tag, or the tag the
    BibFrame profile maps the subject type to).
    """
    folded: list[FoldedSubject] = []
    seen: set[tuple[str, Optional[str], Optional[str]]] = set()

    for raw in raw_subjects:
        heading = (raw.get("heading") or "").strip()
        if not heading:
            continue
        declared_vocab = raw.get("vocabulary")
        field = raw.get("field")
        uris = [u for u in (raw.get("uri"), *(raw.get("uris") or ())) if u]

        vocabulary: Optional[str] = None
        matched_uri: Optional[str] = None
        for uri in uris:
            candidate = normalize_subject_vocabulary(
                declared_vocab or vocabulary_hint_from_uri(uri),
                uri=uri,
                supported_vocabularies=supported_vocabularies,
                legacy_to_modern=legacy_to_modern,
            )
            if candidate:
                vocabulary = candidate
                matched_uri = uri
                break
        if not vocabulary and declared_vocab:
            vocabulary = normalize_subject_vocabulary(
                declared_vocab,
                uri=None,
                supported_vocabularies=supported_vocabularies,
                legacy_to_modern=legacy_to_modern,
            )

        concept_id: Optional[str] = None
        if vocabulary:
            concept_id = extract_concept_id(matched_uri or (uris[0] if uris else None))
            if not concept_id:
                concept_id = resolve_subject_concept_id(
                    heading,
                    vocab=vocabulary,
                    vocab_store=vocab_store,
                    language=language,
                )

        subject = FoldedSubject(
            heading=heading,
            field=str(field) if field else None,
            vocabulary=vocabulary,
            uri=matched_uri or (uris[0] if uris else None),
            concept_id=concept_id,
        )
        dedup_key = (subject.heading, subject.vocabulary, subject.field)
        if dedup_key in seen:
            continue
        seen.add(dedup_key)
        folded.append(subject)

    return folded


def subject_footprint(subjects: Iterable[FoldedSubject]) -> dict[str, int]:
    """The `vocab:concept` histogram stamped on every agent in the record."""
    return dict(Counter(key for s in subjects if (key := s.key)))

