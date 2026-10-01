"""Semantic concept vector indexing and neural similarity resolution mixin for VocabStore."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Optional, Protocol

from sqlalchemy import select

from reconciliator.database import concept_labels_table, concepts_table
from reconciliator.models import VocabConcept
from reconciliator.utils.curie import to_curie
from reconciliator.vocab.detection import concept_id_to_uri

if TYPE_CHECKING:
    from sqlalchemy import Engine

logger = logging.getLogger(__name__)


class SemanticVocabProtocol(Protocol):
    """Protocol for semantic vocab store dependencies."""

    engine: Engine

    def resolve_concept_alias(
        self, label: str, vocabulary: Optional[str] = None, target_field: Optional[str] = None
    ) -> Optional[dict[str, Any]]: ...
    def find_concept_by_id(self, vocabulary: str, concept_id: str) -> Optional[VocabConcept]: ...


class SemanticVocabMixin:
    """Manages dense vector index construction for concepts and semantic ANN subject resolution."""

    def build_concept_index(
        self: SemanticVocabProtocol,
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
        self: SemanticVocabProtocol,
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
