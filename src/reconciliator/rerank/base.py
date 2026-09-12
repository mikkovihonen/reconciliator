"""Abstract protocol and base definitions for text-pair reranking scorers."""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class RerankScorer(Protocol):
    """Unified interface protocol for candidate reranking scoring engines."""

    def score(self, query: str, document: str) -> float:
        """
        Compute similarity score in [0.0, 1.0] for a query-document pair.
        Higher score indicates closer match.
        """
        ...

    def score_batch(self, query: str, documents: list[str]) -> list[float]:
        """
        Compute similarity scores for a query against a list of candidate documents.
        Returns a list of scores in [0.0, 1.0].
        """
        ...
