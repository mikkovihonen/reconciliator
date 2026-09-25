"""Factory for instantiating LLM Judge backends."""

from __future__ import annotations

import importlib.util
import logging
import platform
from typing import TYPE_CHECKING, Any

from reconciliator.judge.protocol import LLMJudgeBackend
from reconciliator.judge.torch_backend import PytorchJudgeBackend

if TYPE_CHECKING:
    from reconciliator.config import LLMJudgeConfig

logger = logging.getLogger(__name__)


_JUDGE_BACKEND_CACHE: dict[tuple[Any, ...], LLMJudgeBackend] = {}


def clear_judge_backend_cache() -> None:
    """Clear the cached LLM judge backends."""
    _JUDGE_BACKEND_CACHE.clear()


def create_judge_backend(config: LLMJudgeConfig, use_cache: bool = True) -> LLMJudgeBackend:
    """Instantiate the appropriate LLMJudgeBackend according to hardware availability and config.

    When use_cache=True, reuses an existing instantiated backend for identical configuration
    to prevent reloading large model weights into memory multiple times.
    """
    cache_key = (
        getattr(config, "backend", "auto"),
        getattr(config, "model_name", ""),
        getattr(config, "device", "auto"),
        getattr(config, "torch_dtype", "auto"),
        getattr(config, "quantization", None),
        getattr(config, "attn_implementation", "auto"),
        getattr(config, "max_seq_length", 2048),
    )

    if use_cache and cache_key in _JUDGE_BACKEND_CACHE:
        logger.debug("Reusing cached LLMJudgeBackend for model=%s", config.model_name)
        return _JUDGE_BACKEND_CACHE[cache_key]

    backend_type = (config.backend or "auto").lower()

    if backend_type == "mlx":
        from reconciliator.judge.mlx_backend import MLXJudgeBackend

        logger.info("Instantiating native Apple Silicon MLX Judge backend")
        backend: LLMJudgeBackend = MLXJudgeBackend(config)
    elif backend_type == "torch":
        logger.info("Instantiating PyTorch Judge backend on device=%s", config.device)
        backend = PytorchJudgeBackend(config)
    elif platform.system() == "Darwin" and platform.machine() == "arm64":
        # Auto detection: prefer MLX on Apple Silicon if mlx is available
        mlx_ok = importlib.util.find_spec("mlx") is not None
        mlx_lm_ok = importlib.util.find_spec("mlx_lm") is not None
        if mlx_ok and mlx_lm_ok:
            from reconciliator.judge.mlx_backend import MLXJudgeBackend

            logger.info("Auto-selected Apple Silicon native MLX backend")
            backend = MLXJudgeBackend(config)
        else:
            logger.info("Auto-selected PyTorch Judge backend on device=%s", config.device)
            backend = PytorchJudgeBackend(config)
    else:
        logger.info("Auto-selected PyTorch Judge backend on device=%s", config.device)
        backend = PytorchJudgeBackend(config)

    if use_cache:
        _JUDGE_BACKEND_CACHE[cache_key] = backend

    return backend
