"""Hybrid Coarse-to-Fine WEMI Corpus Clusterer.

Combines high-throughput GPU continuous manifold optimization (BigClustMetal)
for macro Work family candidate discovery with deterministic bibliographical domain
verification (MultiplexGraphClusterer pairwise scoring, hard vetoes, and boundary reranking).
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from typing import Any, Optional, Sequence

import numpy as np
import torch
from rapidfuzz import fuzz

from reconciliator.authority.lifecycle import ClusterIdentityManager
from reconciliator.clustering.bigclust.engine import BigClustMetal
from reconciliator.clustering.evaluator import BibliographicalEdgeEvaluator
from reconciliator.clustering.protocol import BaseCorpusClusterer
from reconciliator.clustering.wemi_builder import (
    WEMIClusterBuilder,
    infer_rda_content_type,
    select_canonical_work_record,
)
from reconciliator.config import WEMIClusterConfig
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
from reconciliator.reconciliation import WorkMatcher
from reconciliator.utils import cluster_edges, extract_identifiers_from_target

logger = logging.getLogger(__name__)


class HybridBigClustGraphClusterer(BaseCorpusClusterer):
    """Hybrid Coarse-to-Fine WEMI Clusterer.

    Phase 1 (Coarse GPU Partitioning):
        Uses BigClustMetal to perform fast, continuous MSSC clustering on dense work embeddings
        with automatic k* discovery via Davies-Bouldin index. Discovers global candidate
        work neighborhoods across the whole corpus in sub-second GPU time without rigid blocking.

    Phase 2 (Fine Boundary & Pairwise Graph Reranking):
        Within each coarse macro-partition and across ambiguous decision boundaries,
        evaluates fine-grained bibliographical compatibility:
        - Identifier matches (ISBN, URN, OCLC, etc.)
        - Specialized domain keys
        - Hard vetoes: Volume conflict, numeric title conflict, container-part collision
        - Sequence alignment and aggregate constituent overlaps
        - Weighted fuzzy author-title-year score
        Connected components over verified edges confirm cluster boundaries and resolve ambiguities.

    Phase 3 (Micro Expression Realization & Manifestation Structuring):
        Within each verified Work cluster, expressions and manifestations are structured
        using RDA content type inference and hierarchical sub-clustering.

    Phase 4 (Transactional Batch Persistence):
        Delegates to WEMIClusterBuilder for atomic multi-tier persistence.
    """

    def __init__(
        self,
        corpus_repo: WEMIRepository,
        wemi_repo: Optional[WEMIRepository] = None,
        embedder: Optional[VectorEmbedder] = None,
        matcher: Optional[WorkMatcher] = None,
        config: Optional[WEMIClusterConfig] = None,
        corpus_origins: Optional[list[str]] = None,
        identity_manager: Optional[ClusterIdentityManager] = None,
        max_work_clusters: int = 50,
        min_work_clusters: int = 2,
        max_expression_clusters_per_work: int = 10,
        boundary_margin: float = 0.15,
        fine_threshold: float = 0.70,
        twilight_lower: Optional[float] = None,
        neural_boundary_threshold: Optional[float] = None,
        evaluator: Optional[Any] = None,
        rerank_scorer: Optional[Any] = None,
        edge_evaluator: Optional[BibliographicalEdgeEvaluator] = None,
        device: Optional[str] = None,
    ) -> None:
        self.corpus_repo = corpus_repo
        self.wemi_repo = wemi_repo if wemi_repo is not None else corpus_repo
        self.embedder = embedder
        self.matcher = matcher
        self.config = config or WEMIClusterConfig()
        self.corpus_origins = corpus_origins
        self.identity_manager = identity_manager or ClusterIdentityManager(self.corpus_repo)
        self.builder = WEMIClusterBuilder(self.wemi_repo, self.identity_manager)

        self.max_work_clusters = max_work_clusters
        self.min_work_clusters = min_work_clusters
        self.max_expression_clusters_per_work = max_expression_clusters_per_work
        self.boundary_margin = boundary_margin
        self.fine_threshold = fine_threshold

        hy_cfg = getattr(self.config, "hybrid", None)
        self.twilight_lower = (
            twilight_lower
            if twilight_lower is not None
            else (getattr(hy_cfg, "twilight_lower", 0.55) if hy_cfg else 0.55)
        )
        self.neural_boundary_threshold = (
            neural_boundary_threshold
            if neural_boundary_threshold is not None
            else (getattr(hy_cfg, "neural_boundary_threshold", 0.75) if hy_cfg else 0.75)
        )
        self.evaluator = evaluator or (getattr(matcher, "evaluator", None) if matcher else None)
        self.rerank_scorer = rerank_scorer or (
            self.evaluator.scorer if self.evaluator and hasattr(self.evaluator, "scorer") else None
        )
        self.edge_evaluator = edge_evaluator or BibliographicalEdgeEvaluator(config=self.config)

        # Hardware accelerator selection
        from reconciliator.utils.device import resolve_device

        self.device = resolve_device(device if device is not None else getattr(getattr(self.config, "bigclust", None), "device", getattr(self.config, "device", "auto")))

    def _embed_texts_cached(
        self, texts: Sequence[str], entity_type: str = "work"
    ) -> np.ndarray:
        """Embed a sequence of texts, utilizing WEMIRepository vector cache when available."""
        if not texts:
            dim = getattr(self.embedder, "dimension", 768) if self.embedder else 768
            return np.empty((0, dim), dtype=np.float32)

        if self.embedder is None:
            raise RuntimeError(
                "HybridBigClustGraphClusterer requires a configured VectorEmbedder to encode records."
            )
        if self.wemi_repo is not None and hasattr(self.wemi_repo, "embed_texts_cached"):
            res = self.wemi_repo.embed_texts_cached(self.embedder, texts, entity_type=entity_type)
            if isinstance(res, np.ndarray) and len(res) == len(texts):
                return res
        if hasattr(self.embedder, "encode_batch_dense"):
            return self.embedder.encode_batch_dense(list(texts))
        return self.embedder.embed_texts(list(texts))

    def _check_pair_affinity(
        self,
        r_i: MatchRecord,
        r_j: MatchRecord,
        ids_i: set[tuple[str, str]],
        ids_j: set[tuple[str, str]],
    ) -> bool:
        """Evaluate fine-grained bibliographical compatibility between two records.

        Combines graph pairwise scoring, hard veto detection, and neural boundary
        adjudication for ambiguous pairs in the twilight zone.
        """
        score, reason = self.edge_evaluator.evaluate(r_i, r_j, ids_i, ids_j)
        if score >= self.fine_threshold:
            return True
        if score < self.twilight_lower:
            return False
        # Do not allow pairs with hard conflicts/vetoes through
        if "conflict" in reason or "veto" in reason or score <= 0.0:
            return False

        # Twilight Zone: evaluate with neural reranker if available
        if self.rerank_scorer is not None:
            text_i = self._format_work_text(r_i)
            text_j = self._format_work_text(r_j)
            try:
                rerank_score = float(self.rerank_scorer.score(text_i, text_j))
                if rerank_score >= self.neural_boundary_threshold:
                    logger.debug(
                        "Twilight pair (%s, %s) rescued by neural reranker (base=%.3f, rerank=%.3f >= %.3f)",
                        r_i.record_id,
                        r_j.record_id,
                        score,
                        rerank_score,
                        self.neural_boundary_threshold,
                    )
                    return True
            except Exception as e:
                logger.debug("Failed neural boundary adjudication: %s", e)
        return False

    def _cluster_coarse_to_fine(
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
        """Internal coarse-to-fine clustering pipeline across unassigned records."""
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
                version_designation=rec.volume_designation,
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
        k_work_min = min(self.min_work_clusters, k_work_max)

        work_engine = BigClustMetal(
            k_max=k_work_max,
            k_min=k_work_min,
            device=self.device,
        )

        work_result = work_engine.fit(work_tensor, target_k=target_work_k)
        coarse_assignments = work_result.assignments.cpu().numpy()
        centroids = work_result.optimal_centroids

        # Boundary candidate detection
        dists = torch.cdist(work_tensor, centroids)
        num_centroids = centroids.shape[0]

        boundary_candidates: list[tuple[int, int, int]] = []
        if num_centroids > 1:
            sorted_dists, sorted_indices = torch.sort(dists, dim=1)
            for idx in range(len(records)):
                d1 = float(sorted_dists[idx, 0])
                d2 = float(sorted_dists[idx, 1])
                margin = (d2 - d1) / (d1 + 1e-6)
                if margin < self.boundary_margin:
                    c1 = int(sorted_indices[idx, 0])
                    c2 = int(sorted_indices[idx, 1])
                    boundary_candidates.append((idx, c1, c2))

        # Fine-grained boundary adjudication and graph resolution
        record_ids_map = {idx: extract_identifiers_from_target(r) for idx, r in enumerate(records)}
        coarse_groups: dict[int, list[int]] = defaultdict(list)
        for idx, c in enumerate(coarse_assignments):
            coarse_groups[int(c)].append(idx)

        edges: list[tuple[int, int]] = []

        # Intra-partition verification
        for _c_id, member_indices in coarse_groups.items():
            n_members = len(member_indices)
            if n_members <= 1:
                continue

            if n_members <= 50:
                for i_pos in range(n_members):
                    idx_i = member_indices[i_pos]
                    r_i = records[idx_i]
                    ids_i = record_ids_map[idx_i]
                    for j_pos in range(i_pos + 1, n_members):
                        idx_j = member_indices[j_pos]
                        r_j = records[idx_j]
                        ids_j = record_ids_map[idx_j]
                        if self._check_pair_affinity(r_i, r_j, ids_i, ids_j):
                            edges.append((idx_i, idx_j))
            else:
                sub_tensor = work_tensor[member_indices]
                sub_norm = torch.nn.functional.normalize(sub_tensor, dim=1)
                sim_matrix = torch.mm(sub_norm, sub_norm.t()).cpu().numpy()
                for i_pos in range(n_members):
                    idx_i = member_indices[i_pos]
                    r_i = records[idx_i]
                    ids_i = record_ids_map[idx_i]
                    for j_pos in range(i_pos + 1, n_members):
                        if sim_matrix[i_pos, j_pos] >= 0.60:
                            idx_j = member_indices[j_pos]
                            r_j = records[idx_j]
                            ids_j = record_ids_map[idx_j]
                            if self._check_pair_affinity(r_i, r_j, ids_i, ids_j):
                                edges.append((idx_i, idx_j))

        # Cross-partition boundary verification
        max_boundary_checks = getattr(
            getattr(self.config, "hybrid", None), "max_candidates_per_boundary", 20
        )
        for b_idx, _c1, c2 in boundary_candidates:
            r_b = records[b_idx]
            ids_b = record_ids_map[b_idx]
            candidates_c2 = coarse_groups.get(c2, [])
            checked = 0
            for idx_other in candidates_c2:
                if checked >= max_boundary_checks:
                    break
                r_other = records[idx_other]
                ids_other = record_ids_map[idx_other]
                if self._check_pair_affinity(r_b, r_other, ids_b, ids_other):
                    edges.append((b_idx, idx_other))
                checked += 1

        all_nodes = list(range(len(records)))
        fine_components = cluster_edges(all_nodes, edges)

        work_clusters: list[WorkCluster] = []
        expression_clusters: list[ExpressionCluster] = []
        manifestation_clusters: list[ManifestationCluster] = []
        corpus_linkages: list[CorpusLinkage] = []
        centroids_map: dict[str, np.ndarray] = {}

        for _root_id, member_indices in fine_components.items():
            member_recs = [records[i] for i in member_indices]
            sub_work_embs = work_embeddings[member_indices]
            canon_rec = select_canonical_work_record(member_recs)
            w_cluster = self.builder.build_work_cluster_from_records(
                member_recs, canonical_record=canon_rec
            )
            work_cluster_id = w_cluster.cluster_id
            work_clusters.append(w_cluster)
            centroids_map[work_cluster_id] = np.mean(sub_work_embs, axis=0)

            if len(member_recs) == 1:
                rec = member_recs[0]
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

            # Multi-record Work: Hierarchical Expression Sub-clustering
            expr_texts = [self._format_expression_text(r) for r in member_recs]
            expr_embeddings = self._embed_texts_cached(expr_texts, entity_type="expression")
            if isinstance(expr_embeddings, np.ndarray):
                expr_tensor = torch.from_numpy(expr_embeddings).float().to(self.device)
            else:
                expr_tensor = torch.tensor(expr_embeddings, dtype=torch.float32, device=self.device)

            k_expr_max = min(self.max_expression_clusters_per_work, len(member_recs))
            expr_engine = BigClustMetal(
                k_max=k_expr_max,
                k_min=1,
                device=self.device,
            )
            expr_result = expr_engine.fit(expr_tensor)
            expr_assignments = expr_result.assignments.cpu().numpy()

            for local_idx, rec in enumerate(member_recs):
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
                        confidence=1.0,
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
        """Execute hybrid coarse-to-fine clustering across given MatchRecords."""
        start_time = time.time()
        if not records:
            return MultiplexClusterResult(records_clustered=0, elapsed_seconds=0.0)

        logger.info(
            "Starting Hybrid BigClust+Graph clustering on %d records (device: %s, incremental: %s)",
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

            for idx, rec in enumerate(records):
                sim = float(max_sims_np[idx])
                if sim >= self.fine_threshold:
                    c_id = c_ids[int(best_indices_np[idx])]
                    # Check author compatibility if available
                    compatible = True
                    try:
                        existing_w = self.wemi_repo.get_work_cluster_full(c_id)
                        if existing_w and existing_w.canonical_author and rec.author_key:
                            if fuzz.ratio(existing_w.canonical_author.lower(), rec.author_key.lower()) < 60:
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

            # Update centroids for existing clusters that received new records
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
            ) = self._cluster_coarse_to_fine(
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
                work_clusters=work_clusters,
                expression_clusters=expression_clusters,
                manifestation_clusters=manifestation_clusters,
                corpus_linkages=corpus_linkages,
            )

        if centroids_map and self.wemi_repo is not None and hasattr(self.wemi_repo, "store_cluster_centroids"):
            try:
                self.wemi_repo.store_cluster_centroids(centroids_map, model_name=model_name)
            except Exception as e:
                logger.debug("Failed to store cluster centroids: %s", e)

        elapsed = time.time() - start_time
        total_work_count = len(work_clusters) + len(assigned_cluster_members)

        logger.info(
            "Hybrid clustering complete: %d works, %d expressions, %d manifestations across %d records in %.3fs",
            total_work_count,
            len(expression_clusters),
            len(manifestation_clusters),
            len(records),
            elapsed,
        )

        return MultiplexClusterResult(
            work_clusters_count=total_work_count,
            expression_clusters_count=len(expression_clusters),
            manifestation_clusters_count=len(manifestation_clusters),
            records_clustered=len(records),
            elapsed_seconds=round(elapsed, 3),
        )

    def cluster_corpus(
        self,
        origins: Optional[list[str]] = None,
        incremental: bool = False,
    ) -> MultiplexClusterResult:
        """Execute full intra-corpus co-reference clustering pipeline across staged records."""
        actual_origins = origins or self.corpus_origins
        records = self.corpus_repo.get_all_match_records(origins=actual_origins)
        if not records:
            logger.info("No staged records found for origins: %s", actual_origins)
            return MultiplexClusterResult(records_clustered=0, elapsed_seconds=0.0)

        logger.info(
            "Loaded %d staged records for hybrid clustering (incremental: %s)",
            len(records),
            incremental,
        )
        return self.cluster_records(records, persist=True, incremental=incremental)
