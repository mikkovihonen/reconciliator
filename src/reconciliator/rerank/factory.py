"""Factory function for instantiating reranking scorers."""

from __future__ import annotations

from typing import TYPE_CHECKING

from reconciliator.rerank.base import RerankScorer
from reconciliator.rerank.bm25 import BM25Scorer
from reconciliator.rerank.cross_encoder import CrossEncoderScorer
from reconciliator.rerank.fuzzy import FuzzyScorer
from reconciliator.rerank.noop import NoOpScorer

if TYPE_CHECKING:
    from reconciliator.config import RerankConfig


def create_rerank_scorer(config: RerankConfig) -> RerankScorer:
    """Instantiate a RerankScorer implementation based on configuration."""
    engine = config.engine.lower().strip()
    if engine == "bm25":
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
            f"Expected one of 'bm25', 'cross_encoder', 'fuzzy', 'noop'."
        )
