"""No-op identity reranking scorer for pure dense retrieval."""

from __future__ import annotations


class NoOpScorer:
    """Identity scorer that returns 1.0 (or neutral pass-through score)."""

    def score(self, query: str, document: str) -> float:
        """Return 1.0."""
        return 1.0

    def score_batch(self, query: str, documents: list[str]) -> list[float]:
        """Return list of 1.0 scores."""
        return [1.0] * len(documents)

