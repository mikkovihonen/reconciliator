"""Typo-tolerant and token-set fuzzy string similarity scorer."""

from __future__ import annotations

from rapidfuzz import fuzz


class FuzzyScorer:
    """Fast typo-tolerant string similarity scorer using RapidFuzz token-set ratio."""

    def score(self, query: str, document: str) -> float:
        """Compute normalized token-set ratio similarity in [0.0, 1.0]."""
        if not query or not document:
            return 0.0
        ratio = fuzz.token_set_ratio(query, document)
        return max(0.0, min(1.0, float(ratio) / 100.0))

    def score_batch(self, query: str, documents: list[str]) -> list[float]:
        """Compute normalized similarity for a query across documents."""
        if not query:
            return [0.0] * len(documents)
        return [self.score(query, doc) for doc in documents]
