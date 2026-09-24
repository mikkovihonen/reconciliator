"""Abstract protocol and data structures for autoregressive LLM Judge engines."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional, Protocol, runtime_checkable

import numpy as np


@dataclass
class TargetEncoding:
    """Output of single-pass encoding for a target entity."""

    dense_vector: np.ndarray
    kv_cache: Optional[Any]
    base_seq_len: int
    prior_bias: float = 0.0


@dataclass
class CandidateEvaluation:
    """Result of evaluating a candidate entity suffix."""

    is_match: bool
    confidence_score: float
    raw_token: str
    calibrated_margin: float = 0.0
    rationale: Optional[str] = None


@runtime_checkable
class LLMJudgeBackend(Protocol):
    """Unified interface for hardware-native autoregressive LLM Judge backends."""

    @property
    def dimension(self) -> int:
        """Embedding dimension."""
        ...

    def encode_target(self, text: str, retain_cache: bool = True) -> TargetEncoding:
        """Process target record once to extract dense representation and base KV-cache."""
        ...

    def encode_batch_dense(self, texts: list[str]) -> np.ndarray:
        """Batch dense encoding for vector index ingestion."""
        ...

    def embed_texts(self, texts: list[str], batch_size: Optional[int] = None) -> np.ndarray:
        """Batch dense encoding (SentenceEmbedder protocol parity)."""
        ...

    def evaluate_candidate(
        self,
        base_cache: Any,
        base_seq_len: int,
        candidate_suffix: str,
        prior_bias: float = 0.0,
    ) -> CandidateEvaluation:
        """Evaluate candidate suffix, decode decision, and truncate cache back to base_seq_len."""
        ...

    def truncate_kv_cache(self, kv_cache: Any, base_seq_len: int) -> None:
        """Zero-copy in-place truncation of key/value tensors back to base_seq_len."""
        ...
