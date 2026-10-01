"""Legacy concept mapping and vocabulary migration mixin for VocabStore."""

from __future__ import annotations

import functools
import logging
from typing import TYPE_CHECKING, Optional, Protocol

from sqlalchemy import func, insert, select

from reconciliator.database import (
    calculate_batch_size,
    concept_mapping_labels_table,
    concept_mappings_table,
)
from reconciliator.models import ConceptMapping
from reconciliator.utils.curie import parse_curie
from reconciliator.vocab.detection import (
    detect_vocabulary,
    extract_concept_id,
    normalize_concept_id,
)

if TYPE_CHECKING:
    from sqlalchemy import Engine

    from reconciliator.config import VocabulariesConfig

logger = logging.getLogger(__name__)


class LegacyMappingProtocol(Protocol):
    """Protocol for legacy mapping dependencies."""

    engine: Engine
    config: VocabulariesConfig

    def clear_caches(self) -> None: ...
    def find_legacy_mapping(self, legacy_vocab: str, legacy_id: str) -> Optional[ConceptMapping]: ...


class LegacyMappingMixin:
    """Manages legacy vocabulary migration mappings and lookups by legacy ID, URI, or label."""

    def insert_mappings(
        self: LegacyMappingProtocol, mappings: list[ConceptMapping]
    ) -> None:
        """Bulk insert legacy mappings into concept_mappings and concept_mapping_labels."""
        if not mappings:
            return

        mapping_batch_size = calculate_batch_size(num_columns=len(concept_mappings_table.c))
        label_batch_size = calculate_batch_size(num_columns=len(concept_mapping_labels_table.c))

        with self.engine.begin() as conn:
            for i in range(0, len(mappings), mapping_batch_size):
                chunk_m = mappings[i : i + mapping_batch_size]
                chunk_rows = [
                    {
                        "legacy_vocab": m.legacy_vocab,
                        "legacy_id": m.legacy_id,
                        "target_vocab": m.target_vocab,
                        "target_id": m.target_id,
                        "mapping_type": m.mapping_type,
                        "target_field": m.target_field,
                    }
                    for m in chunk_m
                ]
                stmt = (
                    insert(concept_mappings_table)
                    .values(chunk_rows)
                    .returning(concept_mappings_table.c.id)
                )
                res = conn.execute(stmt)
                m_ids = [row[0] for row in res.fetchall()]

                label_rows = []
                for m_id, m in zip(m_ids, chunk_m):
                    for lang, lbl in m.legacy_labels.items():
                        if lbl and lbl.strip():
                            label_rows.append(
                                {"mapping_id": m_id, "lang": lang, "label": lbl.strip()}
                            )

                if label_rows:
                    for l_idx in range(0, len(label_rows), label_batch_size):
                        conn.execute(
                            insert(concept_mapping_labels_table),
                            label_rows[l_idx : l_idx + label_batch_size],
                        )

        self.clear_caches()

    @functools.lru_cache(maxsize=16384)
    def find_legacy_mapping(
        self: LegacyMappingProtocol, legacy_vocab: str, legacy_id: str
    ) -> Optional[ConceptMapping]:
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
    def find_legacy_mapping_by_uri(
        self: LegacyMappingProtocol, uri: str
    ) -> Optional[ConceptMapping]:
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
        self: LegacyMappingProtocol,
        label: str,
        legacy_vocab: Optional[str] = None,
        lang: Optional[str] = None,
    ) -> Optional[ConceptMapping]:
        """Look up migration mapping by legacy label."""
        effective_lang = lang or getattr(self.config, "default_language", None) or ""
        clean_lbl = label.strip().lower()
        if legacy_vocab:
            clean_leg = legacy_vocab.strip().lower()
            legacy_sources = getattr(self.config, "legacy_to_modern", None)
            if legacy_sources is not None and clean_leg not in legacy_sources:
                return None
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
