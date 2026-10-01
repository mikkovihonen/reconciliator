"""Neural Cross-Encoder reranking scorer based on SentenceTransformers."""

from __future__ import annotations

import math
from typing import Any, Literal, Optional

import torch


from reconciliator.utils.device import resolve_device


def sigmoid(x: float) -> float:
    """Compute sigmoid of x."""
    try:
        return 1.0 / (1.0 + math.exp(-x))
    except OverflowError:
        return 0.0 if x < 0 else 1.0


def normalize_cross_encoder_score(raw_score: float) -> float:
    """
    Ensure the score is properly bounded in [0.0, 1.0].
    If raw_score is already within [0.0, 1.0], it is kept as-is.
    If outside (e.g. uncalibrated logits from MS-MARCO), sigmoid is applied.
    """
    if 0.0 <= raw_score <= 1.0:
        return raw_score
    return max(0.0, min(1.0, sigmoid(raw_score)))


class CrossEncoderScorer:
    """
    Neural text-pair similarity scorer using full cross-attention Transformer.
    Uses lazy model loading to avoid startup latency when another engine is active.
    """

    def __init__(
        self,
        model_name: str = "cross-encoder/ms-marco-MiniLM-L-6-v2",
        device: Literal["auto", "cpu", "cuda", "mps"] = "auto",
        batch_size: int = 16,
        model: Optional[Any] = None,
    ) -> None:
        self.model_name = model_name
        self.device = resolve_device(device)
        self.batch_size = batch_size
        self._model = model

    def _get_model(self) -> Any:
        if self._model is None:
            from sentence_transformers import CrossEncoder

            self._model = CrossEncoder(self.model_name, device=self.device)
        return self._model

    def score(self, query: str, document: str) -> float:
        """Compute similarity score for a single query-document pair in [0.0, 1.0]."""
        if not query or not document:
            return 0.0
        model = self._get_model()
        pred = model.predict([[query, document]])
        raw = float(pred[0]) if hasattr(pred, "__len__") else float(pred)
        return normalize_cross_encoder_score(raw)

    def score_batch(self, query: str, documents: list[str]) -> list[float]:
        """Compute similarity scores for query across candidate documents."""
        if not query or not documents:
            return [0.0] * len(documents)

        pairs = [[query, doc] for doc in documents]
        model = self._get_model()
        preds = model.predict(pairs, batch_size=self.batch_size)
        return [normalize_cross_encoder_score(float(p)) for p in preds]
