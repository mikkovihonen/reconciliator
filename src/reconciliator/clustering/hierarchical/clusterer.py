"""GPU-accelerated Multi-Tier Hierarchical Agglomerative WEMI Clusterer."""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from collections.abc import Sequence
from typing import Optional

import numpy as np
import torch

from reconciliator.authority.lifecycle import ClusterIdentityManager
from reconciliator.utils.device import clear_accelerator_memory, resolve_compute_device
from reconciliator.clustering.hierarchical.dendrogram import GPUHierarchicalEngine
from reconciliator.clustering.protocol import BaseCorpusClusterer
from reconciliator.clustering.wemi_builder import (
    WEMIClusterBuilder,
    infer_rda_content_type,
    select_canonical_work_record,
)
from reconciliator.config.matching import HierarchicalClusterConfig
from reconciliator.database import WEMIRepository
from reconciliator.embedding.protocol import VectorEmbedder
from reconciliator.models import (
    CorpusLinkage,
    ExpressionCluster,
    ManifestationCluster,
    MatchRecord,
    MultiplexClusterResult,
    WorkCluster,
)

logger = logging.getLogger(__name__)


class HierarchicalWEMIClusterer(BaseCorpusClusterer):
    """Multi-tier WEMI HAC clusterer supporting GPU/MPS/CUDA/CPU."""

    def __init__(
        self,
        corpus_repo: WEMIRepository,
        wemi_repo: Optional[WEMIRepository] = None,
        embedder: Optional[VectorEmbedder] = None,
        config: Optional[HierarchicalClusterConfig] = None,
        identity_manager: Optional[ClusterIdentityManager] = None,
        device: Optional[torch.device] = None,
    ) -> None:
        self.corpus_repo = corpus_repo
        self.wemi_repo = wemi_repo if wemi_repo is not None else corpus_repo
        self._embedder = embedder
        self.config = config or HierarchicalClusterConfig()
        self.identity_manager = identity_manager or ClusterIdentityManager(self.corpus_repo)
        self.builder = WEMIClusterBuilder(self.wemi_repo, self.identity_manager)
        self._template_engine = None

        if device is not None:
            self.device = device
        else:
            self.device = resolve_compute_device(self.config.device)

        self.engine = GPUHierarchicalEngine(
            linkage=self.config.linkage,
            metric=self.config.metric,
            device=self.device,
        )

    @property
    def embedder(self) -> VectorEmbedder:
        """Lazily initialize vector embedder."""
        if self._embedder is None:
            from reconciliator.config import EmbeddingConfig
            from reconciliator.embedding.embedder import SentenceEmbedder

            self._embedder = SentenceEmbedder(EmbeddingConfig())
        return self._embedder

    def cluster_corpus(self, origins: Optional[list[str]] = None) -> MultiplexClusterResult:
        """Fetch staged records, run hierarchical clustering, and persist WEMI clusters."""
        actual_origins = origins or self.corpus_repo.non_authoritative_sources() or None
        records = self.corpus_repo.get_all_match_records(origins=actual_origins)
        if not records:
            return MultiplexClusterResult(records_clustered=0, elapsed_seconds=0.0)

        return self.cluster_records(records, persist=True)

    def cluster_records(
        self,
        records: Sequence[MatchRecord],
        persist: bool = False,
        incremental: bool = False,
    ) -> MultiplexClusterResult:
        """Cluster records across Work and Expression distance thresholds."""
        start_time = time.perf_counter()
        if not records:
            return MultiplexClusterResult(records_clustered=0, elapsed_seconds=0.0)

        work_texts = [self.format_work_text(r) for r in records]
        work_embeddings = self._embed_texts_cached(work_texts, entity_type="work")
        work_tensor = torch.from_numpy(work_embeddings).float()

        # Build Work-level dendrogram
        work_tree = self.engine.fit(work_tensor)
        work_assignments = work_tree.cut_tree(self.config.work_distance_threshold)

        work_groups: dict[int, list[tuple[int, MatchRecord]]] = defaultdict(list)
        for idx, rec in enumerate(records):
            w_idx = int(work_assignments[idx])
            work_groups[w_idx].append((idx, rec))

        work_clusters: list[WorkCluster] = []
        expression_clusters: list[ExpressionCluster] = []
        manifestation_clusters: list[ManifestationCluster] = []
        corpus_linkages: list[CorpusLinkage] = []

        for w_idx, members in work_groups.items():
            member_recs = [r for _, r in members]
            canon_rec = select_canonical_work_record(member_recs)
            w_cluster = self.builder.build_work_cluster_from_records(
                member_recs, canonical_record=canon_rec
            )
            work_cluster_id = w_cluster.cluster_id
            work_clusters.append(w_cluster)

            if len(members) == 1:
                rec_idx, rec = members[0]
                expr_cluster = self.builder.create_expression_cluster(
                    work_cluster_id=work_cluster_id,
                    content_type=infer_rda_content_type(rec),
                    language=(rec.language or "und").lower(),
                    seed=f"{work_cluster_id}_0",
                )
                expression_clusters.append(expr_cluster)

                man_cluster = self.builder.create_manifestation_cluster(
                    canonical_record_id=rec.record_id,
                    publication_year=rec.year,
                    seed=f"{work_cluster_id}_0_{rec.record_id}",
                )
                manifestation_clusters.append(man_cluster)

                corpus_linkages.append(
                    CorpusLinkage(
                        record_id=rec.record_id,
                        work_cluster_id=work_cluster_id,
                        expression_cluster_id=expr_cluster.cluster_id,
                        manifestation_cluster_id=man_cluster.cluster_id,
                        confidence=1.0,
                    )
                )
                continue

            # Multi-record Expression sub-clustering via expression distance threshold
            expr_texts = [self.format_expression_text(r) for r in member_recs]
            expr_embeddings = self._embed_texts_cached(expr_texts, entity_type="expression")
            expr_tensor = torch.from_numpy(expr_embeddings).float()

            expr_tree = self.engine.fit(expr_tensor)
            expr_assignments = expr_tree.cut_tree(self.config.expression_distance_threshold)

            expr_group_map: dict[int, list[tuple[int, MatchRecord]]] = defaultdict(list)
            for local_idx, (global_idx, rec) in enumerate(members):
                e_idx = int(expr_assignments[local_idx])
                expr_group_map[e_idx].append((local_idx, rec))

            for e_sub_idx, sub_members in expr_group_map.items():
                first_rec = sub_members[0][1]
                expr_cluster = self.builder.create_expression_cluster(
                    work_cluster_id=work_cluster_id,
                    content_type=infer_rda_content_type(first_rec),
                    language=(first_rec.language or "und").lower(),
                    version_designation=first_rec.volume_designation,
                    seed=f"{work_cluster_id}_{e_sub_idx}",
                )
                expression_clusters.append(expr_cluster)

                for _, rec in sub_members:
                    man_cluster = self.builder.create_manifestation_cluster(
                        canonical_record_id=rec.record_id,
                        publication_year=rec.year,
                        seed=f"{work_cluster_id}_{e_sub_idx}_{rec.record_id}",
                    )
                    manifestation_clusters.append(man_cluster)
                    corpus_linkages.append(
                        CorpusLinkage(
                            record_id=rec.record_id,
                            work_cluster_id=work_cluster_id,
                            expression_cluster_id=expr_cluster.cluster_id,
                            manifestation_cluster_id=man_cluster.cluster_id,
                            confidence=1.0,
                        )
                    )

        if persist:
            self.builder.persist_batch(
                work_clusters, expression_clusters, manifestation_clusters, corpus_linkages
            )

        elapsed = time.perf_counter() - start_time
        clear_accelerator_memory(self.device)

        return MultiplexClusterResult(
            work_clusters_count=len(work_clusters),
            expression_clusters_count=len(expression_clusters),
            manifestation_clusters_count=len(manifestation_clusters),
            records_clustered=len(records),
            elapsed_seconds=round(elapsed, 3),
        )

    def _embed_texts_cached(
        self, texts: Sequence[str], entity_type: str = "work"
    ) -> np.ndarray:
        """Embed a sequence of texts using cache if available."""
        if not texts:
            dim = getattr(self.embedder, "dimension", 768)
            return np.empty((0, dim), dtype=np.float32)
        if self.wemi_repo is not None and hasattr(self.wemi_repo, "embed_texts_cached"):
            res = self.wemi_repo.embed_texts_cached(self.embedder, texts, entity_type=entity_type)
            if isinstance(res, np.ndarray) and len(res) == len(texts):
                return res
        if hasattr(self.embedder, "encode_batch_dense"):
            return self.embedder.encode_batch_dense(list(texts))
        return self.embedder.embed_texts(list(texts))
