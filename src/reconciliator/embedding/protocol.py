"""Abstract protocol and base class for vector embedders in Reconciliator."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, Optional, Protocol, runtime_checkable

import numpy as np

if TYPE_CHECKING:
    from reconciliator.judge.protocol import TargetEncoding
    from reconciliator.models import BibliographicRecord


@runtime_checkable
class VectorEmbedder(Protocol):
    """Unified protocol for dense vector embedding models.

    Defines the contract required across offline vector index builders,
    online lexical/semantic hybrid search endpoints, funnel matching tiers,
    and unsupervised clustering engines (such as BigClust Metal).
    """

    @property
    def dimension(self) -> int:
        """Embedding dimension (number of float32 dimensions per vector)."""
        ...

    def embed_texts(
        self, texts: Sequence[str], batch_size: Optional[int] = None
    ) -> np.ndarray:
        """Generate dense L2-normalized embeddings for a sequence of texts.

        Returns an (N, dimension) float32 numpy array.
        """
        ...

    def embed_records(
        self, records: Sequence[BibliographicRecord], batch_size: Optional[int] = None
    ) -> np.ndarray:
        """Serialize and encode a sequence of BibliographicRecord models.

        Returns an (N, dimension) float32 numpy array.
        """
        ...

    def serialize_record(self, record: BibliographicRecord | dict[str, Any]) -> str:
        """Convert a bibliographic record or dict into descriptive text representation."""
        ...

    def encode_batch_dense(self, texts: Sequence[str]) -> np.ndarray:
        """Batch dense encoding for vector index ingestion (protocol parity with LLMJudgeBackend)."""
        ...

    def encode_target(self, text: str, retain_cache: bool = True) -> Any:
        """Single-pass target encoding for coarse retrieval (protocol parity with LLMJudgeBackend)."""
        ...

    def embed_query(self, query: str) -> np.ndarray:
        """Embed a single search query string into a dense 1D vector of shape (dimension,)."""
        ...


class BaseVectorEmbedder(ABC, VectorEmbedder):
    """Abstract base class providing standard boilerplate implementations for vector embedders.

    Subclasses must implement:
      - dimension (property)
      - embed_texts(texts, batch_size=None)
      - serialize_record(record)
    """

    @property
    @abstractmethod
    def dimension(self) -> int:
        """Embedding dimension."""
        ...

    @abstractmethod
    def embed_texts(
        self, texts: Sequence[str], batch_size: Optional[int] = None
    ) -> np.ndarray:
        """Generate dense L2-normalized embeddings for a sequence of texts."""
        ...

    @abstractmethod
    def serialize_record(self, record: BibliographicRecord | dict[str, Any]) -> str:
        """Convert a record into descriptive text representation."""
        ...

    def embed_records(
        self, records: Sequence[BibliographicRecord], batch_size: Optional[int] = None
    ) -> np.ndarray:
        """Serialize and encode a sequence of BibliographicRecord models."""
        texts = [self.serialize_record(r) for r in records]
        return self.embed_texts(texts, batch_size=batch_size)

    def encode_batch_dense(self, texts: Sequence[str]) -> np.ndarray:
        """Batch dense encoding for vector index ingestion."""
        return self.embed_texts(texts)

    def embed_query(self, query: str) -> np.ndarray:
        """Embed a single search query string into a dense 1D vector of shape (dimension,)."""
        vecs = self.embed_texts([query])
        if len(vecs) > 0:
            return vecs[0]
        return np.zeros(self.dimension, dtype=np.float32)

    def encode_target(self, text: str, retain_cache: bool = True) -> TargetEncoding:
        """Single-pass target encoding for coarse retrieval (protocol parity with LLMJudgeBackend)."""
        from reconciliator.judge.protocol import TargetEncoding

        dense_vec = self.embed_query(text)
        return TargetEncoding(
            dense_vector=dense_vec,
            kv_cache=None,
            base_seq_len=0,
        )
