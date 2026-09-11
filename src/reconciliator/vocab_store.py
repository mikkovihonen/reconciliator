"""Dedicated SQLite store for controlled vocabularies, ontologies, and migration mappings."""

from __future__ import annotations

import functools
import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Optional, Sequence

from reconciliator.db import get_db_connection
from reconciliator.models import ConceptMapping, VocabConcept
from reconciliator.normalizers.linguistic import fold_label


def init_vocab_db(conn: sqlite3.Connection) -> None:
    """Initialize concepts, concept_mappings, relator_terms, and concept_aliases tables in vocab.db."""
    with conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS concepts (
                uri TEXT PRIMARY KEY,
                vocabulary TEXT NOT NULL,
                pref_label_fi TEXT,
                pref_label_sv TEXT,
                pref_label_en TEXT,
                alt_labels_fi TEXT,
                alt_labels_sv TEXT,
                is_deprecated INTEGER DEFAULT 0,
                target_field TEXT
            );
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_concepts_fi ON concepts(vocabulary, lower(pref_label_fi));"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_concepts_sv ON concepts(vocabulary, lower(pref_label_sv));"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_concepts_en ON concepts(vocabulary, lower(pref_label_en));"
        )

        conn.execute("""
            CREATE TABLE IF NOT EXISTS concept_mappings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                legacy_uri TEXT,
                legacy_vocab TEXT NOT NULL,
                legacy_label_fi TEXT,
                legacy_label_sv TEXT,
                target_uri TEXT NOT NULL,
                target_vocab TEXT NOT NULL,
                mapping_type TEXT NOT NULL,
                target_field TEXT NOT NULL
            );
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_mappings_legacy_uri ON concept_mappings(legacy_uri);"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_mappings_legacy_lbl_fi ON concept_mappings(legacy_vocab, lower(legacy_label_fi));"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_mappings_legacy_lbl_sv ON concept_mappings(legacy_vocab, lower(legacy_label_sv));"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_mappings_target_uri ON concept_mappings(target_uri);"
        )

        conn.execute("""
            CREATE TABLE IF NOT EXISTS relator_terms (
                folded_term TEXT PRIMARY KEY,
                raw_term TEXT NOT NULL,
                lang TEXT NOT NULL,
                code TEXT,
                uri TEXT
            );
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_relators_lang ON relator_terms(lang);")

        conn.execute("""
            CREATE TABLE IF NOT EXISTS concept_aliases (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                folded_label TEXT NOT NULL,
                raw_label TEXT NOT NULL,
                lang TEXT NOT NULL,
                concept_uri TEXT NOT NULL,
                wikidata_qid TEXT,
                vocabulary TEXT NOT NULL,
                target_field TEXT NOT NULL,
                is_preferred INTEGER NOT NULL,
                source TEXT NOT NULL
            );
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_concept_aliases_lookup ON concept_aliases(folded_label, target_field);"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_concept_aliases_uri ON concept_aliases(concept_uri);"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_concept_aliases_qid ON concept_aliases(wikidata_qid);"
        )


class VocabStore:
    """Repository handling queries and bulk ingestion into vocab.db."""

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        with self.get_connection() as conn:
            init_vocab_db(conn)

    def get_connection(self) -> sqlite3.Connection:
        """Get or create a thread-local SQLite connection with WAL pragmas."""
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = get_db_connection(self.db_path)
            self._local.conn = conn
        return conn

    def close(self) -> None:
        """Close thread-local SQLite connection if open."""
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
            self._local.conn = None

    def clear_caches(self) -> None:
        """Clear all in-memory LRU caches."""
        self.find_concept_by_uri.cache_clear()
        self.find_concept_by_label.cache_clear()
        self.find_legacy_mapping_by_uri.cache_clear()
        self.find_legacy_mapping_by_label.cache_clear()
        self.find_concept_by_alias.cache_clear()
        self.is_known_relator.cache_clear()
        self.get_relator_code.cache_clear()

    def insert_concepts(self, concepts: list[VocabConcept]) -> None:
        """Bulk insert or replace concepts in concepts table."""
        if not concepts:
            return
        rows = [
            (
                c.uri,
                c.vocabulary,
                c.pref_label_fi,
                c.pref_label_sv,
                c.pref_label_en,
                json.dumps(c.alt_labels_fi, ensure_ascii=False),
                json.dumps(c.alt_labels_sv, ensure_ascii=False),
                1 if c.is_deprecated else 0,
                c.target_field,
            )
            for c in concepts
        ]
        with self.get_connection() as conn:
            conn.executemany(
                """
                INSERT INTO concepts (
                    uri, vocabulary, pref_label_fi, pref_label_sv, pref_label_en,
                    alt_labels_fi, alt_labels_sv, is_deprecated, target_field
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(uri) DO UPDATE SET
                    vocabulary = excluded.vocabulary,
                    pref_label_fi = excluded.pref_label_fi,
                    pref_label_sv = excluded.pref_label_sv,
                    pref_label_en = excluded.pref_label_en,
                    alt_labels_fi = excluded.alt_labels_fi,
                    alt_labels_sv = excluded.alt_labels_sv,
                    is_deprecated = excluded.is_deprecated,
                    target_field = excluded.target_field;
                """,
                rows,
            )
        self.clear_caches()

    def insert_mappings(self, mappings: list[ConceptMapping]) -> None:
        """Bulk insert legacy mappings into concept_mappings."""
        if not mappings:
            return
        rows = [
            (
                m.legacy_uri,
                m.legacy_vocab,
                m.legacy_label_fi,
                m.legacy_label_sv,
                m.target_uri,
                m.target_vocab,
                m.mapping_type,
                m.target_field,
            )
            for m in mappings
        ]
        with self.get_connection() as conn:
            conn.executemany(
                """
                INSERT INTO concept_mappings (
                    legacy_uri, legacy_vocab, legacy_label_fi, legacy_label_sv,
                    target_uri, target_vocab, mapping_type, target_field
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?);
                """,
                rows,
            )
        self.clear_caches()

    @functools.lru_cache(maxsize=32768)
    def find_concept_by_uri(self, uri: str) -> Optional[VocabConcept]:
        """Look up concept by URI."""
        with self.get_connection() as conn:
            row = conn.execute("SELECT * FROM concepts WHERE uri = ?;", (uri,)).fetchone()
            if not row:
                return None
            return VocabConcept(
                uri=row["uri"],
                vocabulary=row["vocabulary"],
                pref_label_fi=row["pref_label_fi"],
                pref_label_sv=row["pref_label_sv"],
                pref_label_en=row["pref_label_en"],
                alt_labels_fi=json.loads(row["alt_labels_fi"] or "[]"),
                alt_labels_sv=json.loads(row["alt_labels_sv"] or "[]"),
                is_deprecated=bool(row["is_deprecated"]),
                target_field=row["target_field"],
            )

    @functools.lru_cache(maxsize=32768)
    def find_concept_by_label(
        self, label: str, vocabulary: Optional[str] = None, lang: str = "fi"
    ) -> Optional[VocabConcept]:
        """Look up concept by preferred label."""
        clean_lbl = label.strip().lower()
        col = f"pref_label_{lang}" if lang in ("fi", "sv", "en") else "pref_label_fi"
        with self.get_connection() as conn:
            if vocabulary:
                query = f"SELECT * FROM concepts WHERE vocabulary = ? AND lower({col}) = ? LIMIT 1;"
                row = conn.execute(query, (vocabulary, clean_lbl)).fetchone()
            else:
                query = f"SELECT * FROM concepts WHERE lower({col}) = ? LIMIT 1;"
                row = conn.execute(query, (clean_lbl,)).fetchone()

            if not row:
                return None
            return VocabConcept(
                uri=row["uri"],
                vocabulary=row["vocabulary"],
                pref_label_fi=row["pref_label_fi"],
                pref_label_sv=row["pref_label_sv"],
                pref_label_en=row["pref_label_en"],
                alt_labels_fi=json.loads(row["alt_labels_fi"] or "[]"),
                alt_labels_sv=json.loads(row["alt_labels_sv"] or "[]"),
                is_deprecated=bool(row["is_deprecated"]),
                target_field=row["target_field"],
            )

    @functools.lru_cache(maxsize=16384)
    def find_legacy_mapping_by_uri(self, uri: str) -> Optional[ConceptMapping]:
        """Look up migration mapping by legacy URI."""
        with self.get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM concept_mappings WHERE legacy_uri = ? LIMIT 1;", (uri,)
            ).fetchone()
            if not row:
                return None
            return ConceptMapping(
                legacy_uri=row["legacy_uri"],
                legacy_vocab=row["legacy_vocab"],
                legacy_label_fi=row["legacy_label_fi"],
                legacy_label_sv=row["legacy_label_sv"],
                target_uri=row["target_uri"],
                target_vocab=row["target_vocab"],
                mapping_type=row["mapping_type"],
                target_field=row["target_field"],
            )

    @functools.lru_cache(maxsize=16384)
    def find_legacy_mapping_by_label(
        self, label: str, legacy_vocab: Optional[str] = None, lang: str = "fi"
    ) -> Optional[ConceptMapping]:
        """Look up migration mapping by legacy label."""
        clean_lbl = label.strip().lower()
        col = f"legacy_label_{lang}" if lang in ("fi", "sv") else "legacy_label_fi"
        with self.get_connection() as conn:
            if legacy_vocab:
                query = f"SELECT * FROM concept_mappings WHERE legacy_vocab = ? AND lower({col}) = ? LIMIT 1;"
                row = conn.execute(query, (legacy_vocab, clean_lbl)).fetchone()
            else:
                query = f"SELECT * FROM concept_mappings WHERE lower({col}) = ? LIMIT 1;"
                row = conn.execute(query, (clean_lbl,)).fetchone()

            if not row:
                return None
            return ConceptMapping(
                legacy_uri=row["legacy_uri"],
                legacy_vocab=row["legacy_vocab"],
                legacy_label_fi=row["legacy_label_fi"],
                legacy_label_sv=row["legacy_label_sv"],
                target_uri=row["target_uri"],
                target_vocab=row["target_vocab"],
                mapping_type=row["mapping_type"],
                target_field=row["target_field"],
            )

    def insert_relators(self, terms: Sequence[dict[str, Any] | tuple[Any, ...] | str]) -> None:
        """Bulk insert or replace relator terms in relator_terms."""
        if not terms:
            return
        rows: list[tuple[str, str, str, Optional[str], Optional[str]]] = []
        for t in terms:
            if isinstance(t, dict):
                raw = str(t.get("raw_term", ""))
                folded = str(t.get("folded_term") or fold_label(raw))
                lang = str(t.get("lang", "fi"))
                code = t.get("code")
                uri = t.get("uri")
            elif isinstance(t, (tuple, list)):
                raw = str(t[0])
                folded = fold_label(raw)
                lang = str(t[1]) if len(t) > 1 else "fi"
                code = t[2] if len(t) > 2 else None
                uri = t[3] if len(t) > 3 else None
            else:
                raw = str(t)
                folded = fold_label(raw)
                lang = "fi"
                code = None
                uri = None
            rows.append((folded, raw, lang, code, uri))
        with self.get_connection() as conn:
            conn.executemany(
                """
                INSERT INTO relator_terms (folded_term, raw_term, lang, code, uri)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(folded_term) DO UPDATE SET
                    raw_term = excluded.raw_term,
                    lang = excluded.lang,
                    code = excluded.code,
                    uri = excluded.uri;
                """,
                rows,
            )
        self.clear_caches()

    @functools.lru_cache(maxsize=8192)
    def is_known_relator(self, folded_term: str) -> bool:
        """Check whether a folded role term exists in relator_terms."""
        with self.get_connection() as conn:
            row = conn.execute(
                "SELECT 1 FROM relator_terms WHERE folded_term = ? LIMIT 1;",
                (folded_term.strip().casefold(),),
            ).fetchone()
            return bool(row)

    @functools.lru_cache(maxsize=8192)
    def get_relator_code(self, folded_term: str) -> Optional[str]:
        """Look up standard MARC code for a folded role term in relator_terms."""
        with self.get_connection() as conn:
            row = conn.execute(
                "SELECT code FROM relator_terms WHERE folded_term = ? AND code IS NOT NULL LIMIT 1;",
                (folded_term.strip().casefold(),),
            ).fetchone()
            return row["code"] if row and row["code"] else None

    def get_all_relators(self) -> set[str]:
        """Retrieve all known folded relator terms."""
        with self.get_connection() as conn:
            rows = conn.execute("SELECT folded_term FROM relator_terms;").fetchall()
            return {r["folded_term"] for r in rows}

    def insert_aliases(self, aliases: list[dict[str, Any]]) -> None:
        """Bulk insert alias records into concept_aliases."""
        if not aliases:
            return
        rows = [
            (
                a["folded_label"],
                a["raw_label"],
                a["lang"],
                a["concept_uri"],
                a.get("wikidata_qid"),
                a["vocabulary"],
                a.get("target_field", "650"),
                a.get("is_preferred", 1),
                a.get("source", "vocab"),
            )
            for a in aliases
        ]
        with self.get_connection() as conn:
            conn.executemany(
                """
                INSERT INTO concept_aliases (
                    folded_label, raw_label, lang, concept_uri, wikidata_qid,
                    vocabulary, target_field, is_preferred, source
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
                """,
                rows,
            )
        self.clear_caches()

    @functools.lru_cache(maxsize=65536)
    def find_concept_by_alias(
        self,
        folded_label: str,
        target_field: Optional[str] = None,
        vocabulary: Optional[str] = None,
    ) -> tuple[Optional[VocabConcept], Optional[str]]:
        """Look up concept by normalized alias.

        Returns (concept, wikidata_qid) or (None, None).
        """
        clean = folded_label.strip()
        if not clean:
            return None, None

        with self.get_connection() as conn:
            params: list[Any] = [clean]
            where_clauses = ["folded_label = ?"]
            if target_field:
                where_clauses.append("target_field = ?")
                params.append(target_field)
            if vocabulary:
                where_clauses.append("vocabulary = ?")
                params.append(vocabulary)

            query = f"""
                SELECT concept_uri, wikidata_qid
                FROM concept_aliases
                WHERE {" AND ".join(where_clauses)}
                ORDER BY is_preferred DESC, id ASC
                LIMIT 1;
            """
            row = conn.execute(query, params).fetchone()
            if not row and target_field:
                # Fallback: try without target_field filter
                fallback_query = """
                    SELECT concept_uri, wikidata_qid
                    FROM concept_aliases
                    WHERE folded_label = ?
                    ORDER BY is_preferred DESC, id ASC
                    LIMIT 1;
                """
                row = conn.execute(fallback_query, (clean,)).fetchone()

            if not row:
                return None, None

            concept_uri = row["concept_uri"]
            wikidata_qid = row["wikidata_qid"]
            concept = self.find_concept_by_uri(concept_uri)
            return concept, wikidata_qid

    def rebuild_aliases_from_concepts(self) -> int:
        """Rebuild concept_aliases table from concepts and concept_mappings."""
        aliases_to_insert: list[dict[str, Any]] = []

        with self.get_connection() as conn:
            # Clear existing vocabulary-derived aliases
            conn.execute(
                "DELETE FROM concept_aliases WHERE source IN ('vocab', 'concept_mapping');"
            )

            # 1. From concepts
            legacy_vocabs = {"ysa", "allars", "kaunokki", "bella", "musa", "cilla"}
            rows = conn.execute("SELECT * FROM concepts;").fetchall()
            for r in rows:
                c_uri = r["uri"]
                c_vocab = r["vocabulary"]
                if c_vocab in legacy_vocabs or r["is_deprecated"]:
                    continue
                t_field = r["target_field"] or "650"

                for col, lang in (
                    ("pref_label_fi", "fi"),
                    ("pref_label_sv", "sv"),
                    ("pref_label_en", "en"),
                ):
                    raw = r[col]
                    if raw and raw.strip():
                        folded = fold_label(raw)
                        if folded:
                            aliases_to_insert.append(
                                {
                                    "folded_label": folded,
                                    "raw_label": raw.strip(),
                                    "lang": lang,
                                    "concept_uri": c_uri,
                                    "wikidata_qid": None,
                                    "vocabulary": c_vocab,
                                    "target_field": t_field,
                                    "is_preferred": 1,
                                    "source": "vocab",
                                }
                            )

                for alt_col, lang in (("alt_labels_fi", "fi"), ("alt_labels_sv", "sv")):
                    raw_json = r[alt_col]
                    if raw_json:
                        try:
                            alts = json.loads(raw_json)
                            for alt in alts:
                                if alt and str(alt).strip():
                                    folded = fold_label(str(alt))
                                    if folded:
                                        aliases_to_insert.append(
                                            {
                                                "folded_label": folded,
                                                "raw_label": str(alt).strip(),
                                                "lang": lang,
                                                "concept_uri": c_uri,
                                                "wikidata_qid": None,
                                                "vocabulary": c_vocab,
                                                "target_field": t_field,
                                                "is_preferred": 0,
                                                "source": "vocab",
                                            }
                                        )
                        except Exception:
                            pass

            # 2. From concept_mappings
            mapping_rows = conn.execute("SELECT * FROM concept_mappings;").fetchall()
            for m in mapping_rows:
                t_uri = m["target_uri"]
                t_vocab = m["target_vocab"]
                t_field = m["target_field"] or "650"
                for lbl_col, lang in (
                    ("legacy_label_fi", "fi"),
                    ("legacy_label_sv", "sv"),
                ):
                    raw = m[lbl_col]
                    if raw and raw.strip():
                        folded = fold_label(raw)
                        if folded:
                            aliases_to_insert.append(
                                {
                                    "folded_label": folded,
                                    "raw_label": raw.strip(),
                                    "lang": lang,
                                    "concept_uri": t_uri,
                                    "wikidata_qid": None,
                                    "vocabulary": t_vocab,
                                    "target_field": t_field,
                                    "is_preferred": 0,
                                    "source": "concept_mapping",
                                }
                            )

        self.insert_aliases(aliases_to_insert)
        return len(aliases_to_insert)
