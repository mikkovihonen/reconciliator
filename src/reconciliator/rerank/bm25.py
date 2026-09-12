"""BM25 lexical reranking scorer."""

from __future__ import annotations

import re

from rank_bm25 import BM25Okapi


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
        query_tokens = tokenize_text(query)
        doc_tokens = tokenize_text(document)

        if not query_tokens or not doc_tokens:
            return 0.0

        bm25 = BM25Okapi([doc_tokens], k1=self.k1, b=self.b)
        # In a single-pair context, fix term weights to positive unity to leverage
        # BM25 term-frequency saturation and length normalization without negative IDF singularity
        for term in bm25.idf:
            bm25.idf[term] = 1.0

        raw_score = float(bm25.get_scores(query_tokens)[0])
        max_possible = float(bm25.get_scores(doc_tokens)[0])

        if max_possible > 0:
            norm_score = raw_score / max_possible
        else:
            norm_score = 0.0

        return max(0.0, min(1.0, float(norm_score)))

    def score_batch(self, query: str, documents: list[str]) -> list[float]:
        """Compute normalized BM25 lexical similarity for a query across candidate documents."""
        if not query or not documents:
            return [0.0] * len(documents)
        return [self.score(query, doc) for doc in documents]
