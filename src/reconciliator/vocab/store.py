"""SQLAlchemy 2.0 PostgreSQL store for controlled vocabularies, ontologies, and migration mappings."""

from __future__ import annotations

import functools
import logging
from pathlib import Path
from typing import Any, Optional, Sequence

from sqlalchemy import (
    Engine,
    and_,
    delete,
    func,
    insert,
    select,
    text,
    tuple_,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert

from reconciliator.config import VocabulariesConfig
from reconciliator.utils.curie import extract_wikidata_qid, parse_curie, to_curie
from reconciliator.database import (
    concept_aliases_table,
    concept_hierarchy_table,
    concept_labels_table,
    concept_mapping_labels_table,
    concept_mappings_table,
    concepts_table,
    get_engine,
    relator_terms_table,
)
from reconciliator.database import (
    metadata as vocab_metadata,
)
from reconciliator.models import ConceptMapping, VocabConcept
from reconciliator.normalizers.linguistic import fold_label
from reconciliator.vocab.detection import (
    concept_id_to_uri,
    detect_vocabulary,
    extract_concept_id,
    normalize_concept_id,
)

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


def init_vocab_db(engine: Any) -> None:
    """Initialize concepts, concept_labels, concept_mappings, relator_terms, concept_aliases, and concept_hierarchy tables."""
    from reconciliator.database import create_schema_tables

    create_schema_tables(engine, vocab_metadata.sorted_tables)


class VocabStore:
    """Store for controlled vocabularies backed by SQLAlchemy 2.0 Engine."""

    def __init__(
        self,
        db_path: Path | str | None = None,
        config: Optional[VocabulariesConfig] = None,
        engine: Optional[Engine] = None,
    ) -> None:
        self.config = config or VocabulariesConfig()
        if engine is not None:
            self.engine = engine
            self.db_path = engine.url.render_as_string(hide_password=False)
        else:
            self.engine = get_engine(db_path)
            self.db_path = self.engine.url.render_as_string(hide_password=False)
        init_vocab_db(self.engine)

    def get_connection(self) -> Any:
        """Get a configured connection context manager backed by the SQLAlchemy Engine."""
        return self.engine.begin()

    def close(self) -> None:
        """Dispose SQLAlchemy engine and release connection pool."""
        self.engine.dispose()

    def clear_caches(self) -> None:
        """Clear all in-memory LRU caches."""
        if hasattr(self.find_concept_by_id, "cache_clear"):
            self.find_concept_by_id.cache_clear()
        if hasattr(self.find_concept_by_uri, "cache_clear"):
            self.find_concept_by_uri.cache_clear()
        if hasattr(self.find_concept_by_label, "cache_clear"):
            self.find_concept_by_label.cache_clear()
        if hasattr(self.find_legacy_mapping, "cache_clear"):
            self.find_legacy_mapping.cache_clear()
        if hasattr(self.find_legacy_mapping_by_uri, "cache_clear"):
            self.find_legacy_mapping_by_uri.cache_clear()
        if hasattr(self.find_legacy_mapping_by_label, "cache_clear"):
            self.find_legacy_mapping_by_label.cache_clear()
        if hasattr(self.is_known_relator, "cache_clear"):
            self.is_known_relator.cache_clear()
        if hasattr(self.get_relator_code, "cache_clear"):
            self.get_relator_code.cache_clear()
        if hasattr(self.find_concept_by_alias, "cache_clear"):
            self.find_concept_by_alias.cache_clear()
        if hasattr(self.get_broader_concepts, "cache_clear"):
            self.get_broader_concepts.cache_clear()
        if hasattr(self.get_narrower_concepts, "cache_clear"):
            self.get_narrower_concepts.cache_clear()
        if hasattr(self.load_hierarchy_edges, "cache_clear"):
            self.load_hierarchy_edges.cache_clear()

    def insert_concepts_batch(self, concepts: list[VocabConcept]) -> None:
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

        batch_size = 5000

        with self.engine.begin() as conn:
            for i in range(0, len(concept_rows), batch_size):
                chunk = concept_rows[i : i + batch_size]
                stmt = pg_insert(concepts_table).values(chunk)
                stmt = stmt.on_conflict_do_update(
                    index_elements=["vocabulary", "concept_id"],
                    set_={
                        "is_deprecated": stmt.excluded.is_deprecated,
                        "target_field": stmt.excluded.target_field,
                    },
                )
                conn.execute(stmt)

            keys = [(c.vocabulary, c.concept_id) for c in concepts]
            row_id_map: dict[tuple[str, str], int] = {}
            for chunk_start in range(0, len(keys), 500):
                chunk_keys = keys[chunk_start : chunk_start + 500]
                q = select(
                    concepts_table.c.id, concepts_table.c.vocabulary, concepts_table.c.concept_id
                ).where(
                    tuple_(concepts_table.c.vocabulary, concepts_table.c.concept_id).in_(chunk_keys)
                )
                for r_id, voc, cid in conn.execute(q).fetchall():
                    row_id_map[(voc, cid)] = r_id

            row_ids = list(row_id_map.values())
            if row_ids:
                for chunk_start in range(0, len(row_ids), 1000):
                    chunk_rids = row_ids[chunk_start : chunk_start + 1000]
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
                for i in range(0, len(label_rows), batch_size):
                    conn.execute(insert(concept_labels_table), label_rows[i : i + batch_size])

        self.clear_caches()

    insert_concepts = insert_concepts_batch

    def insert_mappings(self, mappings: list[ConceptMapping]) -> None:
        """Bulk insert legacy mappings into concept_mappings and concept_mapping_labels."""
        if not mappings:
            return
        with self.engine.begin() as conn:
            for m in mappings:
                res = conn.execute(
                    insert(concept_mappings_table).values(
                        legacy_vocab=m.legacy_vocab,
                        legacy_id=m.legacy_id,
                        target_vocab=m.target_vocab,
                        target_id=m.target_id,
                        mapping_type=m.mapping_type,
                        target_field=m.target_field,
                    )
                )
                m_id = res.inserted_primary_key[0] if res.inserted_primary_key else None
                label_rows = []
                for lang, lbl in m.legacy_labels.items():
                    if lbl and lbl.strip():
                        label_rows.append({"mapping_id": m_id, "lang": lang, "label": lbl.strip()})
                if label_rows and m_id is not None:
                    conn.execute(insert(concept_mapping_labels_table), label_rows)

        self.clear_caches()

    def insert_hierarchy_batch(self, edges: list[dict[str, Any]]) -> None:
        """Bulk insert concept hierarchy edges (skos:broader / skos:narrower).

        Each edge dict must provide:
          - vocabulary: str
          - concept_id: str (the narrower concept)
          - broader_concept_id: str (the broader concept)
          - relation_type: Optional[str] (default 'broader')
        """
        if not edges:
            return

        # Deduplicate edges by (vocabulary, concept_id, broader_concept_id)
        seen: set[tuple[str, str, str]] = set()
        clean_rows: list[dict[str, Any]] = []
        for e in edges:
            voc = str(e["vocabulary"]).strip().lower()
            cid = str(e["concept_id"]).strip()
            pcid = str(e["broader_concept_id"]).strip()
            if not voc or not cid or not pcid or cid == pcid:
                continue
            key = (voc, cid, pcid)
            if key not in seen:
                seen.add(key)
                clean_rows.append(
                    {
                        "vocabulary": voc,
                        "concept_id": cid,
                        "broader_concept_id": pcid,
                        "relation_type": str(e.get("relation_type", "broader")),
                    }
                )

        if not clean_rows:
            return

        batch_size = 5000
        with self.engine.begin() as conn:
            for i in range(0, len(clean_rows), batch_size):
                chunk = clean_rows[i : i + batch_size]
                stmt = pg_insert(concept_hierarchy_table).values(chunk)
                stmt = stmt.on_conflict_do_nothing(
                    index_elements=["vocabulary", "concept_id", "broader_concept_id"]
                )
                conn.execute(stmt)

        self.clear_caches()

    @functools.lru_cache(maxsize=32768)
    def get_broader_concepts(
        self, vocabulary: str, concept_id: str, max_depth: Optional[int] = None
    ) -> list[tuple[str, int]]:
        """Query broader concepts (ancestors) in a hierarchy using recursive CTE.

        Returns list of (broader_concept_id, depth) sorted by shortest distance.
        """
        if not vocabulary or not concept_id:
            return []
        clean_voc = str(vocabulary).strip().lower()
        clean_id = str(concept_id).strip()
        depth_limit = max_depth or self.config.hierarchy.max_traversal_depth

        query = text(
            """
            WITH RECURSIVE broader_tree(concept_id, broader_concept_id, depth) AS (
                SELECT concept_id, broader_concept_id, 1
                FROM concept_hierarchy
                WHERE vocabulary = :vocab AND concept_id = :cid
              UNION
                SELECT bt.concept_id, h.broader_concept_id, bt.depth + 1
                FROM concept_hierarchy h
                JOIN broader_tree bt ON h.concept_id = bt.broader_concept_id
                WHERE h.vocabulary = :vocab AND bt.depth < :max_depth
            )
            SELECT broader_concept_id, min(depth) as min_depth
            FROM broader_tree
            GROUP BY broader_concept_id
            ORDER BY min_depth ASC;
            """
        )

        with self.engine.connect() as conn:
            rows = conn.execute(
                query, {"vocab": clean_voc, "cid": clean_id, "max_depth": depth_limit}
            ).fetchall()
            return [(str(r[0]), int(r[1])) for r in rows]

    @functools.lru_cache(maxsize=32768)
    def get_narrower_concepts(
        self, vocabulary: str, concept_id: str, max_depth: Optional[int] = None
    ) -> list[tuple[str, int]]:
        """Query narrower concepts (descendants) in a hierarchy using recursive CTE.

        Returns list of (narrower_concept_id, depth) sorted by shortest distance.
        """
        if not vocabulary or not concept_id:
            return []
        clean_voc = str(vocabulary).strip().lower()
        clean_id = str(concept_id).strip()
        depth_limit = max_depth or self.config.hierarchy.max_traversal_depth

        query = text(
            """
            WITH RECURSIVE narrower_tree(narrower_concept_id, broader_concept_id, depth) AS (
                SELECT concept_id, broader_concept_id, 1
                FROM concept_hierarchy
                WHERE vocabulary = :vocab AND broader_concept_id = :cid
              UNION
                SELECT h.concept_id, nt.broader_concept_id, nt.depth + 1
                FROM concept_hierarchy h
                JOIN narrower_tree nt ON h.broader_concept_id = nt.narrower_concept_id
                WHERE h.vocabulary = :vocab AND nt.depth < :max_depth
            )
            SELECT narrower_concept_id, min(depth) as min_depth
            FROM narrower_tree
            GROUP BY narrower_concept_id
            ORDER BY min_depth ASC;
            """
        )

        with self.engine.connect() as conn:
            rows = conn.execute(
                query, {"vocab": clean_voc, "cid": clean_id, "max_depth": depth_limit}
            ).fetchall()
            return [(str(r[0]), int(r[1])) for r in rows]

    @functools.lru_cache(maxsize=32)
    def load_hierarchy_edges(self, vocabulary: str) -> list[tuple[str, str]]:
        """Load all (concept_id, broader_concept_id) edges for a vocabulary into memory."""
        if not vocabulary:
            return []
        clean_voc = str(vocabulary).strip().lower()
        with self.engine.connect() as conn:
            stmt = select(
                concept_hierarchy_table.c.concept_id,
                concept_hierarchy_table.c.broader_concept_id,
            ).where(concept_hierarchy_table.c.vocabulary == clean_voc)
            rows = conn.execute(stmt).fetchall()
            return [(str(r[0]), str(r[1])) for r in rows]

    @functools.lru_cache(maxsize=32768)
    def find_concept_by_id(self, vocabulary: str, concept_id: str) -> Optional[VocabConcept]:
        """Look up concept by (vocabulary, concept_id)."""
        if not vocabulary or not concept_id:
            return None
        clean_voc = str(vocabulary).strip().lower()
        clean_id = str(concept_id).strip()
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
    def find_concept_by_uri(self, uri: str) -> Optional[VocabConcept]:
        """Look up concept by URI or canonical concept identifier (e.g. yso:p2591, (FI-YSO)p2591)."""
        if not uri:
            return None
        clean_uri = uri.strip()

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
        self, label: str, vocabulary: Optional[str] = None, lang: Optional[str] = None
    ) -> Optional[VocabConcept]:
        """Look up concept by preferred label."""
        effective_lang = lang or getattr(self.config, "default_language", None) or ""
        clean_lbl = label.strip().lower()
        norm_l = self.config.language_code_map.get(effective_lang.lower(), effective_lang.lower())
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

    @functools.lru_cache(maxsize=16384)
    def find_legacy_mapping(self, legacy_vocab: str, legacy_id: str) -> Optional[ConceptMapping]:
        """Look up migration mapping by (legacy_vocab, legacy_id)."""
        if not legacy_vocab or not legacy_id:
            return None
        clean_voc = str(legacy_vocab).strip().lower()
        clean_id = str(legacy_id).strip()
        with self.engine.connect() as conn:
            stmt = (
                select(concept_mappings_table)
                .where(
                    concept_mappings_table.c.legacy_vocab == clean_voc,
                    concept_mappings_table.c.legacy_id == clean_id,
                )
                .limit(1)
            )
            row = conn.execute(stmt).mappings().first()
            if not row:
                return None
            mapping_id = row["id"]
            lbl_stmt = select(
                concept_mapping_labels_table.c.lang,
                concept_mapping_labels_table.c.label,
            ).where(concept_mapping_labels_table.c.mapping_id == mapping_id)
            lbl_rows = conn.execute(lbl_stmt).mappings().all()
            legacy_labels = {r["lang"]: r["label"] for r in lbl_rows}
            return ConceptMapping(
                legacy_vocab=row["legacy_vocab"],
                legacy_id=row["legacy_id"],
                legacy_labels=legacy_labels,
                target_vocab=row["target_vocab"],
                target_id=row["target_id"],
                mapping_type=row["mapping_type"],
                target_field=row["target_field"],
            )

    @functools.lru_cache(maxsize=16384)
    def find_legacy_mapping_by_uri(self, uri: str) -> Optional[ConceptMapping]:
        """Look up migration mapping by legacy URI or canonical concept identifier."""
        if not uri:
            return None
        clean_uri = uri.strip()

        # 1. Direct CURIE or URI decomposition
        src, sid = parse_curie(clean_uri)
        if src != "unknown" and sid and not sid.startswith(("http://", "https://")):
            m = self.find_legacy_mapping(src, sid)
            if m:
                return m

        # 2. Canonical expansion rule
        rules = getattr(self.config, "concept_id_rules", None)
        norm_id = normalize_concept_id(clean_uri, rules=rules)
        if norm_id:
            n_src, n_sid = parse_curie(norm_id)
            if n_src != "unknown" and n_sid:
                m = self.find_legacy_mapping(n_src, n_sid)
                if m:
                    return m

        # 3. Detect vocab + extract ID fallback
        voc = detect_vocabulary(clean_uri)
        cid = extract_concept_id(clean_uri)
        if voc and cid:
            m = self.find_legacy_mapping(voc, cid)
            if m:
                return m

        # 4. Direct match on legacy_id (for full URIs stored as legacy_id)
        with self.engine.connect() as conn:
            stmt = (
                select(concept_mappings_table)
                .where(concept_mappings_table.c.legacy_id == clean_uri)
                .limit(1)
            )
            row = conn.execute(stmt).mappings().first()
            if row:
                return self.find_legacy_mapping(row["legacy_vocab"], row["legacy_id"])

        return None

    @functools.lru_cache(maxsize=16384)
    def find_legacy_mapping_by_label(
        self, label: str, legacy_vocab: Optional[str] = None, lang: Optional[str] = None
    ) -> Optional[ConceptMapping]:
        """Look up migration mapping by legacy label."""
        effective_lang = lang or getattr(self.config, "default_language", None) or ""
        clean_lbl = label.strip().lower()
        norm_l = self.config.language_code_map.get(effective_lang.lower(), effective_lang.lower())
        with self.engine.connect() as conn:
            stmt = (
                select(concept_mappings_table.c.id)
                .select_from(
                    concept_mapping_labels_table.join(
                        concept_mappings_table,
                        concept_mapping_labels_table.c.mapping_id == concept_mappings_table.c.id,
                    )
                )
                .where(
                    concept_mapping_labels_table.c.lang == norm_l,
                    func.lower(concept_mapping_labels_table.c.label) == clean_lbl,
                )
                .limit(1)
            )

            if legacy_vocab:
                stmt = stmt.where(concept_mappings_table.c.legacy_vocab == legacy_vocab)

            row = conn.execute(stmt).first()
            if not row:
                return None
            mapping_id = row[0]
            m_stmt = select(concept_mappings_table).where(concept_mappings_table.c.id == mapping_id)
            m_row = conn.execute(m_stmt).mappings().first()
            if not m_row:
                return None

            lbl_stmt = select(
                concept_mapping_labels_table.c.lang,
                concept_mapping_labels_table.c.label,
            ).where(concept_mapping_labels_table.c.mapping_id == mapping_id)
            lbl_rows = conn.execute(lbl_stmt).mappings().all()
            legacy_labels = {r["lang"]: r["label"] for r in lbl_rows}
            return ConceptMapping(
                legacy_vocab=m_row["legacy_vocab"],
                legacy_id=m_row["legacy_id"],
                legacy_labels=legacy_labels,
                target_vocab=m_row["target_vocab"],
                target_id=m_row["target_id"],
                mapping_type=m_row["mapping_type"],
                target_field=m_row["target_field"],
            )

    def insert_relators(self, terms: Sequence[dict[str, Any] | tuple[Any, ...] | str]) -> None:
        """Bulk insert or replace relator terms in relator_terms."""
        if not terms:
            return
        rows: list[dict[str, Any]] = []
        default_lang = getattr(self.config, "default_language", "und")
        for t in terms:
            if isinstance(t, dict):
                raw = str(t.get("raw_term", ""))
                folded = str(t.get("folded_term") or fold_label(raw))
                lang = str(t.get("lang", default_lang))
                code = t.get("code")
                uri = t.get("uri")
            elif isinstance(t, (tuple, list)):
                raw = str(t[0])
                folded = fold_label(raw)
                lang = str(t[1]) if len(t) > 1 else default_lang
                code = t[2] if len(t) > 2 else None
                uri = t[3] if len(t) > 3 else None
            else:
                raw = str(t)
                folded = fold_label(raw)
                lang = default_lang
                code = None
                uri = None
            rows.append(
                {
                    "folded_term": folded,
                    "raw_term": raw,
                    "lang": lang,
                    "code": code,
                    "uri": uri,
                }
            )

        seen: dict[str, dict[str, Any]] = {}
        for r in rows:
            seen[r["folded_term"]] = r
        unique_rows = list(seen.values())

        with self.engine.begin() as conn:
            batch_size = 5000
            for i in range(0, len(unique_rows), batch_size):
                chunk = unique_rows[i : i + batch_size]
                stmt = pg_insert(relator_terms_table).values(chunk)
                stmt = stmt.on_conflict_do_update(
                    index_elements=["folded_term"],
                    set_={
                        "raw_term": stmt.excluded.raw_term,
                        "lang": stmt.excluded.lang,
                        "code": stmt.excluded.code,
                        "uri": stmt.excluded.uri,
                    },
                )
                conn.execute(stmt)

        self.clear_caches()

    @functools.lru_cache(maxsize=8192)
    def is_known_relator(self, folded_term: str) -> bool:
        """Check whether a folded role term exists in relator_terms."""
        with self.engine.connect() as conn:
            stmt = (
                select(1)
                .where(relator_terms_table.c.folded_term == folded_term.strip().casefold())
                .limit(1)
            )
            row = conn.execute(stmt).first()
            return bool(row)

    @functools.lru_cache(maxsize=8192)
    def get_relator_code(self, folded_term: str) -> Optional[str]:
        """Look up standard MARC code for a folded role term in relator_terms."""
        with self.engine.connect() as conn:
            stmt = (
                select(relator_terms_table.c.code)
                .where(
                    relator_terms_table.c.folded_term == folded_term.strip().casefold(),
                    relator_terms_table.c.code.isnot(None),
                )
                .limit(1)
            )
            row = conn.execute(stmt).first()
            return str(row[0]) if row and row[0] else None

    def insert_aliases(self, aliases: list[dict[str, Any]]) -> None:
        """Bulk insert alias records into concept_aliases."""
        if not aliases:
            return
        rows = []
        for a in aliases:
            voc = a.get("vocabulary", "")
            c_id = a.get("concept_id")
            if not c_id:
                raw_val = a.get("concept_curie") or a.get("concept_uri") or ""
                s, sid = parse_curie(str(raw_val), default_source=voc)
                if not voc and s and s != "unknown":
                    voc = s
                c_id = sid or str(raw_val)
            t_field = (
                a.get("target_field")
                or self.config.vocab_target_fields.get(voc)
                or self.config.default_vocab_target_field
            )
            rows.append(
                {
                    "folded_label": a["folded_label"],
                    "raw_label": a["raw_label"],
                    "lang": a["lang"],
                    "concept_id": c_id,
                    "vocabulary": voc,
                    "target_field": t_field,
                    "is_preferred": a.get("is_preferred", 1),
                    "source": a.get("source", "vocab"),
                }
            )
        with self.engine.begin() as conn:
            batch_size = 5000
            for i in range(0, len(rows), batch_size):
                conn.execute(insert(concept_aliases_table), rows[i : i + batch_size])
        self.clear_caches()

    @functools.lru_cache(maxsize=65536)
    def find_concept_by_alias(
        self,
        folded_label: str,
        target_field: Optional[str] = None,
        vocabulary: Optional[str] = None,
    ) -> tuple[Optional[VocabConcept], Optional[str]]:
        """Look up concept by normalized alias (accepts either pre-folded or raw label)."""
        clean = folded_label.strip()
        if not clean:
            return None, None

        clean_folded = (
            fold_label(
                clean,
                preserved_diacritics=self.config.preserved_diacritics,
            )
            or clean.lower()
        )

        search_tokens = [clean_folded]
        if clean != clean_folded:
            search_tokens.append(clean)

        with self.engine.connect() as conn:
            conditions: list[Any] = [concept_aliases_table.c.folded_label.in_(search_tokens)]
            if target_field:
                conditions.append(concept_aliases_table.c.target_field == target_field)
            if vocabulary:
                conditions.append(concept_aliases_table.c.vocabulary == vocabulary)

            stmt = (
                select(
                    concept_aliases_table.c.vocabulary,
                    concept_aliases_table.c.concept_id,
                    concept_aliases_table.c.source,
                )
                .where(and_(*conditions))
                .order_by(
                    concept_aliases_table.c.is_preferred.desc(),
                    concept_aliases_table.c.id.asc(),
                )
                .limit(1)
            )

            row = conn.execute(stmt).first()
            if not row and target_field:
                fallback_stmt = (
                    select(
                        concept_aliases_table.c.vocabulary,
                        concept_aliases_table.c.concept_id,
                        concept_aliases_table.c.source,
                    )
                    .where(concept_aliases_table.c.folded_label.in_(search_tokens))
                    .order_by(
                        concept_aliases_table.c.is_preferred.desc(),
                        concept_aliases_table.c.id.asc(),
                    )
                    .limit(1)
                )
                row = conn.execute(fallback_stmt).first()

            if not row or not row[1]:
                return None, None

            voc = str(row[0])
            c_id = str(row[1])
            source_val = str(row[2]) if len(row) > 2 and row[2] else ""
            concept = self.find_concept_by_id(voc, c_id)
            concept_curie = to_curie(voc, c_id)
            if not concept:
                concept = self.find_concept_by_uri(concept_curie)
            wikidata_qid = extract_wikidata_qid(concept_curie) or extract_wikidata_qid(source_val)
            return concept, wikidata_qid

    def resolve_concept_alias(
        self, label: str, vocabulary: Optional[str] = None, target_field: Optional[str] = None
    ) -> Optional[dict[str, Any]]:
        """Resolve a label to concept_id, vocabulary, and wikidata_qid via alias lookup."""
        concept, qid = self.find_concept_by_alias(
            label, target_field=target_field, vocabulary=vocabulary
        )
        if concept:
            return {
                "concept_id": concept.concept_id,
                "vocabulary": concept.vocabulary,
                "wikidata_qid": qid,
            }
        return None

    def find_concepts_by_uris(self, uris: Sequence[str]) -> dict[str, VocabConcept]:
        """Look up multiple concepts by URI or concept ID in batch."""
        results: dict[str, VocabConcept] = {}
        for u in uris:
            if not u:
                continue
            concept = self.find_concept_by_uri(u)
            if concept:
                results[u] = concept
        return results

    def rebuild_aliases_from_concepts(self, legacy_vocabs: Optional[set[str]] = None) -> int:
        """Rebuild concept_aliases table from concepts and concept_mappings."""
        aliases_to_insert: list[dict[str, Any]] = []
        active_legacy_vocabs = (
            legacy_vocabs if legacy_vocabs is not None else set(self.config.legacy_to_modern.keys())
        )

        with self.engine.begin() as conn:
            conn.execute(
                delete(concept_aliases_table).where(
                    concept_aliases_table.c.source.in_(["vocab", "concept_mapping"])
                )
            )

            # 1. From concepts and concept_labels
            stmt = (
                select(
                    concepts_table.c.concept_id,
                    concepts_table.c.vocabulary,
                    concepts_table.c.target_field,
                    concept_labels_table.c.lang,
                    concept_labels_table.c.label_type,
                    concept_labels_table.c.label,
                )
                .select_from(
                    concepts_table.join(
                        concept_labels_table,
                        concepts_table.c.id == concept_labels_table.c.concept_row_id,
                    )
                )
                .where(concepts_table.c.is_deprecated == False)  # noqa: E712
            )

            rows = conn.execute(stmt).mappings().all()
            for r in rows:
                c_vocab = r["vocabulary"]
                if c_vocab in active_legacy_vocabs:
                    continue
                c_id = r["concept_id"]
                t_field = (
                    r["target_field"]
                    or self.config.vocab_target_fields.get(c_vocab)
                    or self.config.default_vocab_target_field
                )
                lang = r["lang"]
                label_type = r["label_type"]
                raw = r["label"]
                if raw and raw.strip():
                    folded = fold_label(raw)
                    if folded:
                        aliases_to_insert.append(
                            {
                                "folded_label": folded,
                                "raw_label": raw.strip(),
                                "lang": lang,
                                "concept_id": c_id,
                                "vocabulary": c_vocab,
                                "target_field": t_field,
                                "is_preferred": 1 if label_type == "pref" else 0,
                                "source": "vocab",
                            }
                        )

            # 2. From concept_mappings and concept_mapping_labels
            m_stmt = select(
                concept_mappings_table.c.target_id,
                concept_mappings_table.c.target_vocab,
                concept_mappings_table.c.target_field,
                concept_mapping_labels_table.c.lang,
                concept_mapping_labels_table.c.label,
            ).select_from(
                concept_mappings_table.join(
                    concept_mapping_labels_table,
                    concept_mappings_table.c.id == concept_mapping_labels_table.c.mapping_id,
                )
            )
            mapping_rows = conn.execute(m_stmt).mappings().all()
            for m in mapping_rows:
                t_id = m["target_id"]
                t_vocab = m["target_vocab"]
                t_field = (
                    m["target_field"]
                    or self.config.vocab_target_fields.get(t_vocab)
                    or self.config.default_vocab_target_field
                )
                lang = m["lang"]
                raw = m["label"]
                if raw and raw.strip():
                    folded = fold_label(raw)
                    if folded:
                        aliases_to_insert.append(
                            {
                                "folded_label": folded,
                                "raw_label": raw.strip(),
                                "lang": lang,
                                "concept_id": t_id,
                                "vocabulary": t_vocab,
                                "target_field": t_field,
                                "is_preferred": 0,
                                "source": "concept_mapping",
                            }
                        )

        self.insert_aliases(aliases_to_insert)
        return len(aliases_to_insert)

    def build_concept_index(
        self,
        embedder: Any,
        concept_indexer: Any,
        batch_size: int = 128,
    ) -> int:
        """Embed active concepts into dedicated concept vector index (concept.index)."""
        with self.engine.connect() as conn:
            stmt = (
                select(
                    concepts_table.c.id,
                    concepts_table.c.vocabulary,
                    concepts_table.c.concept_id,
                )
                .where(concepts_table.c.is_deprecated == False)  # noqa: E712
                .order_by(concepts_table.c.id.asc())
            )

            concepts_data = conn.execute(stmt).fetchall()
            if not concepts_data:
                return 0

            lbl_stmt = select(
                concept_labels_table.c.concept_row_id,
                concept_labels_table.c.label,
            )
            lbl_res = conn.execute(lbl_stmt).fetchall()
            labels_by_row_id: dict[int, list[str]] = {}
            for r in lbl_res:
                r_id = r[0]
                lbl = r[1]
                if r_id not in labels_by_row_id:
                    labels_by_row_id[r_id] = []
                labels_by_row_id[r_id].append(lbl)

            total_indexed = 0
            for i in range(0, len(concepts_data), batch_size):
                batch = concepts_data[i : i + batch_size]
                ids: list[int] = []
                texts: list[str] = []
                for c in batch:
                    c_id = c[0]
                    vocab = c[1]
                    cid = c[2]
                    labels = labels_by_row_id.get(c_id, [])
                    labels_str = " | ".join(labels) if labels else cid
                    text = f"{vocab}:{cid} {labels_str}"
                    ids.append(c_id)
                    texts.append(text)

                if texts:
                    vectors = embedder.embed_texts(texts, batch_size=len(texts))
                    concept_indexer.add_vectors(vectors, ids=ids)
                    total_indexed += len(texts)

            concept_indexer.save()
            return total_indexed

    def resolve_subject_semantic(
        self,
        raw_label: str,
        embedder: Any,
        concept_indexer: Any,
        vocabulary: Optional[str] = None,
        threshold: float = 0.85,
    ) -> Optional[tuple[str, str, str]]:
        """Resolve raw subject label to (vocabulary, concept_id, concept_uri) using semantic vector similarity."""
        if not raw_label or not raw_label.strip():
            return None
        clean = raw_label.strip()

        # 1. Exact alias lookup
        alias_res = self.resolve_concept_alias(clean, vocabulary=vocabulary)
        if alias_res and alias_res.get("concept_id"):
            cid = alias_res["concept_id"]
            voc = alias_res.get("vocabulary") or vocabulary or "unknown"
            concept = self.find_concept_by_id(voc, cid)
            uri = concept.uri if concept else to_curie(voc, cid)
            return (voc, cid, uri)

        # 2. Vector ANN search fallback
        if not concept_indexer or not embedder:
            return None

        vectors = embedder.embed_texts([clean])
        if vectors is None or len(vectors) == 0:
            return None

        scores, indices = concept_indexer.search(vectors[0], top_k=5)
        if len(indices) == 0 or len(scores) == 0:
            return None

        top_score = float(scores[0])
        top_id = int(indices[0])
        if top_score < threshold:
            return None

        with self.engine.connect() as conn:
            stmt = select(
                concepts_table.c.vocabulary,
                concepts_table.c.concept_id,
            ).where(concepts_table.c.id == top_id)
            row = conn.execute(stmt).first()
            if row:
                voc, cid = str(row[0]), str(row[1])
                uri = concept_id_to_uri(to_curie(voc, cid)) or to_curie(voc, cid)
                return (voc, cid, uri)

        return None

