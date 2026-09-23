"""PyTorch implementation of the autoregressive LLM Judge backend."""

from __future__ import annotations

import logging
import math
from typing import TYPE_CHECKING, Any, Optional

import numpy as np
import torch
import torch.nn.functional as F

from reconciliator.judge.protocol import CandidateEvaluation, TargetEncoding
from reconciliator.utils.device import resolve_device

if TYPE_CHECKING:
    from reconciliator.config import LLMJudgeConfig

logger = logging.getLogger(__name__)


class PytorchJudgeBackend:
    """
    Hardware-accelerated PyTorch backend for LLM judge and dense vector extraction.
    Supports CUDA (SDPA / FlashAttention-2), Apple MPS, and CPU with zero-copy KV truncation.
    """

    def __init__(
        self,
        config: LLMJudgeConfig,
        model: Optional[Any] = None,
        tokenizer: Optional[Any] = None,
    ) -> None:
        self.config = config
        self.device = resolve_device(config.device)
        self._model = model
        self._tokenizer = tokenizer
        self._dimension: Optional[int] = None

        # Resolve positive and negative token IDs lazily or on init
        self._pos_token_id: Optional[int] = None
        self._neg_token_id: Optional[int] = None

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
        """Load HuggingFace CausalLM model and tokenizer with optimal precision and attention."""
        from transformers import AutoModelForCausalLM, AutoTokenizer

        model_name = self.config.model_name
        logger.info("Loading tokenizer for %s", model_name)
        self._tokenizer = AutoTokenizer.from_pretrained(model_name, trust_remote_code=True)
        if self._tokenizer.pad_token is None:
            self._tokenizer.pad_token = self._tokenizer.eos_token

        # Resolve precision
        dtype: Optional[torch.dtype] = None
        if self.config.torch_dtype == "float16":
            dtype = torch.float16
        elif self.config.torch_dtype == "bfloat16":
            dtype = torch.bfloat16
        elif self.config.torch_dtype == "float32":
            dtype = torch.float32
        elif self.config.torch_dtype == "auto":
            if self.device == "cuda" and torch.cuda.is_bf16_supported():
                dtype = torch.bfloat16
            elif self.device in ("cuda", "mps"):
                dtype = torch.float16
            else:
                dtype = torch.float32

        # Attention implementation
        attn_kwargs: dict[str, Any] = {}
        if self.config.attn_implementation != "auto":
            attn_kwargs["attn_implementation"] = self.config.attn_implementation
        elif self.device == "cuda":
            attn_kwargs["attn_implementation"] = "sdpa"

        logger.info(
            "Loading model %s on %s (dtype=%s, attn=%s)",
            model_name,
            self.device,
            dtype,
            attn_kwargs.get("attn_implementation", "default"),
        )
        loader: Any = AutoModelForCausalLM.from_pretrained
        loaded = loader(
            model_name,
            torch_dtype=dtype,
            trust_remote_code=True,
            **attn_kwargs,
        )
        self._model = loaded.to(self.device)
        self._model.eval()

        self._resolve_decision_token_ids()

    def _resolve_decision_token_ids(self) -> None:
        """Identify token IDs for the positive and negative classification targets."""
        tokenizer = self.tokenizer

        def get_id(token_str: str) -> int:
            # Try with and without leading space
            for candidate in (f" {token_str}", token_str):
                ids = tokenizer.encode(candidate, add_special_tokens=False)
                if ids:
                    return int(ids[-1])
            ids = tokenizer.encode(token_str, add_special_tokens=True)
            return int(ids[-1]) if ids else 0

        self._pos_token_id = get_id(self.config.positive_token)
        self._neg_token_id = get_id(self.config.negative_token)
        logger.debug(
            "Resolved decision tokens: %s -> %s, %s -> %s",
            self.config.positive_token,
            self._pos_token_id,
            self.config.negative_token,
            self._neg_token_id,
        )

    def encode_target(self, text: str, retain_cache: bool = True) -> TargetEncoding:
        """
        Process target record through base model once.
        Simultaneously extracts L2-normalized dense embedding and locks multi-layer KV-cache.
        """
        prompt = self.config.target_template.format(text=text)
        inputs = self.tokenizer(
            prompt,
            return_tensors="pt",
            truncation=True,
            max_length=self.config.max_seq_length,
        ).to(self.device)

        base_seq_len = int(inputs.input_ids.shape[1])

        with torch.inference_mode():
            outputs = self.model(
                **inputs,
                use_cache=retain_cache,
                output_hidden_states=True,
                return_dict=True,
            )

            # Extract dense representation from final hidden states
            last_hidden = outputs.hidden_states[-1]  # [1, seq_len, hidden_dim]
            if self.config.pooling_strategy == "last_token":
                pooled = last_hidden[:, -1, :]
            else:
                pooled = last_hidden.mean(dim=1)

            dense_norm = F.normalize(pooled, p=2, dim=-1)
            dense_vector = dense_norm.squeeze(0).to(torch.float32).cpu().numpy()

        kv_cache = outputs.past_key_values if retain_cache else None
        return TargetEncoding(
            dense_vector=dense_vector,
            kv_cache=kv_cache,
            base_seq_len=base_seq_len,
        )

    def encode_batch_dense(self, texts: list[str]) -> np.ndarray:
        """
        Batch dense encoding for offline vector index ingestion.
        Returns L2-normalized [N, dimension] numpy array.
        """
        if not texts:
            return np.empty((0, self.dimension), dtype=np.float32)

        batch_size = max(1, self.config.batch_size)
        all_embeddings: list[np.ndarray] = []

        for i in range(0, len(texts), batch_size):
            chunk = texts[i : i + batch_size]
            prompts = [self.config.target_template.format(text=t) for t in chunk]
            inputs = self.tokenizer(
                prompts,
                return_tensors="pt",
                padding=True,
                truncation=True,
                max_length=self.config.max_seq_length,
            ).to(self.device)

            with torch.inference_mode():
                outputs = self.model(
                    **inputs,
                    use_cache=False,
                    output_hidden_states=True,
                    return_dict=True,
                )
                hidden_states = outputs.hidden_states[-1]

                if self.config.pooling_strategy == "last_token":
                    # Get last non-padding token index for each sequence
                    lengths = inputs.attention_mask.sum(dim=1) - 1
                    batch_idx = torch.arange(len(chunk), device=hidden_states.device)
                    pooled = hidden_states[batch_idx, lengths]
                else:
                    mask = inputs.attention_mask.unsqueeze(-1).expand_as(hidden_states)
                    pooled = (hidden_states * mask).sum(dim=1) / mask.sum(dim=1).clamp(min=1e-9)

                normed = F.normalize(pooled, p=2, dim=-1)
                all_embeddings.append(normed.to(torch.float32).cpu().numpy())

        return np.vstack(all_embeddings)

    def evaluate_candidate(
        self, base_cache: Any, base_seq_len: int, candidate_suffix: str
    ) -> CandidateEvaluation:
        """
        Evaluate candidate suffix with the locked base KV-cache, decode classification token,
        and perform zero-copy in-place truncation back to base_seq_len.
        """
        if self._pos_token_id is None or self._neg_token_id is None:
            self._resolve_decision_token_ids()

        pos_id = self._pos_token_id or 0
        neg_id = self._neg_token_id or 0

        suffix_inputs = self.tokenizer(
            candidate_suffix,
            return_tensors="pt",
            add_special_tokens=False,
        ).to(self.device)

        try:
            with torch.inference_mode():
                outputs = self.model(
                    input_ids=suffix_inputs.input_ids,
                    past_key_values=base_cache,
                    use_cache=True,
                    return_dict=True,
                )
                logits = outputs.logits[:, -1, :]  # [1, vocab_size]
                pos_logit = float(logits[0, pos_id].item())
                neg_logit = float(logits[0, neg_id].item())

                is_match = pos_logit > neg_logit
                margin = pos_logit - neg_logit

                # Calibrated confidence via sigmoid
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
            # Guarantee zero-copy in-place truncation even if forward pass errors
            self.truncate_kv_cache(base_cache, base_seq_len)

    def truncate_kv_cache(self, kv_cache: Any, base_seq_len: int) -> None:
        """
        Slice key and value tensors back to base_seq_len without memory reallocations or copying.
        Works across standard HuggingFace DynamicCache and legacy tuple caches.
        """
        if kv_cache is None:
            return

        # Modern DynamicCache (transformers >= 4.36)
        if hasattr(kv_cache, "layers") and kv_cache.layers:
            for layer in kv_cache.layers:
                if hasattr(layer, "keys") and layer.keys is not None:
                    layer.keys = layer.keys[..., :base_seq_len, :]
                if hasattr(layer, "values") and layer.values is not None:
                    layer.values = layer.values[..., :base_seq_len, :]
            return

        # DynamicCache with key_cache / value_cache lists
        if hasattr(kv_cache, "key_cache") and hasattr(kv_cache, "value_cache"):
            for i in range(len(kv_cache.key_cache)):
                kv_cache.key_cache[i] = kv_cache.key_cache[i][..., :base_seq_len, :]
                kv_cache.value_cache[i] = kv_cache.value_cache[i][..., :base_seq_len, :]
            return

        # Legacy tuple of tuples: ( (k_0, v_0), (k_1, v_1), ... )
        if isinstance(kv_cache, (list, tuple)):
            for layer in kv_cache:
                if isinstance(layer, (list, tuple)) and len(layer) >= 2:
                    if hasattr(layer[0], "data"):
                        layer[0].data = layer[0].data[..., :base_seq_len, :]
                    if hasattr(layer[1], "data"):
                        layer[1].data = layer[1].data[..., :base_seq_len, :]
