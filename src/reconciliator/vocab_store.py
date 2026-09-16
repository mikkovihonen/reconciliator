"""SQLAlchemy 2.0 Core store for controlled vocabularies, ontologies, and migration mappings."""

from __future__ import annotations

import functools
import json
import logging
from pathlib import Path
from typing import Any, Optional, Sequence

from sqlalchemy import (
    Column,
    Engine,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    and_,
    delete,
    func,
    insert,
    select,
    text,
)
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from reconciliator.config import VocabNormalizationConfig
from reconciliator.db import get_engine
from reconciliator.models import ConceptMapping, VocabConcept
from reconciliator.normalizers.linguistic import fold_label
from reconciliator.vocabularies import concept_id_to_uri, normalize_concept_id

logger = logging.getLogger(__name__)

# ===================================================================
# Vocab Schema & Table Definitions
# ===================================================================

vocab_metadata = MetaData()

concepts_table = Table(
    "concepts",
    vocab_metadata,
    Column("uri", String, primary_key=True),
    Column("vocabulary", String, nullable=False),
    Column("is_deprecated", Integer, default=0, server_default="0"),
    Column("target_field", String, nullable=True),
    Index("idx_concepts_vocab", "vocabulary"),
)

concept_labels_table = Table(
    "concept_labels",
    vocab_metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("concept_uri", String, ForeignKey("concepts.uri", ondelete="CASCADE"), nullable=False),
    Column("lang", String, nullable=False),
    Column("label_type", String, nullable=False),
    Column("label", String, nullable=False),
    Index("idx_concept_labels_concept_uri", "concept_uri"),
)

concept_mappings_table = Table(
    "concept_mappings",
    vocab_metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("legacy_uri", String, nullable=True),
    Column("legacy_vocab", String, nullable=False),
    Column("target_uri", String, nullable=False),
    Column("target_vocab", String, nullable=False),
    Column("mapping_type", String, nullable=False),
    Column("target_field", String, nullable=False),
    Index("idx_mappings_legacy_uri", "legacy_uri"),
    Index("idx_mappings_target_uri", "target_uri"),
    Index("idx_mappings_vocab", "legacy_vocab"),
)

concept_mapping_labels_table = Table(
    "concept_mapping_labels",
    vocab_metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("mapping_id", Integer, ForeignKey("concept_mappings.id", ondelete="CASCADE"), nullable=False),
    Column("lang", String, nullable=False),
    Column("label", String, nullable=False),
    Index("idx_concept_mapping_labels_mapping_id", "mapping_id"),
)

relator_terms_table = Table(
    "relator_terms",
    vocab_metadata,
    Column("folded_term", String, primary_key=True),
    Column("raw_term", String, nullable=False),
    Column("lang", String, nullable=False),
    Column("code", String, nullable=True),
    Column("uri", String, nullable=True),
    Index("idx_relators_lang", "lang"),
)

concept_aliases_table = Table(
    "concept_aliases",
    vocab_metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("folded_label", String, nullable=False),
    Column("raw_label", String, nullable=False),
    Column("lang", String, nullable=False),
    Column("concept_uri", String, nullable=False),
    Column("wikidata_qid", String, nullable=True),
    Column("vocabulary", String, nullable=False),
    Column("target_field", String, nullable=False),
    Column("is_preferred", Integer, nullable=False),
    Column("source", String, nullable=False),
    Index("idx_concept_aliases_lookup", "folded_label", "target_field"),
    Index("idx_concept_aliases_uri", "concept_uri"),
    Index("idx_concept_aliases_qid", "wikidata_qid"),
)


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


