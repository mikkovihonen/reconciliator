"""Centralized factory for instantiating the configured vector embedder."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from reconciliator.config import AppConfig

from reconciliator.embedding.embedder import SentenceEmbedder

logger = logging.getLogger(__name__)


def create_vector_embedder(cfg: AppConfig) -> Any:
    """Instantiate the configured vector embedder (either SentenceEmbedder or LLMJudgeBackend).

    Driven by cfg.vector_model ('embedding' or 'llm').
    """
    model_choice = getattr(cfg, "vector_model", "embedding").lower().strip()
    if model_choice == "llm":
        from reconciliator.judge.factory import create_judge_backend

        logger.info(
            "Creating LLMJudgeBackend for vector indexing and retrieval (model=%s, backend=%s)",
            cfg.llm.model_name,
            cfg.llm.backend,
        )
        return create_judge_backend(cfg.llm)
    elif model_choice == "embedding":
        logger.info(
            "Creating SentenceEmbedder for vector indexing and retrieval (model=%s, device=%s)",
            cfg.embedding.model_name,
            cfg.embedding.device,
        )
        return SentenceEmbedder(cfg.embedding)
    else:
        raise ValueError(
            f"Invalid vector_model setting: '{model_choice}'. Must be either 'embedding' or 'llm'."
        )
