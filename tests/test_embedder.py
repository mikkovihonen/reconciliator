"""Unit tests for SentenceEmbedder with dynamic dimension discovery and metadata verification."""

from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from reconciliator.config import EmbeddingConfig
from reconciliator.embedding.embedder import SentenceEmbedder
from reconciliator.models import BibliographicRecord


@pytest.fixture
def mock_sentence_transformer() -> MagicMock:
    mock = MagicMock()
    mock.get_embedding_dimension.return_value = 384
    mock.get_sentence_embedding_dimension.return_value = 384
    mock.encode.side_effect = lambda texts, **kwargs: np.ones((len(texts), 384), dtype=np.float32)
    return mock


def test_embedder_dimension_discovery(mock_sentence_transformer: MagicMock) -> None:
    with patch(
        "reconciliator.embedding.embedder.SentenceTransformer",
        return_value=mock_sentence_transformer,
    ):
        config = EmbeddingConfig(device="cpu")
        embedder = SentenceEmbedder(config)

        assert embedder.dimension == 384
        assert embedder.template_hash is not None

        # Test embedding records
        rec = BibliographicRecord(
            source_id="REC1",
            source="melinda",
            title="Sinuhe",
        )
        embeddings = embedder.embed_records([rec])
        assert embeddings.shape == (1, 384)
        assert embeddings.dtype == np.float32


def test_embedder_empty_list(mock_sentence_transformer: MagicMock) -> None:
    with patch(
        "reconciliator.embedding.embedder.SentenceTransformer",
        return_value=mock_sentence_transformer,
    ):
        config = EmbeddingConfig(device="cpu")
        embedder = SentenceEmbedder(config)

        res = embedder.embed_texts([])
        assert res.shape == (0, 384)


def test_embedder_fp16_flag(mock_sentence_transformer: MagicMock) -> None:
    with patch(
        "reconciliator.embedding.embedder.SentenceTransformer",
        return_value=mock_sentence_transformer,
    ):
        config = EmbeddingConfig(device="cpu", batch_size=32)
        embedder = SentenceEmbedder(config)
        assert embedder.use_fp16 is False
        assert embedder.batch_size == 32


def test_embedder_vector_embedder_conformance(mock_sentence_transformer: MagicMock) -> None:
    """SentenceEmbedder must conform to VectorEmbedder protocol and support embed_query."""
    from reconciliator.embedding.protocol import VectorEmbedder

    with patch(
        "reconciliator.embedding.embedder.SentenceTransformer",
        return_value=mock_sentence_transformer,
    ):
        config = EmbeddingConfig(device="cpu")
        embedder = SentenceEmbedder(config)

        assert isinstance(embedder, VectorEmbedder)
        q_vec = embedder.embed_query("test query")
        assert isinstance(q_vec, np.ndarray)
        assert q_vec.shape == (384,)