def _migrate_flat_vocab_db_if_needed(engine: Engine) -> None:
    """Migrate legacy flat tables (pref_label_fi, legacy_label_fi, etc.) to normalized schema."""
    with engine.begin() as conn:
        cursor = conn.execute(text("PRAGMA table_info(concepts);"))
        cols = {row[1] for row in cursor.fetchall()}
        if cols and "pref_label_fi" in cols:
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS concept_labels_temp (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    concept_uri TEXT NOT NULL,
                    lang TEXT NOT NULL,
                    label_type TEXT NOT NULL,
                    label TEXT NOT NULL
                );
            """))
            for col_name, lang in (("pref_label_fi", "fi"), ("pref_label_sv", "sv"), ("pref_label_en", "en")):
                if col_name in cols:
                    conn.execute(text(f"""
                        INSERT INTO concept_labels_temp (concept_uri, lang, label_type, label)
                        SELECT uri, '{lang}', 'pref', trim({col_name})
                        FROM concepts
                        WHERE {col_name} IS NOT NULL AND trim({col_name}) != '';
                    """))
            for col_name, lang in (("alt_labels_fi", "fi"), ("alt_labels_sv", "sv")):
                if col_name in cols:
                    rows = conn.execute(
                        text(f"SELECT uri, {col_name} FROM concepts WHERE {col_name} IS NOT NULL AND {col_name} != '';")
                    ).fetchall()
                    for r in rows:
                        c_uri = r[0]
                        raw_json = r[1]
                        try:
                            alts = json.loads(raw_json)
                            for alt in alts:
                                if alt and str(alt).strip():
                                    conn.execute(
                                        text("INSERT INTO concept_labels_temp (concept_uri, lang, label_type, label) VALUES (:uri, :lang, 'alt', :label);"),
                                        {"uri": c_uri, "lang": lang, "label": str(alt).strip()},
                                    )
                        except Exception:
                            pass

            conn.execute(text("""
                CREATE TABLE concepts_new (
                    uri TEXT PRIMARY KEY,
                    vocabulary TEXT NOT NULL,
                    is_deprecated INTEGER DEFAULT 0,
                    target_field TEXT
                );
            """))
            conn.execute(text("""
                INSERT INTO concepts_new (uri, vocabulary, is_deprecated, target_field)
                SELECT uri, vocabulary, coalesce(is_deprecated, 0), target_field
                FROM concepts;
            """))
            conn.execute(text("DROP TABLE concepts;"))
            conn.execute(text("ALTER TABLE concepts_new RENAME TO concepts;"))

            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS concept_labels (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    concept_uri TEXT NOT NULL REFERENCES concepts(uri) ON DELETE CASCADE,
                    lang TEXT NOT NULL,
                    label_type TEXT NOT NULL,
                    label TEXT NOT NULL
                );
            """))
            conn.execute(text("INSERT INTO concept_labels SELECT * FROM concept_labels_temp;"))
            conn.execute(text("DROP TABLE concept_labels_temp;"))

        cursor_m = conn.execute(text("PRAGMA table_info(concept_mappings);"))
        cols_m = {row[1] for row in cursor_m.fetchall()}
        if cols_m and "legacy_label_fi" in cols_m:
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS concept_mapping_labels_temp (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    mapping_id INTEGER NOT NULL,
                    lang TEXT NOT NULL,
                    label TEXT NOT NULL
                );
            """))
            for col_name, lang in (("legacy_label_fi", "fi"), ("legacy_label_sv", "sv")):
                if col_name in cols_m:
                    conn.execute(text(f"""
                        INSERT INTO concept_mapping_labels_temp (mapping_id, lang, label)
                        SELECT id, '{lang}', trim({col_name})
                        FROM concept_mappings
                        WHERE {col_name} IS NOT NULL AND trim({col_name}) != '';
                    """))
            conn.execute(text("""
                CREATE TABLE concept_mappings_new (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    legacy_uri TEXT,
                    legacy_vocab TEXT NOT NULL,
                    target_uri TEXT NOT NULL,
                    target_vocab TEXT NOT NULL,
                    mapping_type TEXT NOT NULL,
                    target_field TEXT NOT NULL
                );
            """))
            conn.execute(text("""
                INSERT INTO concept_mappings_new (id, legacy_uri, legacy_vocab, target_uri, target_vocab, mapping_type, target_field)
                SELECT id, legacy_uri, legacy_vocab, target_uri, target_vocab, mapping_type, target_field
                FROM concept_mappings;
            """))
            conn.execute(text("DROP TABLE concept_mappings;"))
            conn.execute(text("ALTER TABLE concept_mappings_new RENAME TO concept_mappings;"))

            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS concept_mapping_labels (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    mapping_id INTEGER NOT NULL REFERENCES concept_mappings(id) ON DELETE CASCADE,
                    lang TEXT NOT NULL,
                    label TEXT NOT NULL
                );
            """))
            conn.execute(text("INSERT INTO concept_mapping_labels SELECT * FROM concept_mapping_labels_temp;"))
            conn.execute(text("DROP TABLE concept_mapping_labels_temp;"))


