"""Heuristic graph-based WEMI multiplex corpus clusterer.

Implements intra-corpus co-reference resolution using rule-based blocking keys,
LCS / string similarity scoring, Rust acceleration, and graph connected components.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections import defaultdict
from typing import Any, Optional, Sequence

from reconciliator._core.clustering import (
    py_lcs_length as _rust_lcs_length,
)
from reconciliator.authority.lifecycle import ClusterIdentityManager
from reconciliator.clustering.common import InvertedIndexBlocker
from reconciliator.clustering.evaluator import BibliographicalEdgeEvaluator
from reconciliator.clustering.protocol import BaseCorpusClusterer
from reconciliator.clustering.wemi_builder import (
    WEMIClusterBuilder,
    extract_expression_partition_keys,
    extract_manifestation_key,
    select_canonical_work_record,
)
from reconciliator.config import WEMIClusterConfig
from reconciliator.database import WEMIRepository
from reconciliator.models import (
    ClusterEdge,
    ClusterLifecycleEvent,
    ClusterRedirection,
    CorpusConstituentLinkage,
    CorpusLinkage,
    CorpusWorkRelationship,
    ExpressionCluster,
    ManifestationCluster,
    MatchRecord,
    MultiplexClusterResult,
    SammelbandBinding,
    WorkCluster,
)
from reconciliator.reconciliation import WorkMatcher
from reconciliator.utils import (
    UnionFind,
    clean_alphanumeric,
    cluster_edges,
    extract_identifiers_from_target,
)

logger = logging.getLogger(__name__)


def _lcs_length(seq1: list[str], seq2: list[str]) -> int:
    """Compute length of longest common subsequence between two sequences."""
    return _rust_lcs_length(seq1, seq2)


class MultiplexGraphClusterer(BaseCorpusClusterer):
    """Intra-corpus co-reference resolution and 3-tier WEMI multiplex graph clusterer."""

    def __init__(
        self,
        corpus_repo: WEMIRepository,
        wemi_repo: Optional[WEMIRepository] = None,
        matcher: Optional[WorkMatcher] = None,
        config: Optional[WEMIClusterConfig] = None,
        corpus_origins: Optional[list[str]] = None,
        identity_manager: Optional[ClusterIdentityManager] = None,
    ) -> None:
        self.corpus_repo = corpus_repo
        self.wemi_repo = wemi_repo if wemi_repo is not None else corpus_repo
        self.matcher = matcher
        self.config = config or WEMIClusterConfig()
        self.corpus_origins = corpus_origins
        self.identity_manager = identity_manager or ClusterIdentityManager(self.corpus_repo)
        self.builder = WEMIClusterBuilder(self.wemi_repo, self.identity_manager)
        embedder = getattr(self.matcher, "embedder", None)
        self.evaluator = BibliographicalEdgeEvaluator(
            config=self.config,
            embedder=embedder,
            repo=self.corpus_repo,
        )

    def generate_blocking_keys(self, target: MatchRecord) -> list[str]:
        """Generate deterministic blocking keys across all 35 key families and identifiers."""
        keys: list[str] = []

        def _add(family: str, val: Optional[str]) -> None:
            if val and val.strip():
                clean = val.strip().lower()
                keys.append(f"{family}::{clean}")

        # 1. Work-level & domain specific blocking keys
        for k_type, vals in target.keys.items():
            for val in vals:
                _add(k_type, val)

        # Aggregate constituent clusters blocking
        agg_cfg = getattr(self.config, "aggregates", None)
        if agg_cfg and agg_cfg.enabled and target.constituent_cluster_ids:
            bfamily = agg_cfg.blocking_family or "constituent_work"
            for cid in target.constituent_cluster_ids:
                _add(bfamily, cid)

        # 2. Material-specific blocking routing
        wt = target.work_type
        if wt == "serial" and target.get_first_key("series"):
            _add("serial", target.get_first_key("series"))
        elif wt == "music" and target.get_first_key("standard"):
            _add("music", target.get_first_key("standard"))
        elif wt in ("audio_nonmusic", "tactile") and target.work_blocking_key:
            _add("cross_format", target.work_blocking_key)

        if wt == "moving_image":
            _add(
                "moving_image",
                f"{target.author_key or ''}::{target.title_stem}::{target.year or ''}",
            )
        elif wt in ("drama", "choreography"):
            _add("drama", f"{target.author_key or ''}::{target.title_stem}")
        elif wt == "thesis":
            _add("thesis", f"{target.author_key or ''}::{target.title_stem}")
        elif wt == "cartography":
            _add(
                "cartographic", target.get_first_key("geodata_spatial") or target.work_blocking_key
            )
        elif wt == "still_image":
            _add(
                "still_image",
                f"{target.author_key or ''}::{target.title_stem}::{target.year or ''}",
            )
        elif wt == "preservation_surrogate":
            _add("preservation_surrogate", f"{target.title_stem}::{target.year or ''}")

        if wt == "tactile":
            _add("accessible_repro", target.work_blocking_key)
        if (
            target.get_first_key("translators_signature")
            and "parallel" in target.get_first_key("translators_signature").lower()
        ):
            _add("parallel_multilingual", target.work_blocking_key)

        # 3. Standard identifiers extracted from target
        ids = extract_identifiers_from_target(target)
        for scheme, vals in ids.items():
            for v in vals:
                clean_v = re.sub(r"[^0-9a-zA-Z]", "", v).lower()
                if clean_v:
                    keys.append(f"id::{scheme}::{clean_v}")

        return keys

    def compute_pairwise_score(
        self,
        r1: MatchRecord,
        r2: MatchRecord,
        ids1: dict[str, list[str]],
        ids2: dict[str, list[str]],
    ) -> tuple[float, str]:
        """Compute composite pairwise similarity score and evidence between two records."""
        return self.evaluator.evaluate(r1, r2, ids1, ids2)

    def extract_expression_key(self, target: MatchRecord) -> tuple[str, str, str]:
        """Extract (content_type, language, sub_key) partition signature for expression sub-clustering."""
        return extract_expression_partition_keys(target, music_cfg=self.config.music_score)

    def extract_manifestation_key(self, target: MatchRecord) -> str:
        """Extract carrier publication signature for manifestation duplicate clustering."""
        return extract_manifestation_key(target)

    def _merge_manifestation_groups_by_vector(
        self, groups: dict[str, list[MatchRecord]]
    ) -> dict[str, list[MatchRecord]]:
        """Optionally merge manifestation groups by vector similarity if embedder is configured."""
        embedder = getattr(self.matcher, "embedder", None)
        v_cfg = getattr(self.config, "vector_dedup", None)
        if embedder is None or v_cfg is None or not getattr(v_cfg, "enabled", False):
            return groups

        threshold = getattr(v_cfg, "threshold", 0.92)
        keys = list(groups)
        rep_by_source = {groups[k][0].record_id: k for k in keys}

        sem_edges: list[tuple[str, str]] = []
        for key in keys:
            rep = groups[key][0]
            text = rep.serialized_manifestation or rep.title
            if not text:
                continue
            try:
                vector = embedder.embed_texts([text])
                hits = self.corpus_repo.search_manifestation_vectors(
                    vector, top_k=min(len(keys), 20)
                )
            except Exception:
                continue
            for manifestation, score in hits:
                if score < threshold:
                    continue
                other = rep_by_source.get(manifestation.source_id)
                if other is not None and other != key:
                    sem_edges.append((key, other))

        sem_components = cluster_edges(keys, sem_edges)
        merged: dict[str, list[MatchRecord]] = {}
        for root, member_keys in sem_components.items():
            for k in member_keys:
                merged.setdefault(root, []).extend(groups[k])
        return merged

    def cluster_records(
        self,
        records: Sequence[MatchRecord],
        persist: bool = False,
        incremental: bool = False,
    ) -> MultiplexClusterResult:
        """Cluster explicit MatchRecords without re-querying the database."""
        start_time = time.time()
        if not records:
            return MultiplexClusterResult(elapsed_seconds=0.0)

        records_map: dict[str, MatchRecord] = {r.record_id: r for r in records}
        ids_cache: dict[str, dict[str, list[str]]] = {
            r.record_id: extract_identifiers_from_target(r) for r in records
        }

        # Step 1: Candidate Generation via common InvertedIndexBlocker
        blocker = InvertedIndexBlocker[MatchRecord, str](
            key_extractor=self.generate_blocking_keys,
            id_extractor=lambda r: r.record_id,
            min_bucket_size=2,
            max_bucket_size=getattr(
                self.config, "max_block_size", getattr(self.config, "max_cluster_size", 500)
            ),
        )
        candidate_pairs = blocker.generate_candidate_pairs(records)

        # Step 2: Edge Generation with Deterministic Identifier Shortcut & Connected Skipping
        edges: list[ClusterEdge] = []
        all_ids = list(records_map.keys())
        uf: UnionFind[str] = UnionFind(all_ids)
        linked_pairs: set[tuple[str, str]] = set()

        # 2a. Deterministic Pre-Linking Fast Path for exact standard identifiers
        for scheme in self.config.common_schemes:
            ident_index: dict[str, list[str]] = defaultdict(list)
            for rid in all_ids:
                for v in ids_cache[rid].get(scheme, []):
                    clean_v = clean_alphanumeric(v)
                    if clean_v:
                        ident_index[clean_v].append(rid)
            for _, aids in ident_index.items():
                if len(aids) > 1:
                    primary = aids[0]
                    for other in aids[1:]:
                        if uf.find(primary) != uf.find(other):
                            uf.union(primary, other)
                            pair = (primary, other) if primary < other else (other, primary)
                            if pair not in linked_pairs:
                                linked_pairs.add(pair)
                                edges.append(
                                    ClusterEdge(
                                        source_node=pair[0],
                                        target_node=pair[1],
                                        weight=1.0,
                                        evidence_type=f"identifier:{scheme}",
                                    )
                                )

        # 2b. Deterministic Pre-Linking Fast Path for specialized domain keys
        for k_type in set(self.config.domain_key_types):
            domain_index: dict[str, list[str]] = defaultdict(list)
            for r in records:
                for v in r.keys.get(k_type, []):
                    clean_v = v.strip().lower() if v else ""
                    if clean_v:
                        domain_index[clean_v].append(r.record_id)
            for _, aids in domain_index.items():
                if len(aids) > 1:
                    primary = aids[0]
                    for other in aids[1:]:
                        if uf.find(primary) != uf.find(other):
                            uf.union(primary, other)
                            pair = (primary, other) if primary < other else (other, primary)
                            if pair not in linked_pairs:
                                linked_pairs.add(pair)
                                edges.append(
                                    ClusterEdge(
                                        source_node=pair[0],
                                        target_node=pair[1],
                                        weight=1.0,
                                        evidence_type=f"domain_key:{k_type}",
                                    )
                                )

        # 2c. Candidate Scoring for remaining pairs (skipping already-unified components)
        for pair in candidate_pairs:
            id_a, id_b = pair
            if uf.find(id_a) == uf.find(id_b):
                continue

            r_a = records_map[id_a]
            r_b = records_map[id_b]
            score, method = self.compute_pairwise_score(
                r_a, r_b, ids_cache[id_a], ids_cache[id_b]
            )

            if score >= self.config.similarity_threshold:
                uf.union(id_a, id_b)
                edges.append(
                    ClusterEdge(
                        source_node=pair[0],
                        target_node=pair[1],
                        weight=score,
                        evidence_type=method,
                    )
                )

        # Step 3: Connected Components via native Rust IntUnionFind
        edge_pairs = [(e.source_node, e.target_node) for e in edges]
        work_components = cluster_edges(all_ids, edge_pairs)

        # Step 4 & 5: WEMI Multi-tier entity creation
        work_clusters: list[WorkCluster] = []
        expression_clusters: list[ExpressionCluster] = []
        manifestation_clusters: list[ManifestationCluster] = []
        corpus_linkages: list[CorpusLinkage] = []
        all_work_events: list[ClusterLifecycleEvent] = []
        all_work_redirections: list[ClusterRedirection] = []

        existing_work_map = self.corpus_repo.get_all_cluster_mappings()

        for _, member_ids in work_components.items():
            member_recs = [records_map[mid] for mid in member_ids]
            canon_rec = select_canonical_work_record(member_recs)

            # External Authority CURIE Hoisting
            hoisted_work_curie: Optional[str] = None
            hoisted_expr_curies: dict[tuple[str, str], str] = {}

            if self.matcher:
                for rec in member_recs:
                    m_res = self.matcher.match_target(rec)
                    if m_res.matched_work and m_res.confidence_score >= 0.70:
                        if not hoisted_work_curie and m_res.work_curie:
                            hoisted_work_curie = m_res.work_curie
                        if m_res.expression_curie and m_res.matched_expression:
                            exp_key = (
                                m_res.matched_expression.content_type,
                                (m_res.matched_expression.language or "und").lower(),
                            )
                            if exp_key not in hoisted_expr_curies:
                                hoisted_expr_curies[exp_key] = m_res.expression_curie

            (
                w_cluster,
                events,
                redirections,
            ) = self.builder.create_work_cluster(
                canonical_record=canon_rec,
                member_record_ids=member_ids,
                existing_cluster_map=existing_work_map,
                hoisted_work_curie=hoisted_work_curie,
            )
            work_clusters.append(w_cluster)
            all_work_events.extend(events)
            all_work_redirections.extend(redirections)

            # Expression sub-clustering
            exp_groups: dict[tuple[str, str, str], list[MatchRecord]] = defaultdict(list)
            for rec in member_recs:
                e_key = self.extract_expression_key(rec)
                exp_groups[e_key].append(rec)

            for (content_type, lang, sub_key), exp_recs in exp_groups.items():
                hoisted_exp_curie = hoisted_expr_curies.get((content_type, lang))
                e_cluster = self.builder.create_expression_cluster(
                    work_cluster_id=w_cluster.cluster_id,
                    content_type=content_type,
                    language=lang,
                    version_designation=sub_key or None,
                    hoisted_exp_curie=hoisted_exp_curie,
                )
                expression_clusters.append(e_cluster)

                # Manifestation clustering
                manif_groups: dict[str, list[MatchRecord]] = defaultdict(list)
                for rec in exp_recs:
                    m_key = self.extract_manifestation_key(rec)
                    manif_groups[m_key].append(rec)

                manif_groups = self._merge_manifestation_groups_by_vector(manif_groups)

                for _, m_recs in manif_groups.items():
                    m_canon = m_recs[0]
                    m_cluster = self.builder.create_manifestation_cluster(
                        canonical_record_id=m_canon.record_id,
                        publication_year=m_canon.year,
                        publisher=None,
                    )
                    manifestation_clusters.append(m_cluster)

                    for rec in m_recs:
                        linkage = CorpusLinkage(
                            record_id=rec.record_id,
                            manifestation_cluster_id=m_cluster.cluster_id,
                            work_cluster_id=w_cluster.cluster_id,
                            expression_cluster_id=e_cluster.cluster_id,
                            final_work_curie=w_cluster.canonical_work_curie,
                            final_expression_curie=e_cluster.canonical_expression_curie,
                        )
                        corpus_linkages.append(linkage)

        if persist:
            self.builder.persist_batch(
                work_clusters, expression_clusters, manifestation_clusters, corpus_linkages
            )
            self.identity_manager.record_lifecycle_batch(all_work_events, all_work_redirections)

        elapsed = time.time() - start_time
        return MultiplexClusterResult(
            work_clusters_count=len(work_clusters),
            expression_clusters_count=len(expression_clusters),
            manifestation_clusters_count=len(manifestation_clusters),
            records_clustered=len(records),
            elapsed_seconds=round(elapsed, 4),
        )

    def cluster_corpus(self, origins: Optional[list[str]] = None) -> MultiplexClusterResult:
        """Execute full intra-corpus co-reference clustering pipeline across staged records."""
        start_time = time.time()
        actual_origins = origins or self.corpus_origins or self.corpus_repo.non_authoritative_sources() or None
        records = self.corpus_repo.get_all_match_records(origins=actual_origins)
        if not records:
            return MultiplexClusterResult(elapsed_seconds=0.0)

        records_map: dict[str, MatchRecord] = {r.record_id: r for r in records}
        ids_cache: dict[str, dict[str, list[str]]] = {
            r.record_id: extract_identifiers_from_target(r) for r in records
        }

        # Micro-Clustering (Constituent Works First)
        constituent_work_clusters: dict[str, WorkCluster] = {}
        record_constituents: dict[str, list[dict[str, Any]]] = defaultdict(list)

        agg_cfg = getattr(self.config, "aggregates", None)
        if agg_cfg and agg_cfg.enabled:
            try:
                from sqlalchemy import and_, nulls_last, select

                from reconciliator.database.schema import (
                    agents_table,
                    contributions_table,
                    manifestations_table,
                    work_relationships_table,
                    works_table,
                )

                stmt_const = (
                    select(
                        manifestations_table.c.source_id.label("container_record_id"),
                        work_relationships_table.c.sequence_number,
                        works_table.c.id.label("const_work_id"),
                        works_table.c.source_id.label("const_work_source_id"),
                        works_table.c.title.label("const_title"),
                        works_table.c.work_type.label("const_work_type"),
                        agents_table.c.name.label("const_agent_name"),
                        contributions_table.c.field_tag.label("const_field_tag"),
                    )
                    .select_from(
                        manifestations_table.join(
                            work_relationships_table,
                            work_relationships_table.c.source_work_id
                            == manifestations_table.c.work_id,
                        )
                        .join(
                            works_table,
                            works_table.c.id == work_relationships_table.c.target_work_id,
                        )
                        .outerjoin(
                            agents_table,
                            agents_table.c.id == works_table.c.primary_agent_id,
                        )
                        .outerjoin(
                            contributions_table,
                            and_(
                                contributions_table.c.entity_type == "work",
                                contributions_table.c.entity_id == works_table.c.id,
                            ),
                        )
                    )
                    .where(
                        work_relationships_table.c.relationship_type.in_(
                            ("has_part", "has_constituent")
                        )
                    )
                )
                if actual_origins:
                    stmt_const = stmt_const.where(manifestations_table.c.record_origin.in_(actual_origins))
                stmt_const = stmt_const.order_by(
                    manifestations_table.c.source_id,
                    nulls_last(work_relationships_table.c.sequence_number),
                    works_table.c.id,
                )

                with self.corpus_repo.get_connection() as conn:
                    for row in conn.execute(stmt_const).mappings():
                        c_rec_id = row["container_record_id"]
                        record_constituents[c_rec_id].append(dict(row))
            except Exception:
                pass

            for r in records:
                if r.record_id in record_constituents:
                    for idx, c_info in enumerate(record_constituents[r.record_id]):
                        title = c_info.get("const_title") or ""
                        creator = c_info.get("const_agent_name") or ""
                        work_type = c_info.get("const_work_type") or "text"
                        cid = self.builder.mint_constituent_cluster_id(title=title, creator=creator)
                        c_info["cluster_id"] = cid

                        if cid not in constituent_work_clusters:
                            cw_cluster = self.builder.build_constituent_work_cluster(
                                title=title,
                                creator=creator,
                                work_type=work_type,
                            )
                            constituent_work_clusters[cid] = cw_cluster
                        r.constituent_cluster_ids.append(cid)

        # Step 1: Blocking
        blocks: dict[str, list[str]] = defaultdict(list)
        for r in records:
            for b_key in self.generate_blocking_keys(r):
                blocks[b_key].append(r.record_id)

        # Step 2: Edge Generation
        edges: list[ClusterEdge] = []
        visited_pairs: set[tuple[str, str]] = set()

        for _, member_ids in blocks.items():
            if len(member_ids) < 2 or len(member_ids) > self.config.max_block_size:
                continue
            n_members = len(member_ids)
            for i in range(n_members):
                for j in range(i + 1, n_members):
                    id_a, id_b = member_ids[i], member_ids[j]
                    pair = (id_a, id_b) if id_a < id_b else (id_b, id_a)
                    if pair in visited_pairs:
                        continue
                    visited_pairs.add(pair)

                    r_a = records_map[id_a]
                    r_b = records_map[id_b]
                    score, method = self.compute_pairwise_score(
                        r_a, r_b, ids_cache[id_a], ids_cache[id_b]
                    )

                    if score >= self.config.similarity_threshold:
                        edges.append(
                            ClusterEdge(
                                source_record_id=pair[0],
                                target_record_id=pair[1],
                                similarity_score=score,
                                match_method=method,
                            )
                        )

        # Step 3: Connected Components
        all_ids = list(records_map.keys())
        edge_pairs = [(e.source_record_id, e.target_record_id) for e in edges]
        work_components = cluster_edges(all_ids, edge_pairs)

        work_clusters: list[WorkCluster] = []
        expression_clusters: list[ExpressionCluster] = []
        manifestation_clusters: list[ManifestationCluster] = []
        corpus_linkages: list[CorpusLinkage] = []
        all_work_events: list[ClusterLifecycleEvent] = []
        all_work_redirections: list[ClusterRedirection] = []
        all_constituent_linkages: list[CorpusConstituentLinkage] = []
        all_corpus_work_relationships: list[CorpusWorkRelationship] = []
        sammelband_bindings: list[SammelbandBinding] = []

        existing_work_map: dict[str, str] = {}
        try:
            existing_work_map = self.corpus_repo.get_all_cluster_mappings()
        except Exception:
            pass

        for _, member_ids in work_components.items():
            member_recs = [records_map[mid] for mid in member_ids]
            canon_rec = select_canonical_work_record(member_recs)

            hoisted_work_curie: Optional[str] = None
            hoisted_expr_curies: dict[tuple[str, str], str] = {}

            if self.matcher:
                for rec in member_recs:
                    m_res = self.matcher.match_target(rec)
                    if m_res.matched_work and m_res.confidence_score >= 0.70:
                        if not hoisted_work_curie and m_res.work_curie:
                            hoisted_work_curie = m_res.work_curie
                        if m_res.expression_curie and m_res.matched_expression:
                            exp_key = (
                                m_res.matched_expression.content_type,
                                (m_res.matched_expression.language or "und").lower(),
                            )
                            if exp_key not in hoisted_expr_curies:
                                hoisted_expr_curies[exp_key] = m_res.expression_curie

            (
                w_cluster,
                events,
                redirections,
            ) = self.builder.create_work_cluster(
                canonical_record=canon_rec,
                member_record_ids=member_ids,
                existing_cluster_map=existing_work_map,
                hoisted_work_curie=hoisted_work_curie,
            )
            work_clusters.append(w_cluster)
            all_work_events.extend(events)
            all_work_redirections.extend(redirections)

            # Expression Sub-Clustering
            exp_groups: dict[tuple[str, str, str], list[MatchRecord]] = defaultdict(list)
            for rec in member_recs:
                e_key = self.extract_expression_key(rec)
                exp_groups[e_key].append(rec)

            for (content_type, lang, sub_key), exp_recs in exp_groups.items():
                hoisted_exp_curie = hoisted_expr_curies.get((content_type, lang))
                e_cluster = self.builder.create_expression_cluster(
                    work_cluster_id=w_cluster.cluster_id,
                    content_type=content_type,
                    language=lang,
                    version_designation=sub_key or None,
                    hoisted_exp_curie=hoisted_exp_curie,
                )
                expression_clusters.append(e_cluster)

                # Manifestation Duplicate Clustering
                manif_groups: dict[str, list[MatchRecord]] = defaultdict(list)
                for rec in exp_recs:
                    m_key = self.extract_manifestation_key(rec)
                    manif_groups[m_key].append(rec)

                manif_groups = self._merge_manifestation_groups_by_vector(manif_groups)

                for _, m_recs in manif_groups.items():
                    m_canon = m_recs[0]
                    m_cluster = self.builder.create_manifestation_cluster(
                        canonical_record_id=m_canon.record_id,
                        publication_year=m_canon.year,
                        publisher=None,
                    )
                    manifestation_clusters.append(m_cluster)

                    for rec in m_recs:
                        linkage = CorpusLinkage(
                            record_id=rec.record_id,
                            manifestation_cluster_id=m_cluster.cluster_id,
                            work_cluster_id=w_cluster.cluster_id,
                            expression_cluster_id=e_cluster.cluster_id,
                            final_work_curie=w_cluster.canonical_work_curie,
                            final_expression_curie=e_cluster.canonical_expression_curie,
                        )
                        corpus_linkages.append(linkage)

                        # Constituent linkages & work relationships
                        if agg_cfg and agg_cfg.enabled:
                            if rec.record_id in record_constituents:
                                for idx, c_info in enumerate(record_constituents[rec.record_id]):
                                    cid = c_info.get("cluster_id")
                                    if not cid:
                                        continue
                                    seq = c_info.get("sequence_number") or (idx + 1)
                                    c_link = CorpusConstituentLinkage(
                                        record_id=rec.record_id,
                                        constituent_ordinal=seq,
                                        constituent_work_id=c_info.get("const_work_source_id")
                                        or f"c:{seq}",
                                        constituent_work_cluster_id=cid,
                                        title=c_info.get("const_title") or "",
                                        creator=c_info.get("const_agent_name"),
                                        field_tag=c_info.get("const_field_tag"),
                                        final_work_source="reconciliator",
                                        final_work_source_id=cid,
                                        final_work_uri=f"reconciliator:{cid}",
                                        confidence=1.0,
                                    )
                                    all_constituent_linkages.append(c_link)

                                    c_rel = CorpusWorkRelationship(
                                        source_cluster_id=w_cluster.cluster_id,
                                        target_cluster_id=cid,
                                        relationship_type="has_part",
                                        sequence_number=seq,
                                        confidence=1.0,
                                    )
                                    all_corpus_work_relationships.append(c_rel)
                            elif "analytic" in rec.keys:
                                for idx, a_key in enumerate(rec.keys["analytic"]):
                                    try:
                                        a_title, a_creator = json.loads(a_key)
                                    except Exception:
                                        a_title, a_creator = a_key, None
                                    cid = self.builder.mint_constituent_cluster_id(title=a_title, creator=a_creator)
                                    seq = idx + 1
                                    c_link = CorpusConstituentLinkage(
                                        record_id=rec.record_id,
                                        constituent_ordinal=seq,
                                        constituent_work_id=f"c:{seq}",
                                        constituent_work_cluster_id=cid,
                                        title=a_title,
                                        creator=a_creator,
                                        field_tag="730",
                                        final_work_source="reconciliator",
                                        final_work_source_id=cid,
                                        final_work_uri=f"reconciliator:{cid}",
                                        confidence=1.0,
                                    )
                                    all_constituent_linkages.append(c_link)

                                    c_rel = CorpusWorkRelationship(
                                        source_cluster_id=w_cluster.cluster_id,
                                        target_cluster_id=cid,
                                        relationship_type="has_part",
                                        sequence_number=seq,
                                        confidence=1.0,
                                    )
                                    all_corpus_work_relationships.append(c_rel)

        # Step 6: Sammelband Item Binding Tracking
        sammelband_groups: dict[str, list[MatchRecord]] = defaultdict(list)
        for r in records:
            if r.get_first_key("sammelband"):
                sammelband_groups[r.get_first_key("sammelband")].append(r)

        for b_id, b_recs in sammelband_groups.items():
            for idx, r in enumerate(b_recs):
                sammelband_bindings.append(
                    SammelbandBinding(
                        binding_id=b_id,
                        record_id=r.record_id,
                        sequence_in_binding=idx + 1,
                        binding_notes=f"Item {idx + 1} of {len(b_recs)} in composite volume {b_id}",
                    )
                )

        # Step 7: Batch Persistence to PostgreSQL
        if constituent_work_clusters:
            self.corpus_repo.insert_work_clusters_batch(list(constituent_work_clusters.values()))
        self.builder.persist_batch(
            work_clusters, expression_clusters, manifestation_clusters, corpus_linkages
        )
        if all_constituent_linkages:
            self.corpus_repo.insert_corpus_constituent_linkages_batch(all_constituent_linkages)
        if all_corpus_work_relationships:
            self.corpus_repo.insert_corpus_work_relationships_batch(all_corpus_work_relationships)
        if sammelband_bindings:
            self.corpus_repo.insert_sammelband_bindings_batch(sammelband_bindings)
        self.identity_manager.record_lifecycle_batch(all_work_events, all_work_redirections)

        elapsed = time.time() - start_time
        return MultiplexClusterResult(
            work_clusters_count=len(work_clusters),
            expression_clusters_count=len(expression_clusters),
            manifestation_clusters_count=len(manifestation_clusters),
            records_clustered=len(records),
            elapsed_seconds=round(elapsed, 4),
        )
