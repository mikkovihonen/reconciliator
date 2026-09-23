"""Unit tests for the autoregressive LLM Judge backend and zero-copy truncation."""

from __future__ import annotations

from unittest.mock import MagicMock

import numpy as np
import torch

from reconciliator.config import LLMJudgeConfig
from reconciliator.judge.factory import create_judge_backend
from reconciliator.judge.protocol import CandidateEvaluation, TargetEncoding
from reconciliator.judge.torch_backend import PytorchJudgeBackend


class MockDynamicLayer:
    """Mock DynamicLayer for testing cache truncation."""

    def __init__(self, keys: torch.Tensor, values: torch.Tensor) -> None:
        self.keys = keys
        self.values = values


class MockDynamicCache:
    """Mock DynamicCache for testing multi-layer KV cache slicing."""

    def __init__(self, num_layers: int = 4, seq_len: int = 20, hidden_dim: int = 64) -> None:
        self.layers = [
            MockDynamicLayer(
                keys=torch.randn(1, 4, seq_len, hidden_dim),
                values=torch.randn(1, 4, seq_len, hidden_dim),
            )
            for _ in range(num_layers)
        ]


def test_pytorch_backend_zero_copy_truncation() -> None:
    """Verify that truncate_kv_cache slices key/value views in-place back to base_seq_len."""
    config = LLMJudgeConfig(device="cpu")
    backend = PytorchJudgeBackend(config, model=MagicMock(), tokenizer=MagicMock())

    cache = MockDynamicCache(num_layers=4, seq_len=25, hidden_dim=32)
    base_seq_len = 10

    # Ensure initial shape
    for layer in cache.layers:
        assert layer.keys.shape == (1, 4, 25, 32)
        assert layer.values.shape == (1, 4, 25, 32)

    # Perform zero-copy in-place truncation
    backend.truncate_kv_cache(cache, base_seq_len=base_seq_len)

    # Assert sliced shape
    for layer in cache.layers:
        assert layer.keys.shape == (1, 4, base_seq_len, 32)
        assert layer.values.shape == (1, 4, base_seq_len, 32)


def test_pytorch_backend_encode_target_mocked() -> None:
    """Verify single-pass encoding generates normalized dense vector and preserves KV-cache."""
    config = LLMJudgeConfig(
        device="cpu",
        pooling_strategy="last_token",
        target_template="Target: {text}",
    )

    mock_tokenizer = MagicMock()
    mock_inputs = MagicMock()
    mock_inputs.input_ids = torch.tensor([[101, 200, 300]])
    mock_inputs.attention_mask = torch.tensor([[1, 1, 1]])
    mock_inputs.to.return_value = mock_inputs
    mock_tokenizer.return_value = mock_inputs

    # Mock model output: hidden states of shape [1, 3, 64]
    mock_output = MagicMock()
    fake_hidden = torch.randn(1, 3, 64)
    mock_output.hidden_states = [fake_hidden]
    mock_output.past_key_values = MockDynamicCache(seq_len=3, hidden_dim=64)

    mock_model = MagicMock()
    mock_model.return_value = mock_output
    mock_model.config.hidden_size = 64

    backend = PytorchJudgeBackend(config, model=mock_model, tokenizer=mock_tokenizer)
    assert backend.dimension == 64

    target_enc = backend.encode_target("Test record", retain_cache=True)
    assert isinstance(target_enc, TargetEncoding)
    assert target_enc.base_seq_len == 3
    assert target_enc.dense_vector.shape == (64,)
    assert target_enc.dense_vector.dtype == np.float32

    # L2 norm should be 1.0 within float precision
    norm = np.linalg.norm(target_enc.dense_vector)
    assert np.isclose(norm, 1.0, atol=1e-5)
    assert target_enc.kv_cache is not None


def test_pytorch_backend_evaluate_candidate_match() -> None:
    """Verify candidate evaluation with greedy decoding and automatic truncation."""
    config = LLMJudgeConfig(
        device="cpu",
        positive_token="YES",
        negative_token="NO",
    )

    mock_tokenizer = MagicMock()
    mock_tokenizer.encode.side_effect = lambda t, **kw: [10] if "YES" in t else [20]

    mock_suffix_inputs = MagicMock()
    mock_suffix_inputs.input_ids = torch.tensor([[501, 502]])
    mock_tokenizer.return_value.to.return_value = mock_suffix_inputs

    # Logits: YES (token 10) logit higher than NO (token 20)
    fake_logits = torch.zeros(1, 2, 100)
    fake_logits[0, -1, 10] = 5.0  # YES
    fake_logits[0, -1, 20] = 1.0  # NO

    mock_output = MagicMock()
    mock_output.logits = fake_logits

    mock_model = MagicMock()
    mock_model.return_value = mock_output
    mock_model.config.hidden_size = 64

    backend = PytorchJudgeBackend(config, model=mock_model, tokenizer=mock_tokenizer)
    cache = MockDynamicCache(seq_len=15, hidden_dim=64)

    # Evaluate candidate
    verdict = backend.evaluate_candidate(
        base_cache=cache,
        base_seq_len=10,
        candidate_suffix="Candidate: Sinuhe",
    )

    assert isinstance(verdict, CandidateEvaluation)
    assert verdict.is_match is True
    assert verdict.raw_token == "YES"
    assert verdict.confidence_score > 0.9  # sigmoid(5.0 - 1.0) = sigmoid(4.0) ~ 0.982

    # Verify cache was automatically truncated back to base_seq_len (10)
    for layer in cache.layers:
        assert layer.keys.shape[2] == 10
        assert layer.values.shape[2] == 10


def test_factory_backend_selection() -> None:
    """Test factory creates PytorchJudgeBackend when configured."""
    config = LLMJudgeConfig(backend="torch", device="cpu")
    backend = create_judge_backend(config)
    assert isinstance(backend, PytorchJudgeBackend)
