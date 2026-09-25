"""Unit tests for the autoregressive LLM Judge backend and zero-copy truncation."""

from __future__ import annotations

from typing import Any
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


def test_pytorch_backend_multi_token_marginalization() -> None:
    """Verify logsumexp marginalization across positive and negative token clusters."""
    config = LLMJudgeConfig(device="cpu", marginalize_tokens=True)
    backend = PytorchJudgeBackend(config, model=MagicMock(), tokenizer=MagicMock())
    backend._pos_token_ids = [10, 11]
    backend._neg_token_ids = [20, 21]

    # Logits vector of size 50
    logits = torch.zeros(50)
    logits[10] = 2.0  # YES
    logits[11] = 1.0  # Yes
    logits[20] = 0.5  # NO
    logits[21] = 0.2  # No

    pos_val, neg_val, margin = backend._compute_decision_logits(logits)
    expected_pos = float(torch.logsumexp(torch.tensor([2.0, 1.0]), dim=0).item())
    expected_neg = float(torch.logsumexp(torch.tensor([0.5, 0.2]), dim=0).item())

    assert np.isclose(pos_val, expected_pos, atol=1e-5)
    assert np.isclose(neg_val, expected_neg, atol=1e-5)
    assert np.isclose(margin, expected_pos - expected_neg, atol=1e-5)


def test_pytorch_backend_prior_calibration_subtraction() -> None:
    """Verify that prior_bias is subtracted from raw margin, shifting confidence."""
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

    # Logits: raw margin = 2.0 - 1.0 = 1.0
    fake_logits = torch.zeros(1, 2, 100)
    fake_logits[0, -1, 10] = 2.0
    fake_logits[0, -1, 20] = 1.0

    mock_output = MagicMock()
    mock_output.logits = fake_logits

    mock_model = MagicMock()
    mock_model.return_value = mock_output
    mock_model.config.hidden_size = 64

    backend = PytorchJudgeBackend(config, model=mock_model, tokenizer=mock_tokenizer)
    cache = MockDynamicCache(seq_len=15, hidden_dim=64)

    # Without prior bias (prior_bias=0.0): raw margin = 1.0 > 0 -> match
    res_no_bias = backend.evaluate_candidate(
        base_cache=cache,
        base_seq_len=10,
        candidate_suffix="Candidate: Sinuhe",
        prior_bias=0.0,
    )
    assert res_no_bias.calibrated_margin == 1.0
    assert res_no_bias.is_match is True

    # With prior bias of 1.5: calibrated margin = 1.0 - 1.5 = -0.5 < 0 -> not match
    res_calibrated = backend.evaluate_candidate(
        base_cache=cache,
        base_seq_len=10,
        candidate_suffix="Candidate: Sinuhe",
        prior_bias=1.5,
    )
    assert np.isclose(res_calibrated.calibrated_margin, -0.5, atol=1e-5)
    assert res_calibrated.is_match is False
    assert res_calibrated.confidence_score < 0.5


def test_pytorch_backend_prefix_lm_mask_passed() -> None:
    """Verify that Prefix-LM 4D attention mask is created and passed to the model."""
    config = LLMJudgeConfig(
        device="cpu",
        prefix_lm_mask=True,
        calibrate_prior=False,
    )
    mock_tokenizer = MagicMock()
    mock_inputs = MagicMock()
    mock_inputs.input_ids = torch.tensor([[101, 200, 300, 400]])
    mock_inputs.attention_mask = torch.tensor([[1, 1, 1, 1]])
    mock_inputs.to.return_value = mock_inputs
    mock_tokenizer.return_value = mock_inputs

    mock_output = MagicMock()
    mock_output.hidden_states = [torch.randn(1, 4, 64)]
    mock_output.past_key_values = MockDynamicCache(seq_len=4, hidden_dim=64)

    mock_model = MagicMock()
    mock_model.return_value = mock_output
    mock_model.config.hidden_size = 64

    backend = PytorchJudgeBackend(config, model=mock_model, tokenizer=mock_tokenizer)
    backend.encode_target("Prefix test", retain_cache=True)

    # Check the kwargs passed to mock_model
    call_kwargs = mock_model.call_args[1]
    assert "attention_mask" in call_kwargs
    attn_mask = call_kwargs["attention_mask"]
    # Should be 4D tensor with shape [1, 1, 4, 4] and dtype bool
    assert attn_mask.shape == (1, 1, 4, 4)
    assert attn_mask.dtype == torch.bool
    assert torch.all(attn_mask)  # All tokens attend bidirectionally


def test_llm_judge_symmetric_scoring() -> None:
    """Verify that LLMJudgeScorer performs symmetric dual-direction evaluation on close contenders."""
    from reconciliator.rerank.llm_judge import LLMJudgeScorer

    mock_backend = MagicMock()
    mock_backend.config = LLMJudgeConfig(
        symmetric_scoring=True,
        symmetric_margin_threshold=0.10,
    )

    # First target encoding (query -> candidates)
    mock_enc_q = TargetEncoding(
        dense_vector=np.ones(64, dtype=np.float32),
        kv_cache="cache_q",
        base_seq_len=10,
        prior_bias=0.0,
    )
    # Reverse candidate encodings (cand -> query)
    mock_enc_c1 = TargetEncoding(
        dense_vector=np.ones(64, dtype=np.float32),
        kv_cache="cache_c1",
        base_seq_len=12,
        prior_bias=0.0,
    )
    mock_enc_c2 = TargetEncoding(
        dense_vector=np.ones(64, dtype=np.float32),
        kv_cache="cache_c2",
        base_seq_len=14,
        prior_bias=0.0,
    )

    def mock_encode(text: str, retain_cache: bool = True) -> TargetEncoding:
        if "Cand 1" in text:
            return mock_enc_c1
        if "Cand 2" in text:
            return mock_enc_c2
        return mock_enc_q

    mock_backend.encode_target.side_effect = mock_encode

    # First forward pass scores: doc0 = 0.82, doc1 = 0.80 (delta = 0.02 <= 0.10 threshold)
    # Reverse pass scores: doc0 reverse = 0.70 (avg = (0.82+0.70)/2 = 0.76)
    #                      doc1 reverse = 0.86 (avg = (0.80+0.86)/2 = 0.83)
    eval_calls = []

    def mock_eval_candidate(
        base_cache: Any, base_seq_len: int, candidate_suffix: str, prior_bias: float = 0.0
    ) -> CandidateEvaluation:
        eval_calls.append((base_cache, candidate_suffix))
        if base_cache == "cache_q":
            if "Cand 1" in candidate_suffix:
                return CandidateEvaluation(is_match=True, confidence_score=0.82, raw_token="YES")
            return CandidateEvaluation(is_match=True, confidence_score=0.80, raw_token="YES")
        elif base_cache == "cache_c1":
            return CandidateEvaluation(is_match=True, confidence_score=0.70, raw_token="YES")
        elif base_cache == "cache_c2":
            return CandidateEvaluation(is_match=True, confidence_score=0.86, raw_token="YES")
        return CandidateEvaluation(is_match=False, confidence_score=0.0, raw_token="NO")

    mock_backend.evaluate_candidate.side_effect = mock_eval_candidate

    scorer = LLMJudgeScorer(mock_backend)
    scores = scorer.score_batch("Query Book", ["Cand 1", "Cand 2"])

    # Both contenders triggered reverse forward passes
    assert len(eval_calls) == 4  # 2 forward + 2 reverse
    # Symmetrically averaged scores
    assert np.isclose(scores[0], 0.76, atol=1e-4)
    assert np.isclose(scores[1], 0.83, atol=1e-4)
