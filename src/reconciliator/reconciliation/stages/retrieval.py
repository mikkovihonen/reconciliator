"""Candidate retrieval stage for dense vector search and full-text search fallback."""

from __future__ import annotations

from typing import Callable, Optional

import numpy as np

from reconciliator.config import ReconcileConfig, ReconcileFTSFallbackConfig
from reconciliator.database import WEMIRepository
from reconciliator.embedding.protocol import VectorEmbedder
from reconciliator.models import (
    ExpressionEntity,
    MatchRecord,
    ReconciliationMatch,
    WorkEntity,
)
from reconciliator.reconciliation.results import WEMIMatchResult


class CandidateRetrievalStage:
    """Manages multi-index candidate retrieval (dense semantic vectors and full-text lexical search)."""

    def __init__(
        self,
        repo: WEMIRepository,
        embedder: Optional[VectorEmbedder] = None,
        config: Optional[ReconcileConfig] = None,
        authoritative_only: bool = True,
        rank_candidates_fn: Optional[
            Callable[..., list[tuple[WorkEntity, ReconciliationMatch]]]
        ] = None,
        align_expression_fn: Optional[Callable[..., Optional[ExpressionEntity]]] = None,
    ) -> None:
        self.repo = repo
        self.embedder = embedder
        self.config = config
        self.authoritative_only = authoritative_only
        self.rank_candidates_fn = rank_candidates_fn
        self.align_expression_fn = align_expression_fn

    def retrieval_top_k(self, default: int = 50) -> int:
        """How many candidates the dense tiers retrieve, from `rerank.top_k`."""
        rerank = getattr(self.config, "rerank", None)
        value = getattr(rerank, "top_k", None)
        return int(value) if value else default

    def vector_or_fts_fallback(
        self,
        target: MatchRecord,
        work_type: Optional[str] = None,
        funnel_name: str = "fallback",
    ) -> Optional[WEMIMatchResult]:
        """Perform semantic vector search fallback or FTS lexical search."""
        # 1. Dense vector search
        if (
            self.embedder is not None
            and self.repo.work_indexer is not None
            and self.repo.work_indexer.total_vectors > 0
        ):
            query_text = target.serialized_work or f"{target.title} {target.author_key or ''}"
            is_llm_judge = (
                self.config is not None
                and hasattr(self.config, "rerank")
                and getattr(self.config.rerank, "enabled", False)
                and getattr(self.config.rerank, "engine", "bm25") in ("llm_judge", "cascade")
            )
            t_enc = None
            if hasattr(self.embedder, "encode_target"):
                t_enc = self.embedder.encode_target(query_text, retain_cache=is_llm_judge)
                query_vec = t_enc.dense_vector
                if len(query_vec.shape) == 1:
                    query_vec = query_vec[np.newaxis, :]
            elif hasattr(self.repo, "embed_texts_cached"):
                query_vec = self.repo.embed_texts_cached(self.embedder, [query_text], entity_type="work")
            elif hasattr(self.embedder, "encode_batch_dense"):
                query_vec = self.embedder.encode_batch_dense([query_text])
            else:
                query_vec = self.embedder.embed_texts([query_text])

            results = self.repo.search_work_vectors(
                query_vec,
                top_k=self.retrieval_top_k(),
                authoritative_only=self.authoritative_only,
            )
            thresh = (
                self.config.threshold
                if hasattr(self.config, "threshold") and self.config.threshold is not None
                else 0.70
            )
            typed = [
                (cand_work, score)
                for cand_work, score in results
                if not work_type or cand_work.work_type == work_type
            ]
            if self.rank_candidates_fn is not None:
                ranked = self.rank_candidates_fn(
                    target, typed, acceptance_threshold=thresh, target_encoding=t_enc
                )
                for cand_work, verdict in ranked:
                    if not verdict.accepted:
                        continue
                    exps = self.repo.get_expressions_for_work(cand_work.id)
                    exp = (
                        self.align_expression_fn(
                            exps,
                            target.language,
                            translator_signature=target.get_first_key("translators_signature"),
                        )
                        if self.align_expression_fn is not None
                        else (exps[0] if exps else None)
                    )
                    return WEMIMatchResult(
                        target_record_id=target.record_id,
                        matched_work=cand_work,
                        matched_expression=exp,
                        work_uri=cand_work.work_uri,
                        expression_uri=exp.expression_uri if exp else None,
                        match_tier="tier2_vector_fallback",
                        funnel_used=funnel_name,
                        confidence_score=float(verdict.match_score),
                    )

        # 2. PostgreSQL Full-Text Search (Tier 3 Fallback)
        fts_cfg = (
            self.config.fts_fallback
            if (self.config and hasattr(self.config, "fts_fallback"))
            else ReconcileFTSFallbackConfig()
        )
        if fts_cfg.enabled:
            fts_query = target.title_stem or target.title
            if fts_query:
                try:
                    fts_results = self.repo.search_works_fts(
                        fts_query, limit=fts_cfg.limit, authoritative_only=self.authoritative_only
                    )
                    for cand_work, _rank in fts_results:
                        if work_type and cand_work.work_type != work_type:
                            continue
                        exps = self.repo.get_expressions_for_work(cand_work.id)
                        exp = (
                            self.align_expression_fn(
                                exps,
                                target.language,
                                translator_signature=target.get_first_key("translators_signature"),
                            )
                            if self.align_expression_fn is not None
                            else (exps[0] if exps else None)
                        )
                        return WEMIMatchResult(
                            target_record_id=target.record_id,
                            matched_work=cand_work,
                            matched_expression=exp,
                            work_uri=cand_work.work_uri,
                            expression_uri=exp.expression_uri if exp else None,
                            match_tier="tier3_fts_fallback",
                            funnel_used=funnel_name,
                            confidence_score=fts_cfg.confidence_score,
                        )
                except Exception:
                    pass

        return None
