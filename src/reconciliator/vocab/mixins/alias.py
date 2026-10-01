"""Concept alias indexing, resolution, and table rebuild mixin for VocabStore."""

from __future__ import annotations

import functools
import logging
from typing import TYPE_CHECKING, Any, Optional, Protocol

from sqlalchemy import and_, case, delete, insert, select

from reconciliator.database import (
    calculate_batch_size,
    concept_aliases_table,
    concept_labels_table,
    concept_mapping_labels_table,
    concept_mappings_table,
    concepts_table,
)
from reconciliator.models import VocabConcept
from reconciliator.normalizers.linguistic import fold_label
from reconciliator.utils.curie import extract_wikidata_qid, parse_curie, to_curie

if TYPE_CHECKING:
    from sqlalchemy import Engine

    from reconciliator.config import LinguisticConfig, VocabulariesConfig
    from reconciliator.vocab.store import VocabMemoryIndex

logger = logging.getLogger(__name__)


class AliasStoreProtocol(Protocol):
    """Protocol for alias store dependencies."""

    engine: Engine
    config: VocabulariesConfig
    linguistic: LinguisticConfig
    _index: VocabMemoryIndex

    def clear_caches(self) -> None: ...
    def find_concept_by_id(self, vocabulary: str, concept_id: str) -> Optional[VocabConcept]: ...
    def find_concept_by_uri(self, uri: str) -> Optional[VocabConcept]: ...
    def insert_aliases(self, aliases: list[dict[str, Any]]) -> None: ...


class AliasStoreMixin:
    """Manages concept alias insertion, folded alias resolution, and alias index rebuilding."""

    def insert_aliases(self: AliasStoreProtocol, aliases: list[dict[str, Any]]) -> None:
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
        alias_batch_size = calculate_batch_size(num_columns=len(concept_aliases_table.c))
        with self.engine.begin() as conn:
            for i in range(0, len(rows), alias_batch_size):
                conn.execute(insert(concept_aliases_table), rows[i : i + alias_batch_size])
        self.clear_caches()

    @functools.lru_cache(maxsize=65536)
    def find_concept_by_alias(
        self: AliasStoreProtocol,
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
                preserved_diacritics=self.linguistic.preserved_diacritics,
            )
            or clean.lower()
        )

        search_tokens = [clean_folded]
        if clean != clean_folded:
            search_tokens.append(clean)

        if self._index.is_preloaded and self._index.alias_index:
            for tok in search_tokens:
                hit = None
                if target_field:
                    hit = self._index.alias_index.get((tok, target_field))
                if not hit:
                    hit = self._index.alias_index.get((tok, None))
                if hit:
                    voc, c_id, source_val = hit
                    if vocabulary and voc != vocabulary.lower():
                        continue
                    concept = self.find_concept_by_id(voc, c_id)
                    concept_curie = to_curie(voc, c_id)
                    if not concept:
                        concept = self.find_concept_by_uri(concept_curie)
                    wikidata_qid = extract_wikidata_qid(concept_curie) or extract_wikidata_qid(
                        source_val
                    )
                    return concept, wikidata_qid
            return None, None

        priority_order: list[str] = []
        if target_field and target_field in getattr(self.config, "default_field_vocabularies", {}):
            priority_order.append(self.config.default_field_vocabularies[target_field].lower())
        for voc in getattr(self.config, "subject_priority", []):
            v_clean = voc.lower()
            if v_clean not in priority_order:
                priority_order.append(v_clean)

        order_clauses: list[Any] = [concept_aliases_table.c.is_preferred.desc()]
        if priority_order:
            rank_case = case(
                *[
                    (concept_aliases_table.c.vocabulary == voc, idx)
                    for idx, voc in enumerate(priority_order)
                ],
                else_=len(priority_order) + 1,
            )
            order_clauses.append(rank_case.asc())
        order_clauses.append(concept_aliases_table.c.id.asc())

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
                .order_by(*order_clauses)
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
                    .order_by(*order_clauses)
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
        self: AliasStoreProtocol,
        label: str,
        vocabulary: Optional[str] = None,
        target_field: Optional[str] = None,
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

    def rebuild_aliases_from_concepts(
        self: AliasStoreProtocol, legacy_vocabs: Optional[set[str]] = None
    ) -> int:
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
