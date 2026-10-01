"""Vector cache and centroid repository mixin."""

from __future__ import annotations

import logging
from collections import OrderedDict
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, Optional

import numpy as np
from sqlalchemy import and_, delete, insert, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from reconciliator.database.schema import entity_embeddings_table

if TYPE_CHECKING:
    from reconciliator.embedding.protocol import VectorEmbedder

logger = logging.getLogger(__name__)


class VectorCacheRepositoryMixin:
    """Repository mixin for dense vector embedding caching and cluster centroids."""

    def get_cached_embeddings(
        self,
        hashes: Sequence[str],
        model_name: str,
        batch_size: int = 1000,
    ) -> dict[str, np.ndarray]:
        """Retrieve cached dense vectors by content hash for a given model.

        Args:
            hashes: Sequence of SHA-256 content hashes.
            model_name: The embedding model identifier used when encoding.
            batch_size: Chunk size for SQL query IN-clauses.

        Returns:
            Dictionary mapping content_hash to 1D float32 numpy array.
        """
        if not hashes:
            return {}

        unique_hashes = list(OrderedDict.fromkeys(hashes))
        cached: dict[str, np.ndarray] = {}

        with self.get_read_connection() as conn:
            for i in range(0, len(unique_hashes), batch_size):
                chunk = unique_hashes[i : i + batch_size]
                stmt = select(
                    entity_embeddings_table.c.content_hash,
                    entity_embeddings_table.c.dimension,
                    entity_embeddings_table.c.embedding_bytes,
                ).where(
                    and_(
                        entity_embeddings_table.c.model_name == model_name,
                        entity_embeddings_table.c.content_hash.in_(chunk),
                    )
                )
                for h, dim, raw_bytes in conn.execute(stmt):
                    if raw_bytes:
                        vec = np.frombuffer(raw_bytes, dtype=np.float32)
                        if len(vec) == dim:
                            cached[h] = vec
        return cached

    def store_cached_embeddings(
        self,
        records: Sequence[dict[str, Any]],
        batch_size: int = 1000,
    ) -> int:
        """Store dense vectors in the embedding cache table.

        Each item in `records` must contain:
        - content_hash: str (SHA-256)
        - model_name: str
        - entity_type: str ('work', 'expression', 'manifestation', etc.)
        - dimension: int
        - embedding_bytes: bytes (or raw float32 ndarray)
        """
        if not records:
            return 0

        normalized_records = []
        for r in records:
            item = dict(r)
            raw = item.get("embedding_bytes")
            if isinstance(raw, np.ndarray):
                item["embedding_bytes"] = raw.astype(np.float32).tobytes()
                item["dimension"] = raw.shape[-1]
            normalized_records.append(item)

        inserted_count = 0
        with self.get_connection() as conn:
            for i in range(0, len(normalized_records), batch_size):
                chunk = normalized_records[i : i + batch_size]
                if self.engine.dialect.name == "postgresql":
                    stmt = pg_insert(entity_embeddings_table).values(chunk)
                    stmt = stmt.on_conflict_do_nothing(
                        index_elements=["content_hash", "model_name"]
                    )
                    res = conn.execute(stmt)
                    inserted_count += (
                        res.rowcount if res.rowcount is not None and res.rowcount >= 0 else len(chunk)
                    )
                else:
                    stmt = insert(entity_embeddings_table).prefix_with("OR IGNORE")
                    try:
                        res = conn.execute(stmt, chunk)
                        inserted_count += (
                            res.rowcount if res.rowcount is not None and res.rowcount >= 0 else len(chunk)
                        )
                    except Exception:
                        for row in chunk:
                            try:
                                conn.execute(stmt, [row])
                                inserted_count += 1
                            except Exception:
                                pass
        return inserted_count

    def delete_cached_embeddings(self, model_name: Optional[str] = None) -> int:
        """Clear cached embeddings, optionally filtering by model name."""
        stmt = delete(entity_embeddings_table)
        if model_name:
            stmt = stmt.where(entity_embeddings_table.c.model_name == model_name)
        with self.get_connection() as conn:
            res = conn.execute(stmt)
            return res.rowcount if res.rowcount is not None and res.rowcount >= 0 else 0

    def get_cluster_centroids(self, model_name: str) -> dict[str, np.ndarray]:
        """Retrieve stored cluster centroid vectors for warm-starting incremental clustering."""
        stmt = select(
            entity_embeddings_table.c.content_hash,
            entity_embeddings_table.c.dimension,
            entity_embeddings_table.c.embedding_bytes,
        ).where(
            and_(
                entity_embeddings_table.c.model_name == model_name,
                entity_embeddings_table.c.entity_type == "cluster_centroid",
            )
        )
        centroids: dict[str, np.ndarray] = {}
        with self.get_read_connection() as conn:
            for cluster_id, dim, raw_bytes in conn.execute(stmt):
                if raw_bytes:
                    vec = np.frombuffer(raw_bytes, dtype=np.float32)
                    if len(vec) == dim:
                        centroids[cluster_id] = vec
        return centroids

    def store_cluster_centroids(
        self, centroids: dict[str, np.ndarray], model_name: str
    ) -> int:
        """Persist or update cluster centroid vectors for future incremental runs."""
        if not centroids:
            return 0
        records = [
            {
                "content_hash": cluster_id,
                "model_name": model_name,
                "entity_type": "cluster_centroid",
                "dimension": len(vec),
                "embedding_bytes": vec.astype(np.float32).tobytes(),
            }
            for cluster_id, vec in centroids.items()
        ]
        return self.store_cached_embeddings(records)

    def embed_texts_cached(
        self,
        embedder: VectorEmbedder,
        texts: Sequence[str],
        entity_type: str = "work",
        batch_size: int = 1000,
    ) -> np.ndarray:
        """Embed a sequence of texts using embedder, caching and reusing vectors from entity_embeddings.

        Args:
            embedder: VectorEmbedder instance to generate missing embeddings.
            texts: Sequence of strings to embed.
            entity_type: Entity classification ('work', 'expression', 'manifestation', 'agent', etc.).
            batch_size: Chunk size for querying cache and model encoding.

        Returns:
            2D numpy float32 array of shape (len(texts), dimension).
        """
        if not texts:
            dim = getattr(embedder, "dimension", 768)
            return np.empty((0, dim), dtype=np.float32)

        import hashlib

        text_list = list(texts)
        hashes = [hashlib.sha256(t.encode("utf-8")).hexdigest() for t in text_list]
        model_name = getattr(
            getattr(embedder, "config", None), "model_name", "default_embedder"
        )
        dim = getattr(embedder, "dimension", None)

        cached_vectors: dict[str, np.ndarray] = {}
        try:
            cached_vectors = self.get_cached_embeddings(hashes, model_name=model_name, batch_size=batch_size)
        except Exception as e:
            logger.debug("Failed to retrieve cached embeddings: %s", e)

        # Identify missing texts that need model computation
        missing_items: list[tuple[str, str]] = []
        seen_missing_hashes: set[str] = set()

        for t, h in zip(text_list, hashes):
            if h not in cached_vectors and h not in seen_missing_hashes:
                missing_items.append((t, h))
                seen_missing_hashes.add(h)

        newly_computed: dict[str, np.ndarray] = {}
        if missing_items:
            missing_texts = [item[0] for item in missing_items]
            if hasattr(embedder, "encode_batch_dense"):
                computed_arr = embedder.encode_batch_dense(missing_texts)
            else:
                computed_arr = embedder.embed_texts(missing_texts)
            if dim is None and len(computed_arr) > 0:
                dim = computed_arr.shape[1]
            for (_, h), vec in zip(missing_items, computed_arr):
                newly_computed[h] = vec

            # Persist newly computed vectors to repository cache
            if dim is not None:
                records_to_cache = [
                    {
                        "content_hash": h,
                        "model_name": model_name,
                        "entity_type": entity_type,
                        "dimension": dim,
                        "embedding_bytes": vec.astype(np.float32).tobytes(),
                    }
                    for h, vec in newly_computed.items()
                ]
                try:
                    self.store_cached_embeddings(records_to_cache, batch_size=batch_size)
                except Exception as e:
                    logger.debug("Failed to store cached embeddings: %s", e)

            # Zero-copy fast path when entire batch was computed and no duplicates
            if not cached_vectors and len(missing_items) == len(text_list):
                return computed_arr.astype(np.float32, copy=False)

        all_vectors = {**cached_vectors, **newly_computed}
        first_vec = next(iter(all_vectors.values()), None)
        actual_dim = len(first_vec) if first_vec is not None else (dim or 768)

        if not all_vectors:
            return np.empty((len(text_list), actual_dim), dtype=np.float32)

        return np.array([all_vectors[h] for h in hashes], dtype=np.float32)
