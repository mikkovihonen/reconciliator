"""Concept storage and retrieval mixin for VocabStore."""

from __future__ import annotations

import functools
import logging
from typing import TYPE_CHECKING, Any, Optional, Protocol, Sequence

from sqlalchemy import delete, func, insert, select, tuple_
from sqlalchemy.dialects.postgresql import insert as pg_insert

from reconciliator.database import (
    calculate_batch_size,
    concept_labels_table,
    concepts_table,
)
from reconciliator.models import VocabConcept
from reconciliator.utils.curie import parse_curie
from reconciliator.vocab.detection import (
    detect_vocabulary,
    extract_concept_id,
    normalize_concept_id,
)

if TYPE_CHECKING:
    from sqlalchemy import Engine

    from reconciliator.config import VocabulariesConfig
    from reconciliator.vocab.store import VocabMemoryIndex

logger = logging.getLogger(__name__)


def is_language_allowed(lang: str, allowed: Optional[Sequence[str] | str]) -> bool:
    """Check whether a language tag is allowed by an ingestion language filter."""
    if allowed is None:
        return True
    norm_lang = lang.strip().lower()
    if isinstance(allowed, str):
        norm_s = allowed.strip().lower()
        if norm_s in ("*", "all", ""):
            return True
        return norm_lang == norm_s
    norm_set = {str(x).strip().lower() for x in allowed}
    if "*" in norm_set or "all" in norm_set:
        return True
    return norm_lang in norm_set


class ConceptStoreProtocol(Protocol):
    """Protocol for concept store dependencies."""

    engine: Engine
    config: VocabulariesConfig
    _index: VocabMemoryIndex

    def clear_caches(self) -> None: ...
    def find_concept_by_id(self, vocabulary: str, concept_id: str) -> Optional[VocabConcept]: ...
    def find_concept_by_uri(self, uri: str) -> Optional[VocabConcept]: ...


