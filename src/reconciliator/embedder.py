"""SentenceTransformers wrapper with dynamic dimension discovery and hardware acceleration."""

from __future__ import annotations

from typing import Any, Optional
import numpy as np
from sentence_transformers import SentenceTransformer

from reconciliator.config import EmbeddingConfig
from reconciliator.device import get_optimal_batch_size, resolve_device
from reconciliator.models import BibliographicRecord
from reconciliator.template import RecordTemplateEngine


class SentenceEmbedder:
    """Wraps SentenceTransformer with template-based serialization and hardware acceleration."""

    def __init__(self, config: EmbeddingConfig) -> None:
        self.config = config
        self.device = resolve_device(config.device)
        self.batch_size = get_optimal_batch_size(config.batch_size, self.device)

        self.template_engine = RecordTemplateEngine(config.template_path)
        self.template_hash = self.template_engine.compute_template_hash()

        self.model = SentenceTransformer(config.model_name, device=self.device)
        dim = self.model.get_sentence_embedding_dimension()
        if dim is None:
            # Fallback encode test
            test_emb = self.model.encode(["test"], convert_to_numpy=True)
            dim = int(test_emb.shape[1])
        self.dimension: int = int(dim)

    def serialize_record(self, record: BibliographicRecord | dict[str, Any]) -> str:
        """Convert a record into dense descriptive text representation."""
        return self.template_engine.render(record)

    def embed_texts(self, texts: list[str], batch_size: Optional[int] = None) -> np.ndarray:
        """Generate dense L2-normalized sentence embeddings for a list of texts."""
        if not texts:
            return np.empty((0, self.dimension), dtype=np.float32)

        eff_batch_size = batch_size or self.batch_size
        embeddings = self.model.encode(
            texts,
            batch_size=eff_batch_size,
            show_progress_bar=False,
            normalize_embeddings=True,
            convert_to_numpy=True,
        )
        return embeddings.astype(np.float32)

    def embed_records(
        self, records: list[BibliographicRecord], batch_size: Optional[int] = None
    ) -> np.ndarray:
        """Serialize and encode a batch of BibliographicRecord models."""
        texts = [self.serialize_record(r) for r in records]
        return self.embed_texts(texts, batch_size=batch_size)

