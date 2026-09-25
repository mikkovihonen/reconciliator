"""BM25 lexical reranking scorer backed by Rust reconciliator_core."""

from __future__ import annotations

import re

from reconciliator._core.rerank import py_bm25_score_batch


def tokenize_text(text: str) -> list[str]:
    """Tokenize text into lowercase alphanumeric tokens."""
    if not text:
        return []
    return re.findall(r"\w+", text.lower())


class BM25Scorer:
    """Lexical text-pair similarity scorer based on Okapi BM25 with term saturation."""

    def __init__(self, k1: float = 1.5, b: float = 0.75) -> None:
        self.k1 = k1
        self.b = b

    def score(self, query: str, document: str) -> float:
        """Compute normalized BM25 lexical similarity in [0.0, 1.0]."""
        if not query or not document:
            return 0.0
        scores = py_bm25_score_batch(query, [document], k1=self.k1, b=self.b)
        return scores[0] if scores else 0.0

    def score_batch(self, query: str, documents: list[str]) -> list[float]:
        """Compute normalized BM25 lexical similarity for a query across candidate documents."""
        if not query or not documents:
            return [0.0] * len(documents)
        return py_bm25_score_batch(query, list(documents), k1=self.k1, b=self.b)
