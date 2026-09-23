"""Candidate reranking, metadata discriminators, and LLM judge models."""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from reconciliator.config.defaults import get_bundled_llm_defaults

_LLM_DEFAULTS = get_bundled_llm_defaults()

RerankEngineType = Literal["cascade", "llm_judge", "bm25", "cross_encoder", "fuzzy", "noop"]


class ShortCircuitConfig(BaseModel):
    """High-confidence dense fast-path shortcut configuration."""

    enabled: bool = True
    threshold: float = 0.98

    @model_validator(mode="after")
    def validate_threshold(self) -> ShortCircuitConfig:
        if not (0.0 <= self.threshold <= 1.0):
            raise ValueError(
                f"short_circuit threshold must be between 0.0 and 1.0, got {self.threshold}"
            )
        return self


class LLMJudgeConfig(BaseModel):
    """Configuration for autoregressive LLM Judge with KV-branching and zero-copy truncation."""

    backend: Literal["auto", "torch", "mlx"] = Field(
        default_factory=lambda: _LLM_DEFAULTS.get("backend", "auto")
    )
    model_name: str = Field(
        default_factory=lambda: str(_LLM_DEFAULTS.get("model_name", "Qwen/Qwen2.5-7B-Instruct"))
    )
    quantization: Optional[Literal["4bit", "8bit"]] = Field(
        default_factory=lambda: _LLM_DEFAULTS.get("quantization", "8bit")
    )
    device: Literal["auto", "cuda", "mps", "cpu"] = Field(
        default_factory=lambda: _LLM_DEFAULTS.get("device", "auto")
    )
    torch_dtype: Literal["auto", "float16", "bfloat16", "float32"] = Field(
        default_factory=lambda: _LLM_DEFAULTS.get("torch_dtype", "auto")
    )
    attn_implementation: Literal["auto", "sdpa", "flash_attention_2"] = Field(
        default_factory=lambda: _LLM_DEFAULTS.get("attn_implementation", "auto")
    )
    pooling_strategy: Literal["last_token", "mean"] = Field(
        default_factory=lambda: _LLM_DEFAULTS.get("pooling_strategy", "last_token")
    )
    batch_size: int = Field(default_factory=lambda: int(_LLM_DEFAULTS.get("batch_size", 16)))
    positive_token: str = Field(
        default_factory=lambda: str(_LLM_DEFAULTS.get("positive_token", "YES"))
    )
    negative_token: str = Field(
        default_factory=lambda: str(_LLM_DEFAULTS.get("negative_token", "NO"))
    )
    target_template: str = Field(
        default_factory=lambda: str(
            _LLM_DEFAULTS.get(
                "target_template",
                "Instruct: Given a bibliographic record, retrieve matching catalog works.\nQuery: {text}\n",
            )
        )
    )
    candidate_suffix_template: str = Field(
        default_factory=lambda: str(
            _LLM_DEFAULTS.get(
                "candidate_suffix_template",
                "Candidate: {text}\nDoes this refer to the same work? Answer YES or NO: ",
            )
        )
    )
    max_seq_length: int = Field(
        default_factory=lambda: int(_LLM_DEFAULTS.get("max_seq_length", 2048))
    )


JudgeConfig = LLMJudgeConfig


class CascadeRouterConfig(BaseModel):
    """Configuration for cascading dual-route evaluation and dynamic LLM judge escalation."""

    model_config = ConfigDict(extra="forbid")

    heuristic_engine: Literal["bm25", "cross_encoder", "fuzzy", "noop"] = Field(
        default="bm25",
        description="Fast heuristic engine to score candidates first: 'bm25', 'fuzzy', 'cross_encoder', or 'noop'",
    )
    high_confidence_threshold: float = Field(
        default=0.90,
        ge=0.0,
        le=1.0,
        description="Heuristic score at or above which a candidate is accepted without LLM judge if margin holds",
    )
    low_confidence_threshold: float = Field(
        default=0.55,
        ge=0.0,
        le=1.0,
        description="Heuristic score below which candidates are rejected without LLM judge",
    )
    ambiguity_margin: float = Field(
        default=0.08,
        ge=0.0,
        le=1.0,
        description="Minimum score gap required between top candidate and 2nd candidate to avoid LLM escalation",
    )
    uncertainty_band_min: float = Field(
        default=0.60,
        ge=0.0,
        le=1.0,
        description="Lower bound of heuristic score uncertainty band; scores in this range trigger LLM escalation",
    )
    uncertainty_band_max: float = Field(
        default=0.88,
        ge=0.0,
        le=1.0,
        description="Upper bound of heuristic score uncertainty band",
    )
    max_escalated_candidates: int = Field(
        default=5,
        ge=1,
        description="Maximum number of ambiguous candidates to escalate to LLM judge per target",
    )


class RerankConfig(BaseModel):
    """Configuration for candidate reranking, metadata discriminators, and LLM judge."""

    enabled: bool = True
    engine: RerankEngineType = "cascade"
    model_name: Optional[str] = None
    device: Literal["auto", "cpu", "cuda", "mps"] = "auto"
    batch_size: int = 16
    dense_weight: float = 0.6
    rerank_weight: float = 0.4
    top_k: int = 50
    short_circuit: ShortCircuitConfig = Field(default_factory=ShortCircuitConfig)
    require_volume_match: bool = True
    require_surname_match: bool = True
    max_year_difference: int = 5
    year_penalty_threshold: int = 1
    apply_pre_veto: bool = True
    judge: Optional[LLMJudgeConfig] = None
    cascade: CascadeRouterConfig = Field(default_factory=CascadeRouterConfig)

    @model_validator(mode="after")
    def validate_weights(self) -> RerankConfig:
        if not (0.0 <= self.dense_weight <= 1.0):
            raise ValueError(f"dense_weight must be in [0.0, 1.0], got {self.dense_weight}")
        if not (0.0 <= self.rerank_weight <= 1.0):
            raise ValueError(f"rerank_weight must be in [0.0, 1.0], got {self.rerank_weight}")
        if round(self.dense_weight + self.rerank_weight, 5) != 1.0:
            raise ValueError(
                f"dense_weight and rerank_weight must sum to 1.0, got {self.dense_weight + self.rerank_weight}"
            )
        return self
