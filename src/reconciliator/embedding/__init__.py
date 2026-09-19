"""Embedding package: record serialisation templates and SentenceTransformer model wrapper."""

from __future__ import annotations

from reconciliator.embedding.embedder import SentenceEmbedder
from reconciliator.embedding.template import RecordTemplateEngine, WEMISerializationEngine

__all__ = [
    "RecordTemplateEngine",
    "SentenceEmbedder",
    "WEMISerializationEngine",
]