class ConceptStoreMixin:
    """Manages concept entities, multilingual labels, and concept lookups by ID, URI, or label."""

    def insert_concepts_batch(self: ConceptStoreProtocol, concepts: list[VocabConcept]) -> None:
        """Bulk insert concepts and their multilingual labels with upsert support."""
        if not concepts:
            return

        deduped: dict[tuple[str, str], VocabConcept] = {}
        for c in concepts:
            key = (c.vocabulary.strip().lower(), c.concept_id.strip())
            if key in deduped:
                existing = deduped[key]
                for lang, lbl in c.pref_labels.items():
                    if lbl and (lang not in existing.pref_labels or not existing.pref_labels[lang]):
                        existing.pref_labels[lang] = lbl
                for lang, alts in c.alt_labels.items():
                    if lang not in existing.alt_labels:
                        existing.alt_labels[lang] = []
                    for alt in alts:
                        if alt not in existing.alt_labels[lang]:
                            existing.alt_labels[lang].append(alt)
                if c.is_deprecated:
                    existing.is_deprecated = True
                if c.target_field and not existing.target_field:
                    existing.target_field = c.target_field
            else:
                deduped[key] = VocabConcept(
                    uri=c.uri,
                    vocabulary=c.vocabulary.strip().lower(),
                    concept_id=c.concept_id.strip(),
                    pref_labels=dict(c.pref_labels),
                    alt_labels={k: list(v) for k, v in c.alt_labels.items()},
                    is_deprecated=bool(c.is_deprecated),
                    target_field=c.target_field,
                )

        concepts = list(deduped.values())

        concept_rows = [
            {
                "vocabulary": c.vocabulary,
                "concept_id": c.concept_id,
                "is_deprecated": bool(c.is_deprecated),
                "target_field": c.target_field,
            }
            for c in concepts
        ]

        concept_batch_size = calculate_batch_size(num_columns=len(concepts_table.c))
        is_postgres = self.engine.dialect.name == "postgresql"

        with self.engine.begin() as conn:
            row_id_map: dict[tuple[str, str], int] = {}
            for i in range(0, len(concept_rows), concept_batch_size):
                chunk = concept_rows[i : i + concept_batch_size]
                stmt = pg_insert(concepts_table).values(chunk)
                stmt = stmt.on_conflict_do_update(
                    index_elements=["vocabulary", "concept_id"],
                    set_={
                        "is_deprecated": stmt.excluded.is_deprecated,
                        "target_field": stmt.excluded.target_field,
                    },
                )
                if is_postgres:
                    stmt = stmt.returning(
                        concepts_table.c.id,
                        concepts_table.c.vocabulary,
                        concepts_table.c.concept_id,
                    )
                    res = conn.execute(stmt)
                    for r_id, voc, cid in res.fetchall():
                        row_id_map[(voc, cid)] = r_id
                else:
                    conn.execute(stmt)

            # If any keys missing (e.g. non-postgres dialect), fallback query
            missing_keys = [
                (c.vocabulary, c.concept_id)
                for c in concepts
                if (c.vocabulary, c.concept_id) not in row_id_map
            ]
            if missing_keys:
                key_batch_size = calculate_batch_size(num_columns=2)
                for chunk_start in range(0, len(missing_keys), key_batch_size):
                    chunk_keys = missing_keys[chunk_start : chunk_start + key_batch_size]
                    q = select(
                        concepts_table.c.id,
                        concepts_table.c.vocabulary,
                        concepts_table.c.concept_id,
                    ).where(
                        tuple_(concepts_table.c.vocabulary, concepts_table.c.concept_id).in_(
                            chunk_keys
                        )
                    )
                    for r_id, voc, cid in conn.execute(q).fetchall():
                        row_id_map[(voc, cid)] = r_id

            row_ids = list(row_id_map.values())
            if row_ids:
                delete_batch_size = calculate_batch_size(num_columns=1)
                for chunk_start in range(0, len(row_ids), delete_batch_size):
                    chunk_rids = row_ids[chunk_start : chunk_start + delete_batch_size]
                    conn.execute(
                        delete(concept_labels_table).where(
                            concept_labels_table.c.concept_row_id.in_(chunk_rids)
                        )
                    )

            label_rows: list[dict[str, Any]] = []
            for c in concepts:
                c_row_id = row_id_map.get((c.vocabulary, c.concept_id))
                if not c_row_id:
                    continue
                for lang, lbl in c.pref_labels.items():
                    if lbl and lbl.strip():
                        if is_language_allowed(lang, self.config.ingest_pref_label_languages):
                            label_rows.append(
                                {
                                    "concept_row_id": c_row_id,
                                    "lang": lang,
                                    "label_type": "pref",
                                    "label": lbl.strip(),
                                }
                            )
                for lang, alts in c.alt_labels.items():
                    if is_language_allowed(lang, self.config.ingest_alt_label_languages):
                        for alt in alts:
                            if alt and str(alt).strip():
                                label_rows.append(
                                    {
                                        "concept_row_id": c_row_id,
                                        "lang": lang,
                                        "label_type": "alt",
                                        "label": str(alt).strip(),
                                    }
                                )

            if label_rows:
                label_batch_size = calculate_batch_size(num_columns=len(concept_labels_table.c))
                for i in range(0, len(label_rows), label_batch_size):
                    conn.execute(insert(concept_labels_table), label_rows[i : i + label_batch_size])

        self.clear_caches()

    insert_concepts = insert_concepts_batch

    @functools.lru_cache(maxsize=32768)
    def find_concept_by_id(
        self: ConceptStoreProtocol, vocabulary: str, concept_id: str
    ) -> Optional[VocabConcept]:
        """Look up concept by (vocabulary, concept_id)."""
        if not vocabulary or not concept_id:
            return None
        clean_voc = str(vocabulary).strip().lower()
        clean_id = str(concept_id).strip()
        if self._index.is_preloaded:
            key = (clean_voc, clean_id)
            if key in self._index.concepts_by_id:
                return self._index.concepts_by_id[key]

        with self.engine.connect() as conn:
            stmt = select(concepts_table).where(
                concepts_table.c.vocabulary == clean_voc,
                concepts_table.c.concept_id == clean_id,
            )
            row = conn.execute(stmt).mappings().first()
            if not row:
                return None
            row_id = row["id"]
            labels_stmt = select(
                concept_labels_table.c.lang,
                concept_labels_table.c.label_type,
                concept_labels_table.c.label,
            ).where(concept_labels_table.c.concept_row_id == row_id)
            labels_rows = conn.execute(labels_stmt).mappings().all()

            pref_labels: dict[str, str] = {}
            alt_labels: dict[str, list[str]] = {}
            for l_row in labels_rows:
                l_lang = l_row["lang"]
                l_type = l_row["label_type"]
                l_val = l_row["label"]
                if l_type == "pref":
                    pref_labels[l_lang] = l_val
                elif l_type == "alt":
                    alt_labels.setdefault(l_lang, []).append(l_val)

            return VocabConcept(
                vocabulary=row["vocabulary"],
                concept_id=row["concept_id"],
                pref_labels=pref_labels,
                alt_labels=alt_labels,
                is_deprecated=bool(row["is_deprecated"]),
                target_field=row["target_field"],
            )

    @functools.lru_cache(maxsize=32768)
    def find_concept_by_uri(self: ConceptStoreProtocol, uri: str) -> Optional[VocabConcept]:
        """Look up concept by URI or canonical concept identifier (e.g. yso:p2591, (FI-YSO)p2591)."""
        if not uri:
            return None
        clean_uri = uri.strip()
        if self._index.is_preloaded and clean_uri in self._index.concepts_by_uri:
            return self._index.concepts_by_uri[clean_uri]

        # 1. Direct CURIE or URI decomposition
        src, sid = parse_curie(clean_uri)
        if src != "unknown" and sid and not sid.startswith(("http://", "https://")):
            c = self.find_concept_by_id(src, sid)
            if c:
                return c

        # 2. Canonical expansion rule
        rules = getattr(self.config, "concept_id_rules", None)
        norm_id = normalize_concept_id(clean_uri, rules=rules)
        if norm_id:
            n_src, n_sid = parse_curie(norm_id)
            if n_src != "unknown" and n_sid:
                c = self.find_concept_by_id(n_src, n_sid)
                if c:
                    return c

        # 3. Detect vocab + extract ID fallback
        voc = detect_vocabulary(clean_uri)
        cid = extract_concept_id(clean_uri)
        if voc and cid:
            c = self.find_concept_by_id(voc, cid)
            if c:
                return c

        # 4. Direct match on concept_id (for full URIs stored as concept_id)
        with self.engine.connect() as conn:
            stmt = select(concepts_table).where(concepts_table.c.concept_id == clean_uri).limit(1)
            row = conn.execute(stmt).mappings().first()
            if row:
                return self.find_concept_by_id(row["vocabulary"], row["concept_id"])

        return None

    @functools.lru_cache(maxsize=32768)
    def find_concept_by_label(
        self: ConceptStoreProtocol,
        label: str,
        vocabulary: Optional[str] = None,
        lang: Optional[str] = None,
    ) -> Optional[VocabConcept]:
        """Look up concept by preferred label."""
        effective_lang = lang or getattr(self.config, "default_language", None) or ""
        clean_lbl = label.strip().lower()
        norm_l = self.config.language_code_map.get(effective_lang.lower(), effective_lang.lower())
        if self._index.is_preloaded:
            clean_voc = str(vocabulary).strip().lower() if vocabulary else None
            hit = self._index.pref_labels.get((clean_lbl, clean_voc, norm_l))
            if not hit and not lang:
                hit = self._index.pref_labels.get((clean_lbl, clean_voc, None))
            if not hit and not vocabulary:
                hit = self._index.pref_labels.get(
                    (clean_lbl, None, norm_l)
                ) or self._index.pref_labels.get((clean_lbl, None, None))
            if hit:
                return self.find_concept_by_id(hit[0], hit[1])
            return None

        with self.engine.connect() as conn:
            stmt = (
                select(concepts_table.c.vocabulary, concepts_table.c.concept_id)
                .select_from(
                    concept_labels_table.join(
                        concepts_table,
                        concept_labels_table.c.concept_row_id == concepts_table.c.id,
                    )
                )
                .where(
                    concept_labels_table.c.lang == norm_l,
                    concept_labels_table.c.label_type == "pref",
                    func.lower(concept_labels_table.c.label) == clean_lbl,
                )
                .order_by(concepts_table.c.is_deprecated.asc())
                .limit(1)
            )

            if vocabulary:
                stmt = stmt.where(concepts_table.c.vocabulary == vocabulary)

            row = conn.execute(stmt).first()
            if not row:
                return None
            return self.find_concept_by_id(row[0], row[1])

    def find_concepts_by_uris(
        self: ConceptStoreProtocol, uris: Sequence[str]
    ) -> dict[str, VocabConcept]:
        """Look up multiple concepts by URI or concept ID in batch."""
        results: dict[str, VocabConcept] = {}
        for u in uris:
            if not u:
                continue
            concept = self.find_concept_by_uri(u)
            if concept:
                results[u] = concept
        return results
