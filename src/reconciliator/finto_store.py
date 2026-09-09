"""Dedicated SQLite store for modern FINTO ontologies and legacy migration mappings."""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3
from typing import Optional

from reconciliator.db import configure_wal_pragmas, get_db_connection
from reconciliator.models import FintoConcept, FintoMapping


def init_finto_db(conn: sqlite3.Connection) -> None:
    """Initialize finto_concepts and finto_mappings tables in finto.db."""
    with conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS finto_concepts (
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
            "CREATE INDEX IF NOT EXISTS idx_finto_concepts_fi ON finto_concepts(vocabulary, lower(pref_label_fi));"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_finto_concepts_sv ON finto_concepts(vocabulary, lower(pref_label_sv));"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_finto_concepts_en ON finto_concepts(vocabulary, lower(pref_label_en));"
        )

        conn.execute("""
            CREATE TABLE IF NOT EXISTS finto_mappings (
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
        conn.execute("CREATE INDEX IF NOT EXISTS idx_mappings_legacy_uri ON finto_mappings(legacy_uri);")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_mappings_legacy_lbl_fi ON finto_mappings(legacy_vocab, lower(legacy_label_fi));"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_mappings_legacy_lbl_sv ON finto_mappings(legacy_vocab, lower(legacy_label_sv));"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_mappings_target_uri ON finto_mappings(target_uri);")


class FintoStore:
    """Repository handling queries and bulk ingestion into finto.db."""

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self.get_connection() as conn:
            init_finto_db(conn)

    def get_connection(self) -> sqlite3.Connection:
        """Create a new SQLite connection with WAL pragmas."""
        return get_db_connection(self.db_path)

    def insert_concepts(self, concepts: list[FintoConcept]) -> None:
        """Bulk insert or replace concepts in finto_concepts."""
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
                INSERT INTO finto_concepts (
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

    def insert_mappings(self, mappings: list[FintoMapping]) -> None:
        """Bulk insert legacy mappings into finto_mappings."""
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
                INSERT INTO finto_mappings (
                    legacy_uri, legacy_vocab, legacy_label_fi, legacy_label_sv,
                    target_uri, target_vocab, mapping_type, target_field
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?);
                """,
                rows,
            )

    def find_concept_by_uri(self, uri: str) -> Optional[FintoConcept]:
        """Look up concept by URI."""
        with self.get_connection() as conn:
            row = conn.execute("SELECT * FROM finto_concepts WHERE uri = ?;", (uri,)).fetchone()
            if not row:
                return None
            return FintoConcept(
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

    def find_concept_by_label(
        self, label: str, vocabulary: Optional[str] = None, lang: str = "fi"
    ) -> Optional[FintoConcept]:
        """Look up concept by preferred label."""
        clean_lbl = label.strip().lower()
        col = f"pref_label_{lang}" if lang in ("fi", "sv", "en") else "pref_label_fi"
        with self.get_connection() as conn:
            if vocabulary:
                query = f"SELECT * FROM finto_concepts WHERE vocabulary = ? AND lower({col}) = ? LIMIT 1;"
                row = conn.execute(query, (vocabulary, clean_lbl)).fetchone()
            else:
                query = f"SELECT * FROM finto_concepts WHERE lower({col}) = ? LIMIT 1;"
                row = conn.execute(query, (clean_lbl,)).fetchone()

            if not row:
                return None
            return FintoConcept(
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

    def find_legacy_mapping_by_uri(self, uri: str) -> Optional[FintoMapping]:
        """Look up migration mapping by legacy URI."""
        with self.get_connection() as conn:
            row = conn.execute("SELECT * FROM finto_mappings WHERE legacy_uri = ? LIMIT 1;", (uri,)).fetchone()
            if not row:
                return None
            return FintoMapping(
                legacy_uri=row["legacy_uri"],
                legacy_vocab=row["legacy_vocab"],
                legacy_label_fi=row["legacy_label_fi"],
                legacy_label_sv=row["legacy_label_sv"],
                target_uri=row["target_uri"],
                target_vocab=row["target_vocab"],
                mapping_type=row["mapping_type"],
                target_field=row["target_field"],
            )

    def find_legacy_mapping_by_label(
        self, label: str, legacy_vocab: Optional[str] = None, lang: str = "fi"
    ) -> Optional[FintoMapping]:
        """Look up migration mapping by legacy label."""
        clean_lbl = label.strip().lower()
        col = f"legacy_label_{lang}" if lang in ("fi", "sv") else "legacy_label_fi"
        with self.get_connection() as conn:
            if legacy_vocab:
                query = f"SELECT * FROM finto_mappings WHERE legacy_vocab = ? AND lower({col}) = ? LIMIT 1;"
                row = conn.execute(query, (legacy_vocab, clean_lbl)).fetchone()
            else:
                query = f"SELECT * FROM finto_mappings WHERE lower({col}) = ? LIMIT 1;"
                row = conn.execute(query, (clean_lbl,)).fetchone()

            if not row:
                return None
            return FintoMapping(
                legacy_uri=row["legacy_uri"],
                legacy_vocab=row["legacy_vocab"],
                legacy_label_fi=row["legacy_label_fi"],
                legacy_label_sv=row["legacy_label_sv"],
                target_uri=row["target_uri"],
                target_vocab=row["target_vocab"],
                mapping_type=row["mapping_type"],
                target_field=row["target_field"],
            )

