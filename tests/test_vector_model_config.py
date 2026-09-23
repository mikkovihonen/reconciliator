"""Unit tests for explicit vector_model configuration setting ("embedding" vs "llm")."""

from __future__ import annotations

import pytest

from reconciliator.config import (
    AppConfig,
    EmbeddingConfig,
    LLMJudgeConfig,
    get_bundled_embedding_defaults,
    get_bundled_llm_defaults,
    get_bundled_vector_model_default,
)


def test_embedding_config_defaults_from_bundled_yaml() -> None:
    """Verify that EmbeddingConfig dynamically resolves all default values from defaults.yaml."""
    cfg = EmbeddingConfig()
    bundled = get_bundled_embedding_defaults()
    assert cfg.model_name == bundled["model_name"]
    assert str(cfg.template_path) == bundled["template_path"]
    assert cfg.batch_size == bundled["batch_size"]
    assert cfg.cuda_batch_scale == bundled["cuda_batch_scale"]
    assert cfg.mps_batch_scale == bundled["mps_batch_scale"]
    assert cfg.device == bundled["device"]


def test_llm_judge_config_defaults_from_bundled_yaml() -> None:
    """Verify that LLMJudgeConfig dynamically resolves all default values from defaults.yaml."""
    cfg = LLMJudgeConfig()
    bundled = get_bundled_llm_defaults()
    assert cfg.backend == bundled["backend"]
    assert cfg.model_name == bundled["model_name"]
    assert cfg.quantization == bundled["quantization"]
    assert cfg.positive_token == bundled["positive_token"]
    assert cfg.negative_token == bundled["negative_token"]
    assert cfg.pooling_strategy == bundled["pooling_strategy"]
    assert cfg.batch_size == bundled["batch_size"]
    assert cfg.max_seq_length == bundled["max_seq_length"]


def test_bundled_vector_model_default() -> None:
    """Verify that defaults.yaml specifies 'embedding' as default vector_model."""
    assert get_bundled_vector_model_default() == "embedding"


def test_app_config_default_uses_embedding() -> None:
    """By default, AppConfig sets vector_model='embedding' and populates both sections."""
    app_cfg = AppConfig()
    assert app_cfg.vector_model == "embedding"
    assert app_cfg.embedding is not None
    assert app_cfg.llm is not None
    assert app_cfg.embedding.model_name == get_bundled_embedding_defaults()["model_name"]
    assert app_cfg.llm.model_name == get_bundled_llm_defaults()["model_name"]


def test_app_config_explicit_llm() -> None:
    """When vector_model is 'llm', LLM configuration is active without commenting out embedding."""
    app_cfg = AppConfig.model_validate(
        {
            "vector_model": "llm",
            "llm": {"model_name": "Custom/LLM-Model"},
        }
    )
    assert app_cfg.vector_model == "llm"
    assert app_cfg.llm.model_name == "Custom/LLM-Model"
    assert app_cfg.embedding.model_name == get_bundled_embedding_defaults()["model_name"]


def test_app_config_invalid_vector_model_raises() -> None:
    """Setting vector_model to an invalid value must raise a strict ValueError."""
    with pytest.raises(ValueError, match="Input should be 'embedding' or 'llm'"):
        AppConfig.model_validate({"vector_model": "unsupported_model"})


def test_llm_judge_engine_forbidden_with_embedding_vector_model() -> None:
    """Selecting rerank.engine='llm_judge' when vector_model is 'embedding' must raise ValueError."""
    with pytest.raises(
        ValueError,
        match="Rerank engine 'llm_judge' cannot be used when vector_model is 'embedding'",
    ):
        AppConfig.model_validate(
            {
                "vector_model": "embedding",
                "reconcile": {
                    "target_file": "test.xml",
                    "rerank": {
                        "engine": "llm_judge",
                    },
                },
            }
        )


def test_llm_judge_engine_allowed_with_llm_vector_model() -> None:
    """Selecting rerank.engine='llm_judge' when vector_model='llm' must succeed and synchronize judge."""
    app_cfg = AppConfig.model_validate(
        {
            "vector_model": "llm",
            "llm": {"model_name": "Qwen/Qwen2.5-7B-Instruct", "backend": "torch"},
            "reconcile": {
                "target_file": "test.xml",
                "rerank": {
                    "engine": "llm_judge",
                },
            },
        }
    )
    assert app_cfg.vector_model == "llm"
    assert app_cfg.reconcile is not None
    assert app_cfg.reconcile.rerank.engine == "llm_judge"
    assert app_cfg.reconcile.rerank.judge is not None
    assert app_cfg.reconcile.rerank.judge.model_name == "Qwen/Qwen2.5-7B-Instruct"
