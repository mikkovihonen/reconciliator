"""Apple Silicon native MLX implementation of the autoregressive LLM Judge backend."""

from __future__ import annotations

import logging
import math
import platform
from typing import TYPE_CHECKING, Any, Optional

import numpy as np

from reconciliator.judge.protocol import CandidateEvaluation, TargetEncoding

if TYPE_CHECKING:
    from reconciliator.config import LLMJudgeConfig

logger = logging.getLogger(__name__)


class MLXJudgeBackend:
    """
    Apple Silicon native backend utilizing MLX and Metal unified memory.
    Implements prompt cache slicing and zero-copy Metal array views.
    """

    def __init__(
        self,
        config: LLMJudgeConfig,
        model: Optional[Any] = None,
        tokenizer: Optional[Any] = None,
    ) -> None:
        self.config = config
        self._model = model
        self._tokenizer = tokenizer
        self._dimension: Optional[int] = None

        self._pos_token_id: Optional[int] = None
        self._neg_token_id: Optional[int] = None

        self._verify_platform()

    @staticmethod
    def _verify_platform() -> None:
        """Verify that the host environment is Apple Silicon on macOS."""
        if platform.system() != "Darwin" or platform.machine() != "arm64":
            raise RuntimeError(
                f"MLXJudgeBackend requires Apple Silicon on macOS (Darwin arm64), "
                f"but found {platform.system()} {platform.machine()}."
            )

    @property
    def model(self) -> Any:
        if self._model is None:
            self._initialize_model_and_tokenizer()
        return self._model

    @property
    def tokenizer(self) -> Any:
        if self._tokenizer is None:
            self._initialize_model_and_tokenizer()
        return self._tokenizer

    @property
    def dimension(self) -> int:
        if self._dimension is None:
            config_obj = getattr(self.model, "config", None)
            dim = getattr(config_obj, "hidden_size", None)
            if dim is None:
                dim = getattr(config_obj, "d_model", None)
            if dim is None:
                # Fallback: run sample encode
                test_enc = self.encode_target("test", retain_cache=False)
                dim = int(test_enc.dense_vector.shape[0])
            self._dimension = int(dim)
        return self._dimension

    def _initialize_model_and_tokenizer(self) -> None:
        """Load model and tokenizer using mlx_lm."""
        try:
            import mlx_lm
        except ImportError as exc:
            raise ImportError(
                "MLX backend requires 'mlx' and 'mlx-lm'. "
                "Install them via 'uv add mlx mlx-lm' or use backend='torch'."
            ) from exc

        model_name = self.config.model_name
        logger.info("Loading MLX model and tokenizer for %s", model_name)
        self._model, self._tokenizer = mlx_lm.load(model_name)
        self._resolve_decision_token_ids()

    def _resolve_decision_token_ids(self) -> None:
        """Resolve token IDs for positive and negative decisions."""
        tokenizer = self.tokenizer

        def get_id(token_str: str) -> int:
            for cand in (f" {token_str}", token_str):
                ids = tokenizer.encode(cand)
                if ids:
                    return int(ids[-1])
            ids = tokenizer.encode(token_str)
            return int(ids[-1]) if ids else 0

        self._pos_token_id = get_id(self.config.positive_token)
        self._neg_token_id = get_id(self.config.negative_token)

    def encode_target(self, text: str, retain_cache: bool = True) -> TargetEncoding:
        """Process target entity text with MLX and capture base prompt cache."""
        import mlx.core as mx
        from mlx_lm.models.cache import make_prompt_cache

        prompt = self.config.target_template.format(text=text)
        tokens = self.tokenizer.encode(prompt)
        base_seq_len = len(tokens)

        input_arr = mx.array([tokens])
        prompt_cache = make_prompt_cache(self.model) if retain_cache else None

        # Model forward pass
        logits = self.model(input_arr, cache=prompt_cache)
        mx.eval(logits)

        # Extract normalized dense vector (from hidden states, embeddings,
        # or pooled normalized logits).
        if hasattr(self.model, "model") and hasattr(self.model.model, "embed_tokens"):
            # Use embedding projection representation
            final_token = tokens[-1]
            token_emb = self.model.model.embed_tokens(mx.array([final_token]))
            norm = mx.linalg.norm(token_emb, axis=-1, keepdims=True)
            normed = token_emb / mx.maximum(norm, 1e-9)
            dense_vector = np.array(normed.squeeze(), dtype=np.float32)
        else:
            # Pooled normalized output logits
            pooled = logits[:, -1, :]
            norm = mx.linalg.norm(pooled, axis=-1, keepdims=True)
            normed = pooled / mx.maximum(norm, 1e-9)
            dense_vector = np.array(normed.squeeze(), dtype=np.float32)

        if prompt_cache is not None:
            for c in prompt_cache:
                if hasattr(c, "keys") and c.keys is not None:
                    mx.eval(c.keys)
                if hasattr(c, "values") and c.values is not None:
                    mx.eval(c.values)

        return TargetEncoding(
            dense_vector=dense_vector,
            kv_cache=prompt_cache,
            base_seq_len=base_seq_len,
        )

    def encode_batch_dense(self, texts: list[str]) -> np.ndarray:
        """Batch dense encoding for vector indexing."""
        if not texts:
            return np.empty((0, self.dimension), dtype=np.float32)

        vectors = [self.encode_target(t, retain_cache=False).dense_vector for t in texts]
        return np.vstack(vectors)

    def evaluate_candidate(
        self, base_cache: Any, base_seq_len: int, candidate_suffix: str
    ) -> CandidateEvaluation:
        """Evaluate candidate suffix using prompt cache and slice back in-place."""
        import mlx.core as mx

        if self._pos_token_id is None or self._neg_token_id is None:
            self._resolve_decision_token_ids()

        pos_id = self._pos_token_id or 0
        neg_id = self._neg_token_id or 0

        suffix_tokens = self.tokenizer.encode(candidate_suffix)
        suffix_arr = mx.array([suffix_tokens])

        try:
            logits = self.model(suffix_arr, cache=base_cache)
            mx.eval(logits)

            last_logits = logits[0, -1, :]
            pos_logit = float(last_logits[pos_id].item())
            neg_logit = float(last_logits[neg_id].item())

            is_match = pos_logit > neg_logit
            margin = pos_logit - neg_logit

            try:
                conf = 1.0 / (1.0 + math.exp(-margin))
            except OverflowError:
                conf = 1.0 if margin > 0 else 0.0

            raw_token = self.config.positive_token if is_match else self.config.negative_token
            return CandidateEvaluation(
                is_match=is_match,
                confidence_score=float(conf),
                raw_token=raw_token,
            )
        finally:
            self.truncate_kv_cache(base_cache, base_seq_len)

    def truncate_kv_cache(self, kv_cache: Any, base_seq_len: int) -> None:
        """Zero-copy prompt cache slicing in Metal unified memory."""
        if kv_cache is None:
            return

        for c in kv_cache:
            if hasattr(c, "keys") and c.keys is not None:
                c.keys = c.keys[..., :base_seq_len, :]
            if hasattr(c, "values") and c.values is not None:
                c.values = c.values[..., :base_seq_len, :]
            if hasattr(c, "offset"):
                c.offset = base_seq_len
