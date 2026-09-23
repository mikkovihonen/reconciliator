"""Factory function for instantiating reranking scorers."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Optional

from reconciliator.rerank.base import RerankScorer
from reconciliator.rerank.bm25 import BM25Scorer
from reconciliator.rerank.cross_encoder import CrossEncoderScorer
from reconciliator.rerank.fuzzy import FuzzyScorer
from reconciliator.rerank.noop import NoOpScorer

if TYPE_CHECKING:
    from reconciliator.config import RerankConfig


def create_rerank_scorer(
    config: RerankConfig, judge_backend: Optional[Any] = None
) -> RerankScorer:
    """Instantiate a RerankScorer implementation based on configuration."""
    engine = config.engine.lower().strip()
    if engine == "cascade":
        from reconciliator.rerank.cascade import CascadeScorer
        from reconciliator.rerank.llm_judge import LLMJudgeScorer

        heur_type = config.cascade.heuristic_engine.lower().strip()
        if heur_type == "bm25":
            heuristic_scorer: RerankScorer = BM25Scorer()
        elif heur_type == "fuzzy":
            heuristic_scorer = FuzzyScorer()
        elif heur_type == "cross_encoder":
            model_name = config.model_name or "cross-encoder/ms-marco-MiniLM-L-6-v2"
            heuristic_scorer = CrossEncoderScorer(
                model_name=model_name,
                device=config.device,
                batch_size=config.batch_size,
            )
        elif heur_type == "noop":
            heuristic_scorer = NoOpScorer()
        else:
            raise ValueError(f"Unknown heuristic engine for cascade: '{heur_type}'")

        judge_scorer = LLMJudgeScorer(backend=judge_backend, config=getattr(config, "judge", None))
        return CascadeScorer(
            heuristic_scorer=heuristic_scorer,
            judge_scorer=judge_scorer,
            config=config.cascade,
        )
    elif engine == "llm_judge":
        from reconciliator.rerank.llm_judge import LLMJudgeScorer

        return LLMJudgeScorer(backend=judge_backend, config=getattr(config, "judge", None))
    elif engine == "bm25":
        return BM25Scorer()
    elif engine == "cross_encoder":
        model_name = config.model_name or "cross-encoder/ms-marco-MiniLM-L-6-v2"
        return CrossEncoderScorer(
            model_name=model_name,
            device=config.device,
            batch_size=config.batch_size,
        )
    elif engine == "fuzzy":
        return FuzzyScorer()
    elif engine == "noop":
        return NoOpScorer()
    else:
        raise ValueError(
            f"Unknown rerank engine: '{config.engine}'. "
            f"Expected one of 'cascade', 'llm_judge', 'bm25', 'cross_encoder', 'fuzzy', 'noop'."
        )
