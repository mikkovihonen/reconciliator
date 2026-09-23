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
    assert cfg.max_seq_length == 2048


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


def test_defaults_yaml_contains_judge() -> None:
    """Verify that defaults.yaml contains the new rerank.judge block."""
    defaults_file = Path("src/reconciliator/defaults/defaults.yaml")
    assert defaults_file.exists()

    with defaults_file.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    rerank_sec = data.get("reconcile", {}).get("rerank", {})
    assert "judge" in rerank_sec
    assert rerank_sec.get("apply_pre_veto") is True
    judge_sec = rerank_sec["judge"]
    assert judge_sec.get("backend") == "auto"
    assert judge_sec.get("positive_token") == "YES"
    assert judge_sec.get("negative_token") == "NO"

