"""Wide Pilot: BigClust Multiplex WEMI Clusterer on Metal GPU.

Performs two-phase hierarchical unsupervised clustering:
1. Macro-level Work Family Partitioning: Continuous MSSC clustering into canonical Work families.
2. Micro-level Expression Realization Partitioning: Hierarchical MSSC sub-clustering into distinct
   realizations (translations, editions, formats) with automatic k* discovery via Davies-Bouldin index.
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from typing import Optional, Sequence

import numpy as np
import torch
from rapidfuzz import fuzz

from reconciliator.authority.lifecycle import ClusterIdentityManager
from reconciliator.clustering.bigclust.engine import BigClustMetal
from reconciliator.clustering.protocol import BaseCorpusClusterer
from reconciliator.clustering.wemi_builder import (
    WEMIClusterBuilder,
    infer_rda_content_type,
    select_canonical_work_record,
)
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


class BigClustMultiplexClusterer(BaseCorpusClusterer):
    """Two-tier unsupervised WEMI multiplex clusterer powered by BigClust Metal."""

    def __init__(
        self,
        corpus_repo: WEMIRepository,
        wemi_repo: Optional[WEMIRepository] = None,
        embedder: Optional[VectorEmbedder] = None,
        max_work_clusters: int = 50,
        max_expression_clusters_per_work: int = 10,
        similarity_threshold: float = 0.70,
        identity_manager: Optional[ClusterIdentityManager] = None,
        device: Optional[torch.device] = None,
    ) -> None:
        self.corpus_repo = corpus_repo
        self.wemi_repo = wemi_repo if wemi_repo is not None else corpus_repo
        self._embedder = embedder
        self.max_work_clusters = max_work_clusters
        self.max_expression_clusters_per_work = max_expression_clusters_per_work
        self.similarity_threshold = similarity_threshold
        self.identity_manager = identity_manager or ClusterIdentityManager(self.corpus_repo)
        self.builder = WEMIClusterBuilder(self.wemi_repo, self.identity_manager)
        self._template_engine = None

        from reconciliator.utils.device import resolve_compute_device

        self.device = resolve_compute_device(device)

    @property
    def embedder(self) -> VectorEmbedder:
        """Lazily initialize default SentenceEmbedder if not injected."""
        if self._embedder is None:
            from reconciliator.config import EmbeddingConfig
            from reconciliator.embedding.embedder import SentenceEmbedder

            self._embedder = SentenceEmbedder(EmbeddingConfig())
        return self._embedder


    def cluster_corpus(
        self,
        origins: Optional[list[str]] = None,
        target_work_k: Optional[int] = None,
        incremental: bool = False,
    ) -> MultiplexClusterResult:
        """Fetch staged records from repository, execute BigClust, and persist WEMI clusters."""
        actual_origins = origins or self.corpus_repo.non_authoritative_sources() or None
        records = self.corpus_repo.get_all_match_records(origins=actual_origins)
        if not records:
            return MultiplexClusterResult(records_clustered=0, elapsed_seconds=0.0)

        return self.cluster_records(
            records, target_work_k=target_work_k, persist=True, incremental=incremental
        )

    def _cluster_records_internal(
        self,
        records: Sequence[MatchRecord],
        work_embeddings: np.ndarray,
        target_work_k: Optional[int] = None,
    ) -> tuple[
        list[WorkCluster],
        list[ExpressionCluster],
        list[ManifestationCluster],
        list[CorpusLinkage],
        dict[str, np.ndarray],
    ]:
        if not records:
            return [], [], [], [], {}

        if len(records) == 1:
            rec = records[0]
            w_cluster = self.builder.build_work_cluster_from_records([rec], canonical_record=rec)
            work_cluster_id = w_cluster.cluster_id
            expr_cluster = self.builder.create_expression_cluster(
                work_cluster_id=work_cluster_id,
                content_type=infer_rda_content_type(rec),
                language=(rec.language or "und").lower(),
                seed=f"{work_cluster_id}_0",
            )
            man_cluster = self.builder.create_manifestation_cluster(
                canonical_record_id=rec.record_id,
                publication_year=rec.year,
                seed=f"{work_cluster_id}_0_{rec.record_id}",
            )
            linkage = CorpusLinkage(
                record_id=rec.record_id,
                work_cluster_id=work_cluster_id,
                expression_cluster_id=expr_cluster.cluster_id,
                manifestation_cluster_id=man_cluster.cluster_id,
                confidence=1.0,
            )
            return (
                [w_cluster],
                [expr_cluster],
                [man_cluster],
                [linkage],
                {work_cluster_id: work_embeddings[0]},
            )

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

        work_groups: dict[int, list[tuple[int, MatchRecord]]] = defaultdict(list)
        for idx, r in enumerate(records):
            w_idx = int(work_assignments[idx])
            work_groups[w_idx].append((idx, r))

        work_clusters: list[WorkCluster] = []
        expression_clusters: list[ExpressionCluster] = []
        manifestation_clusters: list[ManifestationCluster] = []
        corpus_linkages: list[CorpusLinkage] = []
        centroids_map: dict[str, np.ndarray] = {}

        for w_idx, members in work_groups.items():
            member_recs = [r for _, r in members]
            member_indices = [idx for idx, _ in members]
            sub_embs = work_embeddings[member_indices]
            canon_rec = self._select_canonical_record(member_recs)
            w_cluster = self.builder.build_work_cluster_from_records(
                member_recs, canonical_record=canon_rec
            )
            work_cluster_id = w_cluster.cluster_id
            work_clusters.append(w_cluster)
            centroids_map[work_cluster_id] = np.mean(sub_embs, axis=0)

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

            # Multi-record Work Family: Hierarchical Expression Sub-clustering
            expr_texts = [self._format_expression_text(r) for r in member_recs]
            expr_embeddings = self._embed_texts_cached(expr_texts, entity_type="expression")
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

            for local_idx, (global_idx, rec) in enumerate(members):
                e_sub_idx = int(expr_assignments[local_idx])
                expr_cluster = self.builder.create_expression_cluster(
                    work_cluster_id=work_cluster_id,
                    content_type=infer_rda_content_type(rec),
                    language=(rec.language or "und").lower(),
                    version_designation=rec.volume_designation,
                    seed=f"{work_cluster_id}_{e_sub_idx}",
                )

                if not any(ec.cluster_id == expr_cluster.cluster_id for ec in expression_clusters):
                    expression_clusters.append(expr_cluster)

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
                        confidence=round(1.0 / (1.0 + expr_result.optimal_metrics.davies_bouldin), 4),
                    )
                )

        return (
            work_clusters,
            expression_clusters,
            manifestation_clusters,
            corpus_linkages,
            centroids_map,
        )

    def cluster_records(
        self,
        records: Sequence[MatchRecord],
        target_work_k: Optional[int] = None,
        persist: bool = False,
        incremental: bool = False,
    ) -> MultiplexClusterResult:
        """Execute two-phase Work and Expression clustering across given MatchRecords."""
        start_time = time.time()
        if not records:
            return MultiplexClusterResult(records_clustered=0, elapsed_seconds=0.0)

        logger.info(
            "Starting BigClust Wide Pilot on %d records using device: %s (incremental: %s)",
            len(records),
            self.device,
            incremental,
        )

        work_texts = [self._format_work_text(r) for r in records]
        work_embeddings = self._embed_texts_cached(work_texts, entity_type="work")

        model_name = getattr(
            getattr(self.embedder, "config", None), "model_name", "default_embedder"
        )
        existing_centroids: dict[str, np.ndarray] = {}
        if incremental and self.wemi_repo is not None and hasattr(self.wemi_repo, "get_cluster_centroids"):
            try:
                res = self.wemi_repo.get_cluster_centroids(model_name=model_name)
                if isinstance(res, dict):
                    existing_centroids = res
            except Exception as e:
                logger.debug("Failed to retrieve existing cluster centroids: %s", e)

        work_clusters: list[WorkCluster] = []
        expression_clusters: list[ExpressionCluster] = []
        manifestation_clusters: list[ManifestationCluster] = []
        corpus_linkages: list[CorpusLinkage] = []
        centroids_map: dict[str, np.ndarray] = {}

        assigned_indices: set[int] = set()
        assigned_cluster_members: dict[str, list[int]] = defaultdict(list)

        if existing_centroids:
            c_ids = list(existing_centroids.keys())
            c_matrix = np.stack([existing_centroids[cid] for cid in c_ids])
            c_tensor = torch.from_numpy(c_matrix).float().to(self.device)
            if isinstance(work_embeddings, np.ndarray):
                w_tensor = torch.from_numpy(work_embeddings).float().to(self.device)
            else:
                w_tensor = torch.tensor(work_embeddings, dtype=torch.float32, device=self.device)

            c_norm = torch.nn.functional.normalize(c_tensor, dim=1)
            w_norm = torch.nn.functional.normalize(w_tensor, dim=1)
            sims = torch.mm(w_norm, c_norm.t())
            max_sims, best_indices = torch.max(sims, dim=1)
            max_sims_np = max_sims.cpu().numpy()
            best_indices_np = best_indices.cpu().numpy()

            cluster_author_cache: dict[str, Optional[str]] = {}
            for idx, rec in enumerate(records):
                sim = float(max_sims_np[idx])
                if sim >= self.similarity_threshold:
                    c_id = c_ids[int(best_indices_np[idx])]
                    compatible = True
                    try:
                        if c_id not in cluster_author_cache:
                            existing_w = self.wemi_repo.get_work_cluster_full(c_id)
                            cluster_author_cache[c_id] = (
                                existing_w.canonical_author.lower()
                                if existing_w and existing_w.canonical_author
                                else None
                            )
                        canon_author = cluster_author_cache[c_id]
                        if canon_author and rec.author_key:
                            if fuzz.ratio(canon_author, rec.author_key.lower()) < 60:
                                compatible = False
                    except Exception:
                        pass

                    if compatible:
                        assigned_indices.add(idx)
                        assigned_cluster_members[c_id].append(idx)
                        expr_cluster = self.builder.create_expression_cluster(
                            work_cluster_id=c_id,
                            content_type=infer_rda_content_type(rec),
                            language=(rec.language or "und").lower(),
                            version_designation=rec.volume_designation,
                            seed=f"{c_id}_{rec.record_id}",
                        )
                        expression_clusters.append(expr_cluster)

                        man_cluster = self.builder.create_manifestation_cluster(
                            canonical_record_id=rec.record_id,
                            publication_year=rec.year,
                            seed=f"{c_id}_{rec.record_id}",
                        )
                        manifestation_clusters.append(man_cluster)

                        corpus_linkages.append(
                            CorpusLinkage(
                                record_id=rec.record_id,
                                work_cluster_id=c_id,
                                expression_cluster_id=expr_cluster.cluster_id,
                                manifestation_cluster_id=man_cluster.cluster_id,
                                confidence=round(float(sim), 4),
                            )
                        )

            for c_id, member_idxs in assigned_cluster_members.items():
                old_c = existing_centroids[c_id]
                new_embs = work_embeddings[member_idxs]
                centroids_map[c_id] = (old_c * 2.0 + np.sum(new_embs, axis=0)) / (2.0 + len(member_idxs))

        unassigned_indices = [i for i in range(len(records)) if i not in assigned_indices]

        if unassigned_indices:
            unassigned_records = [records[i] for i in unassigned_indices]
            unassigned_embeddings = work_embeddings[unassigned_indices]

            (
                new_w,
                new_e,
                new_m,
                new_l,
                new_c_map,
            ) = self._cluster_records_internal(
                records=unassigned_records,
                work_embeddings=unassigned_embeddings,
                target_work_k=target_work_k,
            )
            work_clusters.extend(new_w)
            expression_clusters.extend(new_e)
            manifestation_clusters.extend(new_m)
            corpus_linkages.extend(new_l)
            centroids_map.update(new_c_map)

        if persist:
            self.builder.persist_batch(
                work_clusters, expression_clusters, manifestation_clusters, corpus_linkages
            )

        if centroids_map and self.wemi_repo is not None and hasattr(self.wemi_repo, "store_cluster_centroids"):
            try:
                self.wemi_repo.store_cluster_centroids(centroids_map, model_name=model_name)
            except Exception as e:
                logger.debug("Failed to store cluster centroids: %s", e)

        elapsed = time.time() - start_time
        total_work_count = len(work_clusters) + len(assigned_cluster_members)

        logger.info(
            "BigClust Wide Pilot finished in %.2fs: %d Works, %d Expressions, %d Records",
            elapsed,
            total_work_count,
            len(expression_clusters),
            len(records),
        )

        return MultiplexClusterResult(
            work_clusters_count=total_work_count,
            expression_clusters_count=len(expression_clusters),
            manifestation_clusters_count=len(manifestation_clusters),
            records_clustered=len(records),
            elapsed_seconds=round(elapsed, 3),
        )

    def _select_canonical_record(self, records: Sequence[MatchRecord]) -> MatchRecord:
        """Select best representative record for a work cluster."""
        return select_canonical_work_record(records)

    def _infer_content_type(self, record: MatchRecord) -> str:
        """Infer RDA content type from work type."""
        return infer_rda_content_type(record)

    def _embed_texts_cached(
        self, texts: Sequence[str], entity_type: str = "work"
    ) -> np.ndarray:
        """Embed a sequence of texts, utilizing WEMIRepository vector cache when available."""
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
