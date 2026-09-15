"""Dedicated SQLite store for controlled vocabularies, ontologies, and migration mappings."""

from __future__ import annotations

import functools
import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Optional, Sequence

from reconciliator.config import VocabNormalizationConfig
from reconciliator.db import get_db_connection
from reconciliator.models import ConceptMapping, VocabConcept
from reconciliator.normalizers.linguistic import fold_label
from reconciliator.vocabularies import concept_id_to_uri, normalize_concept_id


def is_language_allowed(lang: str, allowed: Optional[Sequence[str] | str]) -> bool:
    """Check whether a language tag is allowed by an ingestion language filter.

    Supports None, '*', 'all', ['*'], ['all'] for wildcard / ingest-all behavior.
    """
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


def _migrate_flat_vocab_db_if_needed(conn: sqlite3.Connection) -> None:
    """Migrate legacy flat tables (pref_label_fi, legacy_label_fi, etc.) to normalized 1-to-many schema."""
    cursor = conn.execute("PRAGMA table_info(concepts);")
    cols = {row["name"] for row in cursor.fetchall()}
    if cols and "pref_label_fi" in cols:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS concept_labels_temp (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                concept_uri TEXT NOT NULL,
                lang TEXT NOT NULL,
                label_type TEXT NOT NULL,
                label TEXT NOT NULL
            );
        """)
        for col_name, lang in (("pref_label_fi", "fi"), ("pref_label_sv", "sv"), ("pref_label_en", "en")):
            if col_name in cols:
                conn.execute(f"""
                    INSERT INTO concept_labels_temp (concept_uri, lang, label_type, label)
                    SELECT uri, '{lang}', 'pref', trim({col_name})
                    FROM concepts
                    WHERE {col_name} IS NOT NULL AND trim({col_name}) != '';
                """)
        for col_name, lang in (("alt_labels_fi", "fi"), ("alt_labels_sv", "sv")):
            if col_name in cols:
                rows = conn.execute(
                    f"SELECT uri, {col_name} FROM concepts WHERE {col_name} IS NOT NULL AND {col_name} != '';"
                ).fetchall()
                for r in rows:
                    c_uri = r["uri"]
                    raw_json = r[col_name]
                    try:
                        alts = json.loads(raw_json)
                        for alt in alts:
                            if alt and str(alt).strip():
                                conn.execute(
                                    "INSERT INTO concept_labels_temp (concept_uri, lang, label_type, label) VALUES (?, ?, 'alt', ?);",
                                    (c_uri, lang, str(alt).strip()),
                                )
                    except Exception:
                        pass

        conn.execute("""
            CREATE TABLE concepts_new (
                uri TEXT PRIMARY KEY,
                vocabulary TEXT NOT NULL,
                is_deprecated INTEGER DEFAULT 0,
                target_field TEXT
            );
        """)
        conn.execute("""
            INSERT INTO concepts_new (uri, vocabulary, is_deprecated, target_field)
            SELECT uri, vocabulary, coalesce(is_deprecated, 0), target_field
            FROM concepts;
        """)
        conn.execute("DROP TABLE concepts;")
        conn.execute("ALTER TABLE concepts_new RENAME TO concepts;")

        conn.execute("""
            CREATE TABLE IF NOT EXISTS concept_labels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                concept_uri TEXT NOT NULL REFERENCES concepts(uri) ON DELETE CASCADE,
                lang TEXT NOT NULL,
                label_type TEXT NOT NULL,
                label TEXT NOT NULL
            );
        """)
        conn.execute("INSERT INTO concept_labels SELECT * FROM concept_labels_temp;")
        conn.execute("DROP TABLE concept_labels_temp;")

    cursor_m = conn.execute("PRAGMA table_info(concept_mappings);")
    cols_m = {row["name"] for row in cursor_m.fetchall()}
    if cols_m and "legacy_label_fi" in cols_m:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS concept_mapping_labels_temp (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                mapping_id INTEGER NOT NULL,
                lang TEXT NOT NULL,
                label TEXT NOT NULL
            );
        """)
        for col_name, lang in (("legacy_label_fi", "fi"), ("legacy_label_sv", "sv")):
            if col_name in cols_m:
                conn.execute(f"""
                    INSERT INTO concept_mapping_labels_temp (mapping_id, lang, label)
                    SELECT id, '{lang}', trim({col_name})
                    FROM concept_mappings
                    WHERE {col_name} IS NOT NULL AND trim({col_name}) != '';
                """)
        conn.execute("""
            CREATE TABLE concept_mappings_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                legacy_uri TEXT,
                legacy_vocab TEXT NOT NULL,
                target_uri TEXT NOT NULL,
                target_vocab TEXT NOT NULL,
                mapping_type TEXT NOT NULL,
                target_field TEXT NOT NULL
            );
        """)
        conn.execute("""
            INSERT INTO concept_mappings_new (id, legacy_uri, legacy_vocab, target_uri, target_vocab, mapping_type, target_field)
            SELECT id, legacy_uri, legacy_vocab, target_uri, target_vocab, mapping_type, target_field
            FROM concept_mappings;
        """)
        conn.execute("DROP TABLE concept_mappings;")
        conn.execute("ALTER TABLE concept_mappings_new RENAME TO concept_mappings;")

        conn.execute("""
            CREATE TABLE IF NOT EXISTS concept_mapping_labels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                mapping_id INTEGER NOT NULL REFERENCES concept_mappings(id) ON DELETE CASCADE,
                lang TEXT NOT NULL,
                label TEXT NOT NULL
            );
        """)
        conn.execute("INSERT INTO concept_mapping_labels SELECT * FROM concept_mapping_labels_temp;")
        conn.execute("DROP TABLE concept_mapping_labels_temp;")


