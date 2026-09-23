"""Embedding model configuration model."""

from __future__ import annotations

from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel, Field, model_validator

from reconciliator.config.defaults import get_bundled_embedding_defaults

_EMBED_DEFAULTS = get_bundled_embedding_defaults()

VectorModelType = Literal["embedding", "llm"]


class EmbeddingConfig(BaseModel):
    """Embedding model and hardware acceleration configuration."""

    model_name: str = Field(
        default_factory=lambda: str(
            _EMBED_DEFAULTS.get("model_name", "TurkuNLP/sbert-cased-finnish-paraphrase")
        )
    )
    template_path: Path = Field(
        default_factory=lambda: Path(
            _EMBED_DEFAULTS.get(
                "template_path", "reconciliator/embedding/templates/default_serialization.j2"
            )
        )
    )
    device: Literal["auto", "cuda", "mps", "cpu"] = Field(
        default_factory=lambda: _EMBED_DEFAULTS.get("device", "auto")
    )
    batch_size: int = Field(default_factory=lambda: int(_EMBED_DEFAULTS.get("batch_size", 64)))
    cuda_batch_scale: int = Field(
        default_factory=lambda: int(_EMBED_DEFAULTS.get("cuda_batch_scale", 4))
    )
    mps_batch_scale: int = Field(
        default_factory=lambda: int(_EMBED_DEFAULTS.get("mps_batch_scale", 2))
    )
    num_cpu_threads: Optional[int] = Field(
        default_factory=lambda: _EMBED_DEFAULTS.get("num_cpu_threads")
    )
    force_template: bool = Field(
        default_factory=lambda: bool(_EMBED_DEFAULTS.get("force_template", False))
    )
    vector_dim: Optional[int] = Field(default_factory=lambda: _EMBED_DEFAULTS.get("vector_dim"))
    max_seq_length: Optional[int] = Field(
        default_factory=lambda: _EMBED_DEFAULTS.get("max_seq_length")
    )

    @model_validator(mode="after")
    def validate_batch_size(self) -> EmbeddingConfig:
        if self.batch_size <= 0:
            raise ValueError(f"batch_size must be a positive integer, got {self.batch_size}")
        return self
