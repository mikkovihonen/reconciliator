"""SentenceTransformers wrapper with dynamic dimension discovery and hardware acceleration."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Optional

import numpy as np
from sentence_transformers import SentenceTransformer

from reconciliator.config import EmbeddingConfig
from reconciliator.embedding.protocol import BaseVectorEmbedder
from reconciliator.embedding.template import RecordTemplateEngine
from reconciliator.models import BibliographicRecord
from reconciliator.utils.device import (
    configure_mixed_precision,
    get_optimal_batch_size,
    resolve_device,
)


class SentenceEmbedder(BaseVectorEmbedder):
    """Wraps SentenceTransformer with template-based serialization and hardware acceleration."""

    def __init__(self, config: EmbeddingConfig) -> None:
        self.config = config
        self.device = resolve_device(config.device)
        self.batch_size = get_optimal_batch_size(
            config.batch_size,
            self.device,
            cuda_batch_scale=config.cuda_batch_scale,
            mps_batch_scale=config.mps_batch_scale,
        )
        self.use_fp16 = configure_mixed_precision(self.device)

        self.template_engine = RecordTemplateEngine(config.template_path)
        self.template_hash = self.template_engine.compute_template_hash()

        self.model = SentenceTransformer(config.model_name, device=self.device)
        get_dim_fn = getattr(self.model, "get_embedding_dimension", None)
        dim: Optional[int] = None
        if get_dim_fn is not None:
            try:
                res = get_dim_fn()
                if isinstance(res, (int, float)):
                    dim = int(res)
            except Exception:
                pass
        if dim is None and hasattr(self.model, "get_sentence_embedding_dimension"):
            try:
                res = self.model.get_sentence_embedding_dimension()
                if isinstance(res, (int, float)):
                    dim = int(res)
            except Exception:
                pass
        if dim is None:
            # Fallback encode test
            test_emb = self.model.encode(["test"], convert_to_numpy=True)
            dim = int(test_emb.shape[1])
        self._dimension: int = int(dim)

    @property
    def dimension(self) -> int:
        """Embedding dimension."""
        return self._dimension

    def serialize_record(self, record: BibliographicRecord | dict[str, Any]) -> str:
        """Convert a record into dense descriptive text representation."""
        return self.template_engine.render(record)

    def embed_texts(
        self, texts: Sequence[str], batch_size: Optional[int] = None
    ) -> np.ndarray:
        """Generate dense L2-normalized sentence embeddings for a list of texts."""
        if not texts:
            return np.empty((0, self.dimension), dtype=np.float32)

        text_list = list(texts)
        eff_batch_size = batch_size or self.batch_size
        # TODO: If sentence-transformers adds support for precision="float16" in encode(),
        # pass precision="float16" when self.use_fp16 and self.device == "cuda".
        # Currently SentenceTransformer.encode only supports ("float32", "int8", "uint8", "binary", "ubinary").
        embeddings = self.model.encode(
            text_list,
            batch_size=eff_batch_size,
            show_progress_bar=False,
            normalize_embeddings=True,
            convert_to_numpy=True,
        )
        return embeddings.astype(np.float32)

    def embed_records(
        self, records: Sequence[BibliographicRecord], batch_size: Optional[int] = None
    ) -> np.ndarray:
        """Serialize and encode a batch of BibliographicRecord models."""
        texts = [self.serialize_record(r) for r in records]
        return self.embed_texts(texts, batch_size=batch_size)

    def encode_batch_dense(self, texts: Sequence[str]) -> np.ndarray:
        """Batch dense encoding for vector index ingestion (protocol parity with LLMJudgeBackend)."""
        return self.embed_texts(texts)


    def encode_target(self, text: str, retain_cache: bool = True) -> Any:
        """Single-pass target encoding for coarse retrieval (protocol parity with LLMJudgeBackend)."""
        from reconciliator.judge.protocol import TargetEncoding

        dense_vecs = self.embed_texts([text])
        dense_vec = (
            dense_vecs[0] if len(dense_vecs) > 0 else np.zeros(self.dimension, dtype=np.float32)
        )
        return TargetEncoding(
            dense_vector=dense_vec,
            kv_cache=None,
            base_seq_len=0,
        )
