"""Subject folding, shared by every loader.

A subject heading is only useful for matching once it has been reduced to a
*vocabulary* and a *concept id*: "Egypti" from one catalogue and "Egyptin
historia" from another are the same subject when both resolve to `yso:p105710`,
and nothing else about the string tells you so.

The MARC path did this inline and the BibFrame path did not do it at all - the
decomposer read no `bf:subject` whatsoever, so a BIBFRAME work reached the
datastore with no subjects and its agents with an empty subject footprint. This
module is that logic in one place, taking subjects as already-extracted
mappings, because extraction is format-specific and folding is not.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Optional

from reconciliator.vocabularies import (
    extract_concept_id,
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
        """The stored form.

        `code` and `scheme` are the names `find_classification_evidence` reads
        out of `works.subjects_json`. That reader was written against this shape
        while both loaders stored a bare list of heading strings, so it matched
        nothing and failed silently inside its own try/except.
        """
        return {
            "heading": self.heading,
            # `pref_label` is what work_serialization.j2 renders; the same string
            # under two names because the template speaks the vocabulary's
            # language and the evidence reader speaks the record's.
            "pref_label": self.heading,
            "field": self.field,
            "vocabulary": self.vocabulary,
            "uri": self.uri,
            "code": self.concept_id,
            "scheme": self.vocabulary,
        }


#: Finto URI shapes that name their vocabulary in a path segment:
#: `http://www.yso.fi/onto/<vocab>/<id>` and
#: `http://urn.fi/URN:NBN:fi:au:<vocab>:<id>`.
_FINTO_ONTO_RE = re.compile(r"/onto/([a-z0-9-]+)/", re.IGNORECASE)
_FINTO_URN_RE = re.compile(r"urn:nbn:fi:au:([a-z0-9-]+):", re.IGNORECASE)


def vocabulary_hint_from_uri(uri: Optional[str]) -> Optional[str]:
    """The vocabulary a Finto URI names in its own path.

    `detect_vocabulary` recognises only the vocabularies in the URI pattern
    table, and that table lists the *modern* ones - there is no pattern for ysa,
    allars, musa or kaunokki, because a record referring to those declares them
    in MARC `$2`. A BIBFRAME subject has no `$2`: it is a URI and nothing else,
    so a legacy heading arrived with no vocabulary at all and was dropped from
    the footprint. Reading the segment lets `legacy_to_modern` do its job on
    either format.
    """
    if not uri:
        return None
    for pattern in (_FINTO_ONTO_RE, _FINTO_URN_RE):
        match = pattern.search(uri)
        if match:
            return match.group(1).lower()
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

    A heading with no supported vocabulary is kept, without a key: it still
    belongs in the record, it just cannot be compared. A supported vocabulary
    with no id in the record is resolved against `vocab_store` by label, which
    is how a heading catalogued without a `$0` still reaches its concept.
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
    """The `vocab:concept` histogram stamped on every agent in the record.

    Per record, not per agent: the evidence is that this person was named on a
    record about these subjects, and MARC does not say which contributor a
    subject belongs to.
    """
    return dict(Counter(key for s in subjects if (key := s.key)))
