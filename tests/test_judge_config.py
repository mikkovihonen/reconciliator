"""Unit tests for LLMJudgeConfig and RerankConfig integration."""

from __future__ import annotations

from pathlib import Path

import yaml

from reconciliator.config import LLMJudgeConfig, RerankConfig


def test_judge_config_defaults() -> None:
    """Verify default parameters of LLMJudgeConfig."""
    cfg = LLMJudgeConfig()
    assert cfg.backend == "auto"
    assert cfg.model_name == "Qwen/Qwen2.5-7B-Instruct"
    assert cfg.quantization == "8bit"
    assert cfg.positive_token == "YES"
    assert cfg.negative_token == "NO"
    assert cfg.pooling_strategy == "last_token"
    assert "Task: Determine if two bibliographic records" in cfg.target_template
    assert "Question: Do Target Record and Candidate Record" in cfg.candidate_suffix_template
    assert cfg.calibrate_prior is True
    assert cfg.marginalize_tokens is True
    assert cfg.prefix_lm_mask is True
    assert cfg.symmetric_scoring is True
    assert cfg.symmetric_margin_threshold == 0.05
    assert cfg.adaptive_cot is False
    assert cfg.adaptive_cot_max_tokens == 64


def test_rerank_config_with_judge() -> None:
    """Verify RerankConfig includes judge and pre-veto options."""
    cfg = RerankConfig(
        engine="llm_judge",
        apply_pre_veto=True,
        judge=LLMJudgeConfig(backend="mlx", quantization="4bit"),
    )
    assert cfg.engine == "llm_judge"
    assert cfg.apply_pre_veto is True
    assert cfg.judge.backend == "mlx"
    assert cfg.judge.quantization == "4bit"


def test_defaults_yaml_contains_vector_model_and_llm() -> None:
    """Verify that defaults.yaml contains vector_model: 'embedding' and the top-level llm block."""
    defaults_file = Path("src/reconciliator/defaults/defaults.yaml")
    assert defaults_file.exists()

    with defaults_file.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    assert "vector_model" in data
    assert data["vector_model"] == "embedding"
    assert "llm" in data
    assert data["llm"] is not None
    llm_sec = data["llm"]
    assert llm_sec.get("backend") == "auto"
    assert llm_sec.get("positive_token") == "YES"
    assert llm_sec.get("negative_token") == "NO"
    assert llm_sec.get("model_name") == "Qwen/Qwen2.5-7B-Instruct"
    assert llm_sec.get("calibrate_prior") is True
    assert llm_sec.get("marginalize_tokens") is True
    assert llm_sec.get("prefix_lm_mask") is True
    assert llm_sec.get("symmetric_scoring") is True
    assert llm_sec.get("symmetric_margin_threshold") == 0.05
    assert llm_sec.get("adaptive_cot") is False
    assert llm_sec.get("adaptive_cot_max_tokens") == 64
