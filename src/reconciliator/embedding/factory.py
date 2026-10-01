"""Centralized factory for instantiating the configured vector embedder."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from reconciliator.config import AppConfig

from reconciliator.embedding.embedder import SentenceEmbedder
from reconciliator.embedding.protocol import VectorEmbedder

logger = logging.getLogger(__name__)


_EMBEDDER_CACHE: dict[tuple[str, Any], VectorEmbedder] = {}


def clear_embedder_cache() -> None:
    """Clear the cached vector embedder instances."""
    _EMBEDDER_CACHE.clear()


def create_vector_embedder(cfg: AppConfig, use_cache: bool = True) -> VectorEmbedder:

    """Instantiate the configured vector embedder (either SentenceEmbedder or LLMJudgeBackend).

    Driven by cfg.vector_model ('embedding' or 'llm').
    When use_cache=True, reuses an already initialized embedder to prevent redundant weight loading.
    """
    model_choice = getattr(cfg, "vector_model", "embedding").lower().strip()
    if model_choice not in ("llm", "embedding"):
        raise ValueError(
            f"Invalid vector_model setting: '{model_choice}'. Must be either 'embedding' or 'llm'."
        )

    cache_key = (
        model_choice,
        (cfg.llm.model_name, cfg.llm.backend, cfg.llm.device)
        if model_choice == "llm"
        else (cfg.embedding.model_name, cfg.embedding.device),
    )

    if use_cache and cache_key in _EMBEDDER_CACHE:
        logger.debug("Reusing cached vector embedder for %s", cache_key)
        return _EMBEDDER_CACHE[cache_key]

    if model_choice == "llm":
        from reconciliator.judge.factory import create_judge_backend

        logger.info(
            "Creating LLMJudgeBackend for vector indexing and retrieval (model=%s, backend=%s)",
            cfg.llm.model_name,
            cfg.llm.backend,
        )
        embedder = create_judge_backend(cfg.llm)
    else:
        logger.info(
            "Creating SentenceEmbedder for vector indexing and retrieval (model=%s, device=%s)",
            cfg.embedding.model_name,
            cfg.embedding.device,
        )
        embedder = SentenceEmbedder(cfg.embedding)

    if use_cache:
        _EMBEDDER_CACHE[cache_key] = embedder

    return embedder