def init_vocab_db(conn: sqlite3.Connection) -> None:
    """Initialize concepts, concept_labels, concept_mappings, concept_mapping_labels, relator_terms, and concept_aliases tables in vocab.db."""
    with conn:
        _migrate_flat_vocab_db_if_needed(conn)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS concepts (
                uri TEXT PRIMARY KEY,
                vocabulary TEXT NOT NULL,
                is_deprecated INTEGER DEFAULT 0,
                target_field TEXT
            );
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_concepts_vocab ON concepts(vocabulary);")

        conn.execute("""
            CREATE TABLE IF NOT EXISTS concept_labels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                concept_uri TEXT NOT NULL REFERENCES concepts(uri) ON DELETE CASCADE,
                lang TEXT NOT NULL,
                label_type TEXT NOT NULL,
                label TEXT NOT NULL
            );
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_concept_labels_lookup ON concept_labels(lang, label_type, lower(label));"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_concept_labels_concept_uri ON concept_labels(concept_uri);"
        )

        conn.execute("""
            CREATE TABLE IF NOT EXISTS concept_mappings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                legacy_uri TEXT,
                legacy_vocab TEXT NOT NULL,
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
            "CREATE INDEX IF NOT EXISTS idx_mappings_target_uri ON concept_mappings(target_uri);"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_mappings_vocab ON concept_mappings(legacy_vocab);"
        )

        conn.execute("""
            CREATE TABLE IF NOT EXISTS concept_mapping_labels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                mapping_id INTEGER NOT NULL REFERENCES concept_mappings(id) ON DELETE CASCADE,
                lang TEXT NOT NULL,
                label TEXT NOT NULL
            );
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_concept_mapping_labels_lookup ON concept_mapping_labels(lang, lower(label));"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_concept_mapping_labels_mapping_id ON concept_mapping_labels(mapping_id);"
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

    def __init__(
        self,
        db_path: Path | str,
        config: Optional[VocabNormalizationConfig] = None,
    ) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.config = config or VocabNormalizationConfig()
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
        """Bulk insert or replace concepts and their multi-language labels."""
        if not concepts:
            return
        concept_rows = [
            (
                c.uri,
                c.vocabulary,
                1 if c.is_deprecated else 0,
                c.target_field,
            )
            for c in concepts
        ]
        label_rows: list[tuple[str, str, str, str]] = []
        for c in concepts:
            for lang, lbl in c.pref_labels.items():
                if lbl and lbl.strip():
                    if is_language_allowed(lang, self.config.ingest_pref_label_languages):
                        label_rows.append((c.uri, lang, "pref", lbl.strip()))
            for lang, alts in c.alt_labels.items():
                if is_language_allowed(lang, self.config.ingest_alt_label_languages):
                    for alt in alts:
                        if alt and str(alt).strip():
                            label_rows.append((c.uri, lang, "alt", str(alt).strip()))

        with self.get_connection() as conn:
            conn.executemany(
                """
                INSERT INTO concepts (uri, vocabulary, is_deprecated, target_field)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(uri) DO UPDATE SET
                    vocabulary = excluded.vocabulary,
                    is_deprecated = excluded.is_deprecated,
                    target_field = excluded.target_field;
                """,
                concept_rows,
            )
            uris = [(c.uri,) for c in concepts]
            conn.executemany("DELETE FROM concept_labels WHERE concept_uri = ?;", uris)
            if label_rows:
                conn.executemany(
                    """
                    INSERT INTO concept_labels (concept_uri, lang, label_type, label)
                    VALUES (?, ?, ?, ?);
                    """,
                    label_rows,
                )
        self.clear_caches()

    def insert_mappings(self, mappings: list[ConceptMapping]) -> None:
        """Bulk insert legacy mappings into concept_mappings and concept_mapping_labels."""
        if not mappings:
            return
        with self.get_connection() as conn:
            for m in mappings:
                cur = conn.execute(
                    """
                    INSERT INTO concept_mappings (
                        legacy_uri, legacy_vocab, target_uri, target_vocab, mapping_type, target_field
                    ) VALUES (?, ?, ?, ?, ?, ?);
                    """,
                    (
                        m.legacy_uri,
                        m.legacy_vocab,
                        m.target_uri,
                        m.target_vocab,
                        m.mapping_type,
                        m.target_field,
                    ),
                )
                m_id = cur.lastrowid
                label_rows = []
                for lang, lbl in m.legacy_labels.items():
                    if lbl and lbl.strip():
                        label_rows.append((m_id, lang, lbl.strip()))
                if label_rows:
                    conn.executemany(
                        "INSERT INTO concept_mapping_labels (mapping_id, lang, label) VALUES (?, ?, ?);",
                        label_rows,
                    )
        self.clear_caches()

    @functools.lru_cache(maxsize=32768)
    def find_concept_by_uri(self, uri: str) -> Optional[VocabConcept]:
        """Look up concept by URI or canonical concept identifier (e.g. yso:p2591, (FI-YSO)p2591)."""
        if not uri:
            return None
        clean_uri = uri.strip()
        with self.get_connection() as conn:
            # 1. Exact lookup
            row = conn.execute("SELECT * FROM concepts WHERE uri = ?;", (clean_uri,)).fetchone()

            # 2. Canonical expansion if exact match missed
            if not row:
                rules = getattr(self.config, "concept_id_rules", None)
                norm_id = normalize_concept_id(clean_uri, rules=rules)
                if norm_id:
                    expanded_uri = concept_id_to_uri(norm_id, rules=rules)
                    if expanded_uri and expanded_uri != clean_uri:
                        row = conn.execute("SELECT * FROM concepts WHERE uri = ?;", (expanded_uri,)).fetchone()

            # 3. Trailing slash / protocol variant fallback
            if not row:
                candidates = [clean_uri.rstrip("/")] if clean_uri.endswith("/") else [clean_uri + "/"]
                if clean_uri.startswith("https://"):
                    candidates.append("http://" + clean_uri[8:])
                elif clean_uri.startswith("http://"):
                    candidates.append("https://" + clean_uri[7:])
                for cand in candidates:
                    row = conn.execute("SELECT * FROM concepts WHERE uri = ?;", (cand,)).fetchone()
                    if row:
                        break

            if not row:
                return None
            concept_uri = row["uri"]
            labels_rows = conn.execute(
                "SELECT lang, label_type, label FROM concept_labels WHERE concept_uri = ?;",
                (concept_uri,),
            ).fetchall()

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
                uri=concept_uri,
                vocabulary=row["vocabulary"],
                pref_labels=pref_labels,
                alt_labels=alt_labels,
                is_deprecated=bool(row["is_deprecated"]),
                target_field=row["target_field"],
            )

    @functools.lru_cache(maxsize=32768)
    def find_concept_by_label(
        self, label: str, vocabulary: Optional[str] = None, lang: str = "fi"
    ) -> Optional[VocabConcept]:
        """Look up concept by preferred label."""
        clean_lbl = label.strip().lower()
        norm_l = self.config.language_code_map.get(lang.lower(), lang.lower())
        with self.get_connection() as conn:
            if vocabulary:
                query = """
                    SELECT c.uri
                    FROM concept_labels cl
                    JOIN concepts c ON cl.concept_uri = c.uri
                    WHERE c.vocabulary = ? AND cl.lang = ? AND cl.label_type = 'pref' AND lower(cl.label) = ?
                    ORDER BY c.is_deprecated ASC
                    LIMIT 1;
                """
                row = conn.execute(query, (vocabulary, norm_l, clean_lbl)).fetchone()
            else:
                query = """
                    SELECT c.uri
                    FROM concept_labels cl
                    JOIN concepts c ON cl.concept_uri = c.uri
                    WHERE cl.lang = ? AND cl.label_type = 'pref' AND lower(cl.label) = ?
                    ORDER BY c.is_deprecated ASC
                    LIMIT 1;
                """
                row = conn.execute(query, (norm_l, clean_lbl)).fetchone()

            if not row:
                return None
            return self.find_concept_by_uri(row["uri"])

    @functools.lru_cache(maxsize=16384)
    def find_legacy_mapping_by_uri(self, uri: str) -> Optional[ConceptMapping]:
        """Look up migration mapping by legacy URI or canonical concept identifier."""
        if not uri:
            return None
        clean_uri = uri.strip()
        with self.get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM concept_mappings WHERE legacy_uri = ? LIMIT 1;", (clean_uri,)
            ).fetchone()

            if not row:
                rules = getattr(self.config, "concept_id_rules", None)
                norm_id = normalize_concept_id(clean_uri, rules=rules)
                if norm_id:
                    expanded_uri = concept_id_to_uri(norm_id, rules=rules)
                    if expanded_uri and expanded_uri != clean_uri:
                        row = conn.execute(
                            "SELECT * FROM concept_mappings WHERE legacy_uri = ? LIMIT 1;", (expanded_uri,)
                        ).fetchone()

            if not row:
                candidates = [clean_uri.rstrip("/")] if clean_uri.endswith("/") else [clean_uri + "/"]
                if clean_uri.startswith("https://"):
                    candidates.append("http://" + clean_uri[8:])
                elif clean_uri.startswith("http://"):
                    candidates.append("https://" + clean_uri[7:])
                for cand in candidates:
                    row = conn.execute(
                        "SELECT * FROM concept_mappings WHERE legacy_uri = ? LIMIT 1;", (cand,)
                    ).fetchone()
                    if row:
                        break

            if not row:
                return None
            mapping_id = row["id"]
            lbl_rows = conn.execute(
                "SELECT lang, label FROM concept_mapping_labels WHERE mapping_id = ?;",
                (mapping_id,),
            ).fetchall()
            legacy_labels = {r["lang"]: r["label"] for r in lbl_rows}
            return ConceptMapping(
                legacy_uri=row["legacy_uri"],
                legacy_vocab=row["legacy_vocab"],
                legacy_labels=legacy_labels,
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
        norm_l = self.config.language_code_map.get(lang.lower(), lang.lower())
        with self.get_connection() as conn:
            if legacy_vocab:
                query = """
                    SELECT m.id
                    FROM concept_mapping_labels cml
                    JOIN concept_mappings m ON cml.mapping_id = m.id
                    WHERE m.legacy_vocab = ? AND cml.lang = ? AND lower(cml.label) = ?
                    LIMIT 1;
                """
                row = conn.execute(query, (legacy_vocab, norm_l, clean_lbl)).fetchone()
            else:
                query = """
                    SELECT m.id
                    FROM concept_mapping_labels cml
                    JOIN concept_mappings m ON cml.mapping_id = m.id
                    WHERE cml.lang = ? AND lower(cml.label) = ?
                    LIMIT 1;
                """
                row = conn.execute(query, (norm_l, clean_lbl)).fetchone()

            if not row:
                return None
            mapping_id = row["id"]
            m_row = conn.execute("SELECT * FROM concept_mappings WHERE id = ?;", (mapping_id,)).fetchone()
            if not m_row:
                return None
            lbl_rows = conn.execute(
                "SELECT lang, label FROM concept_mapping_labels WHERE mapping_id = ?;",
                (mapping_id,),
            ).fetchall()
            legacy_labels = {r["lang"]: r["label"] for r in lbl_rows}
            return ConceptMapping(
                legacy_uri=m_row["legacy_uri"],
                legacy_vocab=m_row["legacy_vocab"],
                legacy_labels=legacy_labels,
                target_uri=m_row["target_uri"],
                target_vocab=m_row["target_vocab"],
                mapping_type=m_row["mapping_type"],
                target_field=m_row["target_field"],
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
                a.get("target_field") or self.config.default_vocab_target_field,
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
        """Look up concept by normalized alias (accepts either pre-folded or raw label).

        Returns (concept, wikidata_qid) or (None, None).
        """
        clean = folded_label.strip()
        if not clean:
            return None, None

        clean_folded = fold_label(
            clean,
            preserved_diacritics=self.config.preserved_diacritics,
        ) or clean.lower()

        search_tokens = [clean_folded]
        if clean != clean_folded:
            search_tokens.append(clean)

        with self.get_connection() as conn:
            token_placeholders = ",".join("?" for _ in search_tokens)
            params: list[Any] = list(search_tokens)
            where_clauses = [f"folded_label IN ({token_placeholders})"]
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
                fallback_query = f"""
                    SELECT concept_uri, wikidata_qid
                    FROM concept_aliases
                    WHERE folded_label IN ({token_placeholders})
                    ORDER BY is_preferred DESC, id ASC
                    LIMIT 1;
                """
                row = conn.execute(fallback_query, tuple(search_tokens)).fetchone()

            if not row:
                return None, None

            concept_uri = row["concept_uri"]
            wikidata_qid = row["wikidata_qid"]
            concept = self.find_concept_by_uri(concept_uri)
            return concept, wikidata_qid

    def find_concepts_by_uris(self, uris: Sequence[str]) -> dict[str, VocabConcept]:
        """Look up multiple concepts by URI or concept ID in batch.

        Returns a dictionary mapping each queried input URI to its corresponding VocabConcept.
        """
        results: dict[str, VocabConcept] = {}
        for u in uris:
            if not u:
                continue
            concept = self.find_concept_by_uri(u)
            if concept:
                results[u] = concept
        return results

    def rebuild_aliases_from_concepts(
        self, legacy_vocabs: Optional[set[str]] = None
    ) -> int:
        """Rebuild concept_aliases table from concepts and concept_mappings."""
        aliases_to_insert: list[dict[str, Any]] = []
        active_legacy_vocabs = (
            legacy_vocabs
            if legacy_vocabs is not None
            else set(self.config.legacy_to_modern.keys())
        )

        with self.get_connection() as conn:
            # Clear existing vocabulary-derived aliases
            conn.execute(
                "DELETE FROM concept_aliases WHERE source IN ('vocab', 'concept_mapping');"
            )

            # 1. From concepts and concept_labels
            query = """
                SELECT c.uri, c.vocabulary, c.target_field, cl.lang, cl.label_type, cl.label
                FROM concepts c
                JOIN concept_labels cl ON c.uri = cl.concept_uri
                WHERE c.is_deprecated = 0;
            """
            rows = conn.execute(query).fetchall()
            for r in rows:
                c_vocab = r["vocabulary"]
                if c_vocab in active_legacy_vocabs:
                    continue
                c_uri = r["uri"]
                t_field = r["target_field"] or self.config.default_vocab_target_field
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
                                "concept_uri": c_uri,
                                "wikidata_qid": None,
                                "vocabulary": c_vocab,
                                "target_field": t_field,
                                "is_preferred": 1 if label_type == "pref" else 0,
                                "source": "vocab",
                            }
                        )

            # 2. From concept_mappings and concept_mapping_labels
            query_m = """
                SELECT m.target_uri, m.target_vocab, m.target_field, cml.lang, cml.label
                FROM concept_mappings m
                JOIN concept_mapping_labels cml ON m.id = cml.mapping_id;
            """
            mapping_rows = conn.execute(query_m).fetchall()
            for m in mapping_rows:
                t_uri = m["target_uri"]
                t_vocab = m["target_vocab"]
                t_field = m["target_field"] or self.config.default_vocab_target_field
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
