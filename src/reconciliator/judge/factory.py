"""Factory for instantiating LLM Judge backends."""

from __future__ import annotations

import importlib.util
import logging
import platform
from typing import TYPE_CHECKING

from reconciliator.judge.protocol import LLMJudgeBackend
from reconciliator.judge.torch_backend import PytorchJudgeBackend

if TYPE_CHECKING:
    from reconciliator.config import LLMJudgeConfig

logger = logging.getLogger(__name__)


def create_judge_backend(config: LLMJudgeConfig) -> LLMJudgeBackend:
    """
    Instantiate the appropriate LLMJudgeBackend according to hardware availability and config.
    """
    backend_type = (config.backend or "auto").lower()

    if backend_type == "mlx":
        from reconciliator.judge.mlx_backend import MLXJudgeBackend

        logger.info("Instantiating native Apple Silicon MLX Judge backend")
        return MLXJudgeBackend(config)

    if backend_type == "torch":
        logger.info("Instantiating PyTorch Judge backend on device=%s", config.device)
        return PytorchJudgeBackend(config)

    # Auto detection: prefer MLX on Apple Silicon if mlx is available
    if platform.system() == "Darwin" and platform.machine() == "arm64":
        mlx_ok = importlib.util.find_spec("mlx") is not None
        mlx_lm_ok = importlib.util.find_spec("mlx_lm") is not None
        if mlx_ok and mlx_lm_ok:
            from reconciliator.judge.mlx_backend import MLXJudgeBackend

            logger.info("Auto-selected Apple Silicon native MLX backend")
            return MLXJudgeBackend(config)

    logger.info("Auto-selected PyTorch Judge backend on device=%s", config.device)
    return PytorchJudgeBackend(config)