def init_vocab_db(engine: Engine) -> None:
    """Initialize concepts, concept_labels, concept_mappings, concept_mapping_labels, relator_terms, and concept_aliases tables in vocab.db."""
    _migrate_flat_vocab_db_if_needed(engine)
    vocab_metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_concept_labels_lookup ON concept_labels(lang, label_type, lower(label));"
        ))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_concept_mapping_labels_lookup ON concept_mapping_labels(lang, lower(label));"
        ))


class VocabStore:
    """Repository handling queries and bulk ingestion into vocab.db using SQLAlchemy 2.0 Core."""

    def __init__(
        self,
        db_path: Path | str,
        config: Optional[VocabNormalizationConfig] = None,
    ) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.config = config or VocabNormalizationConfig()
        self.engine = get_engine(self.db_path)
        init_vocab_db(self.engine)

    def get_connection(self) -> Any:
        """Get a configured connection adapter backed by the SQLAlchemy Engine."""
        from reconciliator.db import ConnectionAdapter
        return ConnectionAdapter(self.engine.connect(), auto_close=True)

    def close(self) -> None:
        """Dispose SQLAlchemy engine and release connection pool."""
        self.engine.dispose()

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
            {
                "uri": c.uri,
                "vocabulary": c.vocabulary,
                "is_deprecated": 1 if c.is_deprecated else 0,
                "target_field": c.target_field,
            }
            for c in concepts
        ]
        label_rows: list[dict[str, str]] = []
        for c in concepts:
            for lang, lbl in c.pref_labels.items():
                if lbl and lbl.strip():
                    if is_language_allowed(lang, self.config.ingest_pref_label_languages):
                        label_rows.append({"concept_uri": c.uri, "lang": lang, "label_type": "pref", "label": lbl.strip()})
            for lang, alts in c.alt_labels.items():
                if is_language_allowed(lang, self.config.ingest_alt_label_languages):
                    for alt in alts:
                        if alt and str(alt).strip():
                            label_rows.append({"concept_uri": c.uri, "lang": lang, "label_type": "alt", "label": str(alt).strip()})

        with self.engine.begin() as conn:
            stmt = sqlite_insert(concepts_table).values(concept_rows)
            stmt = stmt.on_conflict_do_update(
                index_elements=["uri"],
                set_={
                    "vocabulary": stmt.excluded.vocabulary,
                    "is_deprecated": stmt.excluded.is_deprecated,
                    "target_field": stmt.excluded.target_field,
                },
            )
            conn.execute(stmt)

            uris = [c.uri for c in concepts]
            conn.execute(delete(concept_labels_table).where(concept_labels_table.c.concept_uri.in_(uris)))
            if label_rows:
                conn.execute(insert(concept_labels_table), label_rows)

        self.clear_caches()

    def insert_mappings(self, mappings: list[ConceptMapping]) -> None:
        """Bulk insert legacy mappings into concept_mappings and concept_mapping_labels."""
        if not mappings:
            return
        with self.engine.begin() as conn:
            for m in mappings:
                res = conn.execute(
                    insert(concept_mappings_table).values(
                        legacy_uri=m.legacy_uri,
                        legacy_vocab=m.legacy_vocab,
                        target_uri=m.target_uri,
                        target_vocab=m.target_vocab,
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

    @functools.lru_cache(maxsize=32768)
    def find_concept_by_uri(self, uri: str) -> Optional[VocabConcept]:
        """Look up concept by URI or canonical concept identifier (e.g. yso:p2591, (FI-YSO)p2591)."""
        if not uri:
            return None
        clean_uri = uri.strip()
        with self.engine.connect() as conn:
            # 1. Exact lookup
            stmt = select(concepts_table).where(concepts_table.c.uri == clean_uri)
            row = conn.execute(stmt).mappings().first()

            # 2. Canonical expansion if exact match missed
            if not row:
                rules = getattr(self.config, "concept_id_rules", None)
                norm_id = normalize_concept_id(clean_uri, rules=rules)
                if norm_id:
                    expanded_uri = concept_id_to_uri(norm_id, rules=rules)
                    if expanded_uri and expanded_uri != clean_uri:
                        stmt = select(concepts_table).where(concepts_table.c.uri == expanded_uri)
                        row = conn.execute(stmt).mappings().first()

            # 3. Trailing slash / protocol variant fallback
            if not row:
                candidates = [clean_uri.rstrip("/")] if clean_uri.endswith("/") else [clean_uri + "/"]
                if clean_uri.startswith("https://"):
                    candidates.append("http://" + clean_uri[8:])
                elif clean_uri.startswith("http://"):
                    candidates.append("https://" + clean_uri[7:])
                for cand in candidates:
                    stmt = select(concepts_table).where(concepts_table.c.uri == cand)
                    row = conn.execute(stmt).mappings().first()
                    if row:
                        break

            if not row:
                return None
            concept_uri = row["uri"]
            labels_stmt = select(
                concept_labels_table.c.lang,
                concept_labels_table.c.label_type,
                concept_labels_table.c.label,
            ).where(concept_labels_table.c.concept_uri == concept_uri)
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
        with self.engine.connect() as conn:
            stmt = select(concepts_table.c.uri).select_from(
                concept_labels_table.join(concepts_table, concept_labels_table.c.concept_uri == concepts_table.c.uri)
            ).where(
                concept_labels_table.c.lang == norm_l,
                concept_labels_table.c.label_type == "pref",
                func.lower(concept_labels_table.c.label) == clean_lbl,
            ).order_by(concepts_table.c.is_deprecated.asc()).limit(1)

            if vocabulary:
                stmt = stmt.where(concepts_table.c.vocabulary == vocabulary)

            row = conn.execute(stmt).first()
            if not row:
                return None
            return self.find_concept_by_uri(row[0])

    @functools.lru_cache(maxsize=16384)
    def find_legacy_mapping_by_uri(self, uri: str) -> Optional[ConceptMapping]:
        """Look up migration mapping by legacy URI or canonical concept identifier."""
        if not uri:
            return None
        clean_uri = uri.strip()
        with self.engine.connect() as conn:
            stmt = select(concept_mappings_table).where(concept_mappings_table.c.legacy_uri == clean_uri).limit(1)
            row = conn.execute(stmt).mappings().first()

            if not row:
                rules = getattr(self.config, "concept_id_rules", None)
                norm_id = normalize_concept_id(clean_uri, rules=rules)
                if norm_id:
                    expanded_uri = concept_id_to_uri(norm_id, rules=rules)
                    if expanded_uri and expanded_uri != clean_uri:
                        stmt = select(concept_mappings_table).where(concept_mappings_table.c.legacy_uri == expanded_uri).limit(1)
                        row = conn.execute(stmt).mappings().first()

            if not row:
                candidates = [clean_uri.rstrip("/")] if clean_uri.endswith("/") else [clean_uri + "/"]
                if clean_uri.startswith("https://"):
                    candidates.append("http://" + clean_uri[8:])
                elif clean_uri.startswith("http://"):
                    candidates.append("https://" + clean_uri[7:])
                for cand in candidates:
                    stmt = select(concept_mappings_table).where(concept_mappings_table.c.legacy_uri == cand).limit(1)
                    row = conn.execute(stmt).mappings().first()
                    if row:
                        break

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
        with self.engine.connect() as conn:
            stmt = select(concept_mappings_table.c.id).select_from(
                concept_mapping_labels_table.join(
                    concept_mappings_table,
                    concept_mapping_labels_table.c.mapping_id == concept_mappings_table.c.id,
                )
            ).where(
                concept_mapping_labels_table.c.lang == norm_l,
                func.lower(concept_mapping_labels_table.c.label) == clean_lbl,
            ).limit(1)

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
        rows: list[dict[str, Any]] = []
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
            rows.append({
                "folded_term": folded,
                "raw_term": raw,
                "lang": lang,
                "code": code,
                "uri": uri,
            })

        with self.engine.begin() as conn:
            stmt = sqlite_insert(relator_terms_table).values(rows)
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
            stmt = select(1).where(relator_terms_table.c.folded_term == folded_term.strip().casefold()).limit(1)
            row = conn.execute(stmt).first()
            return bool(row)

    @functools.lru_cache(maxsize=8192)
    def get_relator_code(self, folded_term: str) -> Optional[str]:
        """Look up standard MARC code for a folded role term in relator_terms."""
        with self.engine.connect() as conn:
            stmt = select(relator_terms_table.c.code).where(
                relator_terms_table.c.folded_term == folded_term.strip().casefold(),
                relator_terms_table.c.code.isnot(None),
            ).limit(1)
            row = conn.execute(stmt).first()
            return str(row[0]) if row and row[0] else None

    def insert_aliases(self, aliases: list[dict[str, Any]]) -> None:
        """Bulk insert alias records into concept_aliases."""
        if not aliases:
            return
        rows = [
            {
                "folded_label": a["folded_label"],
                "raw_label": a["raw_label"],
                "lang": a["lang"],
                "concept_uri": a["concept_uri"],
                "wikidata_qid": a.get("wikidata_qid"),
                "vocabulary": a["vocabulary"],
                "target_field": a.get("target_field") or self.config.default_vocab_target_field,
                "is_preferred": a.get("is_preferred", 1),
                "source": a.get("source", "vocab"),
            }
            for a in aliases
        ]
        with self.engine.begin() as conn:
            conn.execute(insert(concept_aliases_table), rows)
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

        clean_folded = fold_label(
            clean,
            preserved_diacritics=self.config.preserved_diacritics,
        ) or clean.lower()

        search_tokens = [clean_folded]
        if clean != clean_folded:
            search_tokens.append(clean)

        with self.engine.connect() as conn:
            conditions: list[Any] = [concept_aliases_table.c.folded_label.in_(search_tokens)]
            if target_field:
                conditions.append(concept_aliases_table.c.target_field == target_field)
            if vocabulary:
                conditions.append(concept_aliases_table.c.vocabulary == vocabulary)

            stmt = select(
                concept_aliases_table.c.concept_uri,
                concept_aliases_table.c.wikidata_qid,
            ).where(and_(*conditions)).order_by(
                concept_aliases_table.c.is_preferred.desc(),
                concept_aliases_table.c.id.asc(),
            ).limit(1)

            row = conn.execute(stmt).first()
            if not row and target_field:
                fallback_stmt = select(
                    concept_aliases_table.c.concept_uri,
                    concept_aliases_table.c.wikidata_qid,
                ).where(concept_aliases_table.c.folded_label.in_(search_tokens)).order_by(
                    concept_aliases_table.c.is_preferred.desc(),
                    concept_aliases_table.c.id.asc(),
                ).limit(1)
                row = conn.execute(fallback_stmt).first()

            if not row:
                return None, None

            concept_uri = str(row[0])
            wikidata_qid = str(row[1]) if row[1] is not None else None
            concept = self.find_concept_by_uri(concept_uri)
            return concept, wikidata_qid

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

        with self.engine.begin() as conn:
            conn.execute(
                delete(concept_aliases_table).where(
                    concept_aliases_table.c.source.in_(["vocab", "concept_mapping"])
                )
            )

            # 1. From concepts and concept_labels
            stmt = select(
                concepts_table.c.uri,
                concepts_table.c.vocabulary,
                concepts_table.c.target_field,
                concept_labels_table.c.lang,
                concept_labels_table.c.label_type,
                concept_labels_table.c.label,
            ).select_from(
                concepts_table.join(concept_labels_table, concepts_table.c.uri == concept_labels_table.c.concept_uri)
            ).where(concepts_table.c.is_deprecated == 0)

            rows = conn.execute(stmt).mappings().all()
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
            m_stmt = select(
                concept_mappings_table.c.target_uri,
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
