"""Wide Pilot: BigClust Multiplex WEMI Clusterer on Metal GPU.

Performs two-phase hierarchical unsupervised clustering:
1. Macro-level Work Family Partitioning: Continuous MSSC clustering into canonical Work families.
2. Micro-level Expression Realization Partitioning: Hierarchical MSSC sub-clustering into distinct
   realizations (translations, editions, formats) with automatic k* discovery via Davies-Bouldin index.
"""

from __future__ import annotations

import hashlib
import logging
import time
from collections import defaultdict
from typing import Any, Optional, Sequence

import numpy as np
import torch

from reconciliator.clustering.bigclust.engine import BigClustMetal
from reconciliator.database import WEMIRepository
from reconciliator.embedding.embedder import SentenceEmbedder
from reconciliator.models import (
    CorpusLinkage,
    ExpressionCluster,
    ManifestationCluster,
    MatchRecord,
    MultiplexClusterResult,
    WorkCluster,
)

logger = logging.getLogger(__name__)


class BigClustMultiplexClusterer:
    """Two-tier unsupervised WEMI multiplex clusterer powered by BigClust Metal."""

    def __init__(
        self,
        corpus_repo: WEMIRepository,
        embedder: Optional[SentenceEmbedder] = None,
        max_work_clusters: int = 50,
        max_expression_clusters_per_work: int = 10,
        device: Optional[torch.device] = None,
    ) -> None:
        self.corpus_repo = corpus_repo
        self.embedder = embedder or SentenceEmbedder()
        self.max_work_clusters = max_work_clusters
        self.max_expression_clusters_per_work = max_expression_clusters_per_work

        if device is not None:
            self.device = device
        elif torch.backends.mps.is_available():
            self.device = torch.device("mps")
        elif torch.cuda.is_available():
            self.device = torch.device("cuda")
        else:
            self.device = torch.device("cpu")

    def cluster_records(
        self,
        records: Sequence[MatchRecord],
        target_work_k: Optional[int] = None,
    ) -> MultiplexClusterResult:
        """Execute two-phase Work and Expression clustering across given MatchRecords."""
        start_time = time.time()
        if not records:
            return MultiplexClusterResult(records_clustered=0, elapsed_seconds=0.0)

        logger.info(
            "Starting BigClust Wide Pilot on %d records using device: %s",
            len(records),
            self.device,
        )

        # -------------------------------------------------------------
        # Phase 1: Macro-level Work Family Partitioning
        # -------------------------------------------------------------
        work_texts = [self._format_work_text(r) for r in records]
        work_embeddings = self.embedder.embed_texts(work_texts)
        if isinstance(work_embeddings, np.ndarray):
            work_tensor = torch.from_numpy(work_embeddings).float().to(self.device)
        else:
            work_tensor = torch.tensor(work_embeddings, dtype=torch.float32, device=self.device)

        k_work_max = min(self.max_work_clusters, len(records))
        work_engine = BigClustMetal(
            k_max=k_work_max,
            k_min=min(2, k_work_max),
            device=self.device,
        )

        work_result = work_engine.fit(work_tensor, target_k=target_work_k)
        work_assignments = work_result.assignments.cpu().numpy()

        logger.info(
            "Work Family Partitioning complete: discovered %d Work families (DB Index: %.4f)",
            work_result.optimal_k,
            work_result.optimal_metrics.davies_bouldin,
        )

        # Group records by work cluster index
        work_groups: dict[int, list[tuple[int, MatchRecord]]] = defaultdict(list)
        for idx, r in enumerate(records):
            w_idx = int(work_assignments[idx])
            work_groups[w_idx].append((idx, r))

        work_clusters: list[WorkCluster] = []
        expression_clusters: list[ExpressionCluster] = []
        manifestation_clusters: list[ManifestationCluster] = []
        corpus_linkages: list[CorpusLinkage] = []

        # -------------------------------------------------------------
        # Phase 2: Micro-level Expression Realization Partitioning
        # -------------------------------------------------------------
        for w_idx, members in work_groups.items():
            member_recs = [r for _, r in members]
            canon_rec = self._select_canonical_record(member_recs)

            # Mint Work Cluster ID
            w_hash = hashlib.sha256(
                f"work::{canon_rec.author_key or ''}::{canon_rec.title_stem or canon_rec.title}".encode(
                    "utf-8"
                )
            ).hexdigest()[:16]
            work_cluster_id = f"va_work_{w_hash}"

            w_cluster = WorkCluster(
                cluster_id=work_cluster_id,
                work_type=canon_rec.work_type,
                canonical_work_curie=f"reconciliator:{work_cluster_id}",
                canonical_title=canon_rec.title,
                canonical_author=canon_rec.author_key or None,
                authority_status="provisional",
                canonical_access_point=(
                    f"{canon_rec.author_key or ''} - {canon_rec.title}".strip(" -")
                    or canon_rec.title
                ),
            )
            work_clusters.append(w_cluster)

            if len(members) == 1:
                # Single record work family -> trivial single expression
                rec_idx, rec = members[0]
                expr_cluster_id = f"va_expr_{w_hash}_0"
                man_cluster_id = f"va_man_{w_hash}_0"

                expression_clusters.append(
                    ExpressionCluster(
                        cluster_id=expr_cluster_id,
                        work_cluster_id=work_cluster_id,
                        content_type="text",
                        language=(rec.language or "und").lower(),
                    )
                )
                manifestation_clusters.append(
                    ManifestationCluster(
                        cluster_id=man_cluster_id,
                        canonical_record_id=rec.record_id,
                        publication_year=rec.year,
                        member_record_count=1,
                    )
                )
                corpus_linkages.append(
                    CorpusLinkage(
                        record_id=rec.record_id,
                        work_cluster_id=work_cluster_id,
                        expression_cluster_id=expr_cluster_id,
                        manifestation_cluster_id=man_cluster_id,
                        confidence=1.0,
                    )
                )
                continue

            # Multi-record Work Family: Hierarchical Expression Sub-clustering
            expr_texts = [self._format_expression_text(r) for r in member_recs]
            expr_embeddings = self.embedder.embed_texts(expr_texts)
            if isinstance(expr_embeddings, np.ndarray):
                expr_tensor = torch.from_numpy(expr_embeddings).float().to(self.device)
            else:
                expr_tensor = torch.tensor(expr_embeddings, dtype=torch.float32, device=self.device)

            k_expr_max = min(self.max_expression_clusters_per_work, len(members))
            expr_engine = BigClustMetal(
                k_max=k_expr_max,
                k_min=1,
                device=self.device,
            )

            expr_result = expr_engine.fit(expr_tensor)
            expr_assignments = expr_result.assignments.cpu().numpy()

            logger.debug(
                "Work '%s' partitioned into %d Expression realizations (DB: %.4f)",
                w_cluster.canonical_title,
                expr_result.optimal_k,
                expr_result.optimal_metrics.davies_bouldin,
            )

            # Create Expression clusters and link records
            for local_idx, (global_idx, rec) in enumerate(members):
                e_sub_idx = int(expr_assignments[local_idx])
                expr_cluster_id = f"va_expr_{w_hash}_{e_sub_idx}"
                man_cluster_id = f"va_man_{w_hash}_{e_sub_idx}_{rec.record_id}"

                # Ensure expression cluster exists in list
                if not any(ec.cluster_id == expr_cluster_id for ec in expression_clusters):
                    expression_clusters.append(
                        ExpressionCluster(
                            cluster_id=expr_cluster_id,
                            work_cluster_id=work_cluster_id,
                            content_type=self._infer_content_type(rec),
                            language=(rec.language or "und").lower(),
                        )
                    )

                manifestation_clusters.append(
                    ManifestationCluster(
                        cluster_id=man_cluster_id,
                        canonical_record_id=rec.record_id,
                        publication_year=rec.year,
                        member_record_count=1,
                    )
                )
                corpus_linkages.append(
                    CorpusLinkage(
                        record_id=rec.record_id,
                        work_cluster_id=work_cluster_id,
                        expression_cluster_id=expr_cluster_id,
                        manifestation_cluster_id=man_cluster_id,
                        confidence=round(1.0 / (1.0 + expr_result.optimal_metrics.davies_bouldin), 4),
                    )
                )

        elapsed = time.time() - start_time
        logger.info(
            "BigClust Wide Pilot finished in %.2fs: %d Works, %d Expressions, %d Records",
            elapsed,
            len(work_clusters),
            len(expression_clusters),
            len(records),
        )

        return MultiplexClusterResult(
            work_clusters_count=len(work_clusters),
            expression_clusters_count=len(expression_clusters),
            manifestation_clusters_count=len(manifestation_clusters),
            records_clustered=len(records),
            elapsed_seconds=round(elapsed, 3),
        )

    def _format_work_text(self, record: MatchRecord) -> str:
        """Format high-level work representation focusing on intellectual creation."""
        parts = []
        if record.author_key:
            parts.append(f"Author: {record.author_key}")
        if record.uniform_title:
            parts.append(f"Uniform Title: {record.uniform_title}")
        elif record.title:
            parts.append(f"Title: {record.title}")
        if record.work_type:
            parts.append(f"Type: {record.work_type}")
        return " | ".join(parts) if parts else record.record_id

    def _format_expression_text(self, record: MatchRecord) -> str:
        """Format expression-level representation capturing language, revision, translator."""
        parts = []
        if record.title:
            parts.append(f"Title: {record.title}")
        if record.language:
            parts.append(f"Language: {record.language}")
        if record.work_type:
            parts.append(f"Format: {record.work_type}")
        # Translators, edition, or series keys
        if "translators_signature" in record.keys:
            parts.append(f"Translator: {record.keys['translators_signature'][0]}")
        if record.volume_designation:
            parts.append(f"Edition: {record.volume_designation}")
        return " | ".join(parts) if parts else record.record_id

    def _select_canonical_record(self, records: list[MatchRecord]) -> MatchRecord:
        """Select best representative record for a work cluster."""
        return sorted(
            records,
            key=lambda r: (
                -(1 if r.uniform_title else 0),
                -(len(r.title or "")),
                r.record_id,
            ),
        )[0]

    def _infer_content_type(self, record: MatchRecord) -> str:
        """Infer RDA content type from work type."""
        wt = record.work_type
        if wt == "audio_nonmusic":
            return "spoken_word"
        if wt == "music":
            return "performed_music"
        if wt == "moving_image":
            return "two_dimensional_moving_image"
        if wt == "cartography":
            return "cartographic_image"
        return "text"
