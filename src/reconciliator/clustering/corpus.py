"""Intra-corpus co-reference resolution and 3-tier WEMI multiplex graph clusterer.

Discovers shared Works, Expressions, and Manifestations within staged MatchRecords
in PostgreSQL database using multi-key blocking, pairwise similarity scoring,
connected-component graph clustering, and external authority URI hoisting.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections import defaultdict
from typing import Optional

from rapidfuzz import fuzz

from reconciliator.config import ClusterConfig
from reconciliator.database import WEMIRepository
from reconciliator.reconciliation import WorkMatcher
from reconciliator.models import (
    ClusterEdge,
    CorpusLinkage,
    ExpressionCluster,
    ManifestationCluster,
    MatchRecord,
    MultiplexClusterResult,
    SammelbandBinding,
    WorkCluster,
)
from reconciliator.utils import (
    UnionFind,
    clean_alphanumeric,
    extract_identifiers_from_target,
)


class CorpusClusterer:
    """Intra-corpus co-reference resolution and 3-tier WEMI multiplex graph clusterer."""

    def __init__(
        self,
        corpus_repo: WEMIRepository,
        wemi_repo: Optional[WEMIRepository] = None,
        matcher: Optional[WorkMatcher] = None,
        config: Optional[ClusterConfig] = None,
        corpus_origins: Optional[list[str]] = None,
    ) -> None:
        self.corpus_repo = corpus_repo
        self.wemi_repo = wemi_repo if wemi_repo is not None else corpus_repo
        self.matcher = matcher
        self.config = config or ClusterConfig()
        self.corpus_origins = corpus_origins

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
        # 1. Exact standard identifier match
        common_schemes = self.config.common_schemes
        for scheme in common_schemes:
            if scheme in ids1 and scheme in ids2:
                v1_set = {clean_alphanumeric(x) for x in ids1[scheme]}
                v2_set = {clean_alphanumeric(x) for x in ids2[scheme]}
                v1_set.discard("")
                v2_set.discard("")
                if v1_set & v2_set:
                    return 1.0, f"identifier:{scheme}"

        # 2. Identical specialized domain key match
        domain_key_types = set(self.config.domain_key_types)
        for k_type in domain_key_types:
            if k_type in r1.keys and k_type in r2.keys:
                s1 = {v.strip().lower() for v in r1.keys[k_type] if v and v.strip()}
                s2 = {v.strip().lower() for v in r2.keys[k_type] if v and v.strip()}
                if s1 & s2:
                    return 1.0, f"domain_key:{k_type}"

        # 3. Composite metadata similarity
        # Title similarity (cross-checking uniform_title, title, title_stem)
        titles_r1 = [t for t in (r1.uniform_title, r1.title, r1.title_stem) if t]
        titles_r2 = [t for t in (r2.uniform_title, r2.title, r2.title_stem) if t]
        title_sim = 0.0
        for x1 in titles_r1:
            for x2 in titles_r2:
                sim = fuzz.token_set_ratio(x1, x2) / 100.0
                if sim > title_sim:
                    title_sim = sim

        scoring = self.config.scoring
        # Author similarity
        if r1.author_key and r2.author_key:
            author_sim = fuzz.ratio(r1.author_key, r2.author_key) / 100.0
        elif not r1.author_key and not r2.author_key:
            author_sim = 1.0
        else:
            author_sim = scoring.author_missing_similarity

        # Year similarity
        if r1.year and r2.year:
            year_diff = abs(r1.year - r2.year)
            year_sim = max(0.0, 1.0 - year_diff / scoring.year_max_difference)
        else:
            year_sim = scoring.year_missing_similarity

        # Work type compatibility
        if r1.work_type == r2.work_type:
            type_sim = 1.0
        elif {r1.work_type, r2.work_type}.issubset({"text", "audio_nonmusic", "tactile"}):
            # Cross-format: audiobook or braille realization of print work
            type_sim = 1.0
        elif {r1.work_type, r2.work_type}.issubset({"text", "article", "chapter"}):
            type_sim = scoring.cross_format_type_similarity
        else:
            type_sim = scoring.default_type_similarity

        if r1.author_key and r2.author_key:
            w = scoring.with_author
            score = w.title * title_sim + w.author * author_sim + w.year * year_sim + w.type * type_sim
        else:
            w = scoring.without_author
            score = w.title * title_sim + w.year * year_sim + w.type * type_sim

        return round(score, 4), "metadata_similarity"

    def extract_expression_key(self, target: MatchRecord) -> tuple[str, str, str]:
        """Extract (content_type, language, sub_key) partition signature for expression sub-clustering."""
        wt = target.work_type
        lang = (target.language or "und").strip().lower()

        # Content Type Determination
        if wt == "audio_nonmusic":
            content_type = "spoken_word"
        elif wt == "music":
            music_cfg = self.config.music_score
            score_terms = tuple(t.lower() for t in music_cfg.terms)
            title_text = (target.title_stem or "").lower()
            vol_text = (target.volume_designation or "").lower()
            is_score = any(t in title_text for t in score_terms) or any(
                t in vol_text for t in score_terms
            )
            standard_key = target.get_first_key("standard")
            if standard_key and any(
                x.lower() in standard_key.lower() for x in music_cfg.standard_key_terms
            ):
                is_score = True

            if is_score:
                content_type = "notated_music"
            else:
                content_type = "audio_music"
        elif wt == "moving_image":
            content_type = "moving_image"
        elif wt == "cartography":
            content_type = "cartographic_image"
        elif wt == "tactile":
            content_type = "tactile_text"
        elif wt == "software":
            content_type = "computer_program"
        elif wt == "ai_model":
            content_type = "model_weights"
        elif wt in ("board_game", "realia"):
            content_type = "three_dimensional"
        elif wt in ("dataset", "geodata"):
            content_type = "dataset"
        elif wt == "still_image":
            content_type = "still_image"
        elif wt in ("podcast", "oral_history"):
            content_type = "spoken_word"
        else:
            content_type = "text"

        # Sub-key for Expression Disambiguation
        if content_type == "text":
            sub_key = (target.get_first_key("translators_signature") or "").strip().lower()
        elif content_type == "spoken_word":
            sub_key = (
                (target.get_first_key("translators_signature") or target.author_key or "")
                .strip()
                .lower()
            )
        elif content_type == "audio_music":
            sub_key = f"{(target.author_key or '').strip().lower()}_{target.year or ''}"
        elif content_type == "notated_music":
            sub_key = (target.volume_designation or "score").strip().lower()
        elif content_type in ("computer_program", "model_weights", "dataset"):
            sub_key = (target.volume_designation or "v1").strip().lower()
        elif content_type == "three_dimensional_form":
            sub_key = (target.volume_designation or "").strip().lower()
        else:
            sub_key = (target.volume_designation or "").strip().lower()

        return content_type, lang, sub_key

    def extract_manifestation_key(self, target: MatchRecord) -> str:
        """Extract carrier publication signature for manifestation duplicate clustering."""
        sk = (target.get_first_key("standard") or "").strip().lower()
        year = str(target.year or 0)
        vol = (target.volume_designation or "").strip().lower()
        return f"{sk}::{year}::{vol}"

    def _merge_manifestation_groups_by_vector(
        self, groups: dict[str, list[MatchRecord]]
    ) -> dict[str, list[MatchRecord]]:
        """Union key groups whose representatives are near-duplicates by embedding.

        The manifestation key is `standard::year::volume`, which separates two
        catalogues' records of the same edition whenever they share no standard
        number. This consults the manifestation vector index - the reason those
        embeddings are kept - and unions groups above
        `cluster.thresholds.manifestation`.

        A no-op without an embedder, without an index, or with an empty one, so
        clustering still works before `build-index` has run.
        """
        if len(groups) < 2:
            return groups
        embedder = getattr(self.matcher, "embedder", None) if self.matcher else None
        indexer = getattr(self.corpus_repo, "manifestation_indexer", None)
        if embedder is None or indexer is None or indexer.total_vectors == 0:
            return groups

        threshold = self.config.thresholds.manifestation
        keys = list(groups)
        # source_id of each group's representative, to recognise a hit as one of
        # the groups being clustered rather than some unrelated manifestation.
        rep_by_source = {groups[k][0].record_id: k for k in keys}

        uf: UnionFind[str] = UnionFind(keys)
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
                    uf.union(key, other)

        merged: dict[str, list[MatchRecord]] = {}
        for key in keys:
            root = uf.find(key)
            merged.setdefault(root, []).extend(groups[key])
        return merged

    def cluster_corpus(self) -> MultiplexClusterResult:
        """Execute full intra-corpus co-reference clustering pipeline across staged records."""
        start_time = time.time()
        origins = self.corpus_origins or self.corpus_repo.non_authoritative_sources() or None
        records = self.corpus_repo.get_all_match_records(origins=origins)
        if not records:
            return MultiplexClusterResult(elapsed_seconds=0.0)

        records_map: dict[str, MatchRecord] = {r.record_id: r for r in records}
        ids_cache: dict[str, dict[str, list[str]]] = {
            r.record_id: extract_identifiers_from_target(r) for r in records
        }

        # Step 1: Multi-Key Blocking Pass
        blocks: dict[str, list[str]] = defaultdict(list)
        for r in records:
            bkeys = self.generate_blocking_keys(r)
            for bk in bkeys:
                blocks[bk].append(r.record_id)

        # Step 2: Work Graph Clustering & Pairwise Scoring
        uf: UnionFind[str] = UnionFind([r.record_id for r in records])
        edges: list[ClusterEdge] = []
        candidate_pairs: set[tuple[str, str]] = set()

        max_cluster_size = self.config.max_cluster_size or 500
        threshold = (
            self.config.thresholds.work
            if hasattr(self.config, "thresholds") and self.config.thresholds
            else self.config.similarity_threshold
        )

        for _, rec_ids in blocks.items():
            if len(rec_ids) <= 1:
                continue
            if len(rec_ids) > max_cluster_size:
                rec_ids = rec_ids[:max_cluster_size]
            for i in range(len(rec_ids)):
                for j in range(i + 1, len(rec_ids)):
                    id1, id2 = rec_ids[i], rec_ids[j]
                    if id1 > id2:
                        id1, id2 = id2, id1
                    candidate_pairs.add((id1, id2))

        for id1, id2 in candidate_pairs:
            r1 = records_map[id1]
            r2 = records_map[id2]
            score, evidence = self.compute_pairwise_score(r1, r2, ids_cache[id1], ids_cache[id2])
            if score >= threshold:
                uf.union(id1, id2)
                edges.append(
                    ClusterEdge(
                        source_node=id1,
                        target_node=id2,
                        weight=score,
                        evidence_type=evidence,
                    )
                )

        work_components = uf.get_components()

        # Step 3: Work Cluster Synthesis & External URI Hoisting
        work_clusters: list[WorkCluster] = []
        expression_clusters: list[ExpressionCluster] = []
        manifestation_clusters: list[ManifestationCluster] = []
        corpus_linkages: list[CorpusLinkage] = []
        sammelband_bindings: list[SammelbandBinding] = []

        for _, member_ids in work_components.items():
            member_recs = [records_map[mid] for mid in member_ids]

            # Choose canonical representative record
            member_recs.sort(
                key=lambda r: (
                    -(1 if r.uniform_title else 0),
                    -(len(r.title or "")),
                    r.record_id,
                )
            )
            canon_rec = member_recs[0]

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

            if not hoisted_work_curie:
                work_sig = (
                    f"{canon_rec.title_stem}::{canon_rec.author_key or ''}::{canon_rec.work_type}"
                )
                h_work = hashlib.sha256(work_sig.encode("utf-8")).hexdigest()[:16]
                canonical_work_curie = f"reconciliator:work_{h_work}"
                work_cluster_id = f"wc_{h_work}"
            else:
                canonical_work_curie = hoisted_work_curie
                h_work = hashlib.sha256(canonical_work_curie.encode("utf-8")).hexdigest()[:16]
                work_cluster_id = f"wc_{h_work}"

            w_cluster = WorkCluster(
                cluster_id=work_cluster_id,
                work_type=canon_rec.work_type,
                canonical_work_curie=canonical_work_curie,
                canonical_title=canon_rec.uniform_title or canon_rec.title,
                canonical_author=canon_rec.author_key,
            )
            work_clusters.append(w_cluster)

            # Step 4: Expression Sub-Clustering within Work Cluster
            exp_groups: dict[tuple[str, str, str], list[MatchRecord]] = defaultdict(list)
            for rec in member_recs:
                e_key = self.extract_expression_key(rec)
                exp_groups[e_key].append(rec)

            for (content_type, lang, sub_key), exp_recs in exp_groups.items():
                hoisted_exp_curie = hoisted_expr_curies.get((content_type, lang))
                if hoisted_exp_curie:
                    canonical_exp_curie = hoisted_exp_curie
                    h_exp = hashlib.sha256(canonical_exp_curie.encode("utf-8")).hexdigest()[:16]
                    exp_cluster_id = f"ec_{h_exp}"
                else:
                    exp_sig = f"{work_cluster_id}::{content_type}::{lang}::{sub_key}"
                    h_exp = hashlib.sha256(exp_sig.encode("utf-8")).hexdigest()[:16]
                    canonical_exp_curie = f"reconciliator:exp_{h_exp}"
                    exp_cluster_id = f"ec_{h_exp}"

                e_cluster = ExpressionCluster(
                    cluster_id=exp_cluster_id,
                    work_cluster_id=work_cluster_id,
                    canonical_expression_curie=canonical_exp_curie,
                    content_type=content_type,  # type: ignore[arg-type]
                    language=lang,
                    version_designation=sub_key or None,
                )
                expression_clusters.append(e_cluster)

                # Step 5: Manifestation Duplicate Clustering within Expression Cluster
                manif_groups: dict[str, list[MatchRecord]] = defaultdict(list)
                for rec in exp_recs:
                    m_key = self.extract_manifestation_key(rec)
                    manif_groups[m_key].append(rec)

                manif_groups = self._merge_manifestation_groups_by_vector(manif_groups)

                for _, m_recs in manif_groups.items():
                    m_canon = m_recs[0]
                    h_manif = hashlib.sha256(
                        f"{exp_cluster_id}::{m_canon.record_id}".encode("utf-8")
                    ).hexdigest()[:16]
                    manif_cluster_id = f"mc_{h_manif}"

                    m_cluster = ManifestationCluster(
                        cluster_id=manif_cluster_id,
                        canonical_record_id=m_canon.record_id,
                        publisher=None,
                        publication_year=m_canon.year,
                        edition_statement=m_canon.volume_designation,
                        member_record_count=len(m_recs),
                    )
                    manifestation_clusters.append(m_cluster)

                    for rec in m_recs:
                        linkage = CorpusLinkage(
                            record_id=rec.record_id,
                            manifestation_cluster_id=manif_cluster_id,
                            work_cluster_id=work_cluster_id,
                            expression_cluster_id=exp_cluster_id,
                            final_work_curie=canonical_work_curie,
                            final_expression_curie=canonical_exp_curie,
                        )
                        corpus_linkages.append(linkage)

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
        self.corpus_repo.insert_work_clusters_batch(work_clusters)
        self.corpus_repo.insert_expression_clusters_batch(expression_clusters)
        self.corpus_repo.insert_manifestation_clusters_batch(manifestation_clusters)
        self.corpus_repo.insert_corpus_linkages_batch(corpus_linkages)
        if sammelband_bindings:
            self.corpus_repo.insert_sammelband_bindings_batch(sammelband_bindings)

        elapsed = time.time() - start_time
        return MultiplexClusterResult(
            work_clusters_count=len(work_clusters),
            expression_clusters_count=len(expression_clusters),
            manifestation_clusters_count=len(manifestation_clusters),
            records_clustered=len(records),
            elapsed_seconds=round(elapsed, 4),
        )

