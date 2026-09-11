"""Vector indexers and engine backends for dense semantic search."""

from __future__ import annotations

from reconciliator.indexer.base import VectorIndex
from reconciliator.indexer.factory import create_vector_engine
from reconciliator.indexer.faiss_engine import FAISSIndexEngine
from reconciliator.indexer.torch_engine import TorchVectorEngine

# Alias for compatibility with previous FAISSIndexManager class name if referenced
FAISSIndexManager = FAISSIndexEngine

__all__ = [
    "FAISSIndexEngine",
    "FAISSIndexManager",
    "TorchVectorEngine",
    "VectorIndex",
    "create_vector_engine",
]

