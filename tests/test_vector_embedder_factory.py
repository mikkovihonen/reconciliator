"""Unit tests for create_vector_embedder factory and protocol parity."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from reconciliator.config import AppConfig, EmbeddingConfig, LLMJudgeConfig
from reconciliator.embedding.embedder import SentenceEmbedder
from reconciliator.embedding.factory import create_vector_embedder
from reconciliator.judge.protocol import LLMJudgeBackend, TargetEncoding
from reconciliator.judge.torch_backend import PytorchJudgeBackend


def test_factory_creates_sentence_embedder() -> None:
    """When AppConfig has vector_model='embedding', create_vector_embedder creates SentenceEmbedder."""
    app_cfg = AppConfig.model_validate(
        {
            "vector_model": "embedding",
            "embedding": {"model_name": "mock-model", "batch_size": 32},
        }
    )
    with patch("reconciliator.embedding.factory.SentenceEmbedder") as mock_emb_cls:
        mock_instance = MagicMock(spec=SentenceEmbedder)
        mock_emb_cls.return_value = mock_instance

        embedder = create_vector_embedder(app_cfg)
        assert embedder == mock_instance
        mock_emb_cls.assert_called_once_with(app_cfg.embedding)


def test_factory_creates_llm_judge_backend() -> None:
    """When AppConfig has vector_model='llm', create_vector_embedder creates LLMJudgeBackend."""
    app_cfg = AppConfig.model_validate(
        {
            "vector_model": "llm",
            "llm": {"model_name": "mock-llm", "backend": "torch"},
        }
    )
    with patch("reconciliator.judge.factory.create_judge_backend") as mock_judge_factory:
        mock_instance = MagicMock(spec=LLMJudgeBackend)
        mock_judge_factory.return_value = mock_instance

        embedder = create_vector_embedder(app_cfg)
        assert embedder == mock_instance
        mock_judge_factory.assert_called_once_with(app_cfg.llm)


def test_factory_raises_when_invalid_vector_model() -> None:
    """create_vector_embedder must raise ValueError if vector_model is invalid."""
    mock_cfg = MagicMock(spec=AppConfig)
    mock_cfg.vector_model = "unsupported"

    with pytest.raises(ValueError, match="Invalid vector_model setting"):
        create_vector_embedder(mock_cfg)


def test_sentence_embedder_protocol_parity() -> None:
    """SentenceEmbedder must provide encode_batch_dense and encode_target matching LLMJudgeBackend protocol."""
    cfg = EmbeddingConfig(model_name="mock-model")
    with patch("reconciliator.embedding.embedder.SentenceTransformer") as mock_st_cls:
        mock_st = MagicMock()
        mock_st.get_embedding_dimension.return_value = 128
        fake_vector = np.ones((1, 128), dtype=np.float32) / np.sqrt(128)
        mock_st.encode.return_value = fake_vector
        mock_st_cls.return_value = mock_st

        embedder = SentenceEmbedder(cfg)
        assert embedder.dimension == 128

        # 1. encode_batch_dense parity
        dense_batch = embedder.encode_batch_dense(["Record title"])
        assert isinstance(dense_batch, np.ndarray)
        assert dense_batch.shape == (1, 128)

        # 2. encode_target parity
        target_enc = embedder.encode_target("Target query", retain_cache=True)
        assert isinstance(target_enc, TargetEncoding)
        assert target_enc.dense_vector.shape == (128,)
        assert target_enc.kv_cache is None
        assert target_enc.base_seq_len == 0


def test_llm_judge_backend_protocol_parity() -> None:
    """PytorchJudgeBackend must provide embed_texts matching SentenceEmbedder protocol."""
    cfg = LLMJudgeConfig(model_name="mock-llm", device="cpu")
    backend = PytorchJudgeBackend(cfg, model=MagicMock(), tokenizer=MagicMock())

    with patch.object(backend, "encode_batch_dense") as mock_encode_batch:
        mock_encode_batch.return_value = np.zeros((2, 64), dtype=np.float32)
        res = backend.embed_texts(["doc1", "doc2"], batch_size=8)
        assert res.shape == (2, 64)
        mock_encode_batch.assert_called_once_with(["doc1", "doc2"])


def test_vector_embedder_runtime_protocol_conformance() -> None:
    """Both SentenceEmbedder and PytorchJudgeBackend must conform to VectorEmbedder at runtime."""
    from reconciliator.embedding.protocol import BaseVectorEmbedder, VectorEmbedder
    from reconciliator.models import BibliographicRecord

    # 1. LLMJudgeBackend is a subprotocol of VectorEmbedder
    assert VectorEmbedder in LLMJudgeBackend.__mro__
    assert issubclass(SentenceEmbedder, BaseVectorEmbedder)

    # 2. SentenceEmbedder conforms to VectorEmbedder and inherits BaseVectorEmbedder
    emb_cfg = EmbeddingConfig(model_name="mock-model")
    with patch("reconciliator.embedding.embedder.SentenceTransformer") as mock_st_cls:
        mock_st = MagicMock()
        mock_st.get_embedding_dimension.return_value = 128
        mock_st.encode.return_value = np.ones((1, 128), dtype=np.float32)
        mock_st_cls.return_value = mock_st

        sent_embedder = SentenceEmbedder(emb_cfg)
        assert isinstance(sent_embedder, VectorEmbedder)
        assert isinstance(sent_embedder, BaseVectorEmbedder)
        assert sent_embedder.dimension == 128
        q_vec = sent_embedder.embed_query("query")
        assert q_vec.shape == (128,)

    # 3. PytorchJudgeBackend conforms to VectorEmbedder
    judge_cfg = LLMJudgeConfig(model_name="mock-llm", device="cpu")
    backend = PytorchJudgeBackend(judge_cfg, model=MagicMock(), tokenizer=MagicMock())
    backend._dimension = 64

    with patch.object(backend, "encode_batch_dense") as mock_encode_dense:
        mock_encode_dense.return_value = np.ones((1, 64), dtype=np.float32)

        assert isinstance(backend, VectorEmbedder)
        assert backend.dimension == 64

        q_vec = backend.embed_query("sample query")
        assert q_vec.shape == (64,)

        rec = BibliographicRecord(source_id="1", source="melinda", title="Tuntematon sotilas")
        serialized = backend.serialize_record(rec)
        assert isinstance(serialized, str)
        assert len(serialized) > 0

        rec_embeddings = backend.embed_records([rec])
        assert rec_embeddings.shape == (1, 64)

