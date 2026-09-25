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
from typing import Any, Optional

from rapidfuzz import fuzz

from reconciliator._core.clustering import (
    py_evaluate_aggregate_overlap as _rust_evaluate_aggregate_overlap,
    py_lcs_length as _rust_lcs_length,
)
from reconciliator.authority.lifecycle import ClusterIdentityManager
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
from reconciliator.normalizers.blocking import compute_author_key, compute_title_stem
from reconciliator.reconciliation import WorkMatcher
from reconciliator.utils import (
    clean_alphanumeric,
    cluster_edges,
    extract_identifiers_from_target,
)


def _lcs_length(seq1: list[str], seq2: list[str]) -> int:
    """Compute length of longest common subsequence between two sequences."""
    return _rust_lcs_length(seq1, seq2)


class CorpusClusterer:
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

        # Check explicit volume designation compatibility (hard veto)
        if r1.volume_designation and r2.volume_designation:
            from reconciliator.normalizers.record import normalize_volume

            v1 = normalize_volume(r1.volume_designation) or r1.volume_designation.strip().lower()
            v2 = normalize_volume(r2.volume_designation) or r2.volume_designation.strip().lower()
            if v1 != v2:
                return 0.0, "volume_conflict"

        # Check numeric title conflict (hard veto)
        # Extract numeric tokens from titles, excluding publication years
        pub_years = {str(y) for y in (r1.year, r2.year) if y and 1000 <= y <= 9999}
        r1_title_nums = (
            set(re.findall(r"\b\d+\b", r1.title or ""))
            | set(re.findall(r"\b\d+\b", r1.title_stem or ""))
            | set(re.findall(r"\b\d+\b", r1.uniform_title or ""))
        ) - pub_years
        r2_title_nums = (
            set(re.findall(r"\b\d+\b", r2.title or ""))
            | set(re.findall(r"\b\d+\b", r2.title_stem or ""))
            | set(re.findall(r"\b\d+\b", r2.uniform_title or ""))
        ) - pub_years

        diff1 = r1_title_nums - r2_title_nums
        diff2 = r2_title_nums - r1_title_nums
        if diff1 and diff2:
            return 0.0, "numeric_title_conflict"

        # Check aggregate constituent overlap and container-part collision
        agg_cfg = getattr(self.config, "aggregates", None)
        if agg_cfg and agg_cfg.enabled:
            c1 = r1.constituent_cluster_ids or r1.keys.get("analytic", [])
            c2 = r2.constituent_cluster_ids or r2.keys.get("analytic", [])
            res = _rust_evaluate_aggregate_overlap(
                c1=c1,
                c2=c2,
                is_component1=bool(r1.is_component),
                is_component2=bool(r2.is_component),
                min_constituents=agg_cfg.min_constituents_for_overlap,
                jaccard_veto_threshold=agg_cfg.jaccard_veto_threshold,
                jaccard_merge_threshold=agg_cfg.jaccard_merge_threshold,
                sequence_alignment=bool(agg_cfg.sequence_alignment),
                lcs_threshold=agg_cfg.lcs_threshold,
                merge_bonus_score=agg_cfg.merge_bonus_score,
            )
            if res is not None:
                return res

        # 3. Composite metadata similarity
        # Title similarity (cross-checking uniform_title, title, title_stem)
        titles_r1 = [t for t in (r1.uniform_title, r1.title, r1.title_stem) if t]
        titles_r2 = [t for t in (r2.uniform_title, r2.title, r2.title_stem) if t]
        title_sim = 0.0
        for x1 in titles_r1:
            n1 = set(re.findall(r"\b\d+\b", x1)) - pub_years
            for x2 in titles_r2:
                n2 = set(re.findall(r"\b\d+\b", x2)) - pub_years
                if (n1 - n2) and (n2 - n1):
                    continue
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
            score = (
                w.title * title_sim + w.author * author_sim + w.year * year_sim + w.type * type_sim
            )
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
            nums = re.findall(r"\b\d+\b", target.title_stem or target.title or "")
            num_suffix = f"_{'_'.join(nums)}" if nums else ""
            sub_key = f"{(target.author_key or '').strip().lower()}_{target.year or ''}{num_suffix}"
        elif content_type == "notated_music":
            if target.volume_designation:
                sub_key = target.volume_designation.strip().lower()
            else:
                nums = re.findall(r"\b\d+\b", target.title_stem or target.title or "")
                sub_key = f"score_{'_'.join(nums)}" if nums else "score"
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
        if sk:
            return f"{sk}::{year}::{vol}"
        t_stem = (target.title_stem or target.title or "").strip().lower()
        return f"raw::{target.record_id}::{t_stem}::{year}::{vol}"

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

        # Step 0: Micro-Clustering (Constituent Works First)
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
                if origins:
                    stmt_const = stmt_const.where(manifestations_table.c.record_origin.in_(origins))
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
                        auth_key = compute_author_key(creator)
                        stem = compute_title_stem(title)
                        h = hashlib.sha256(f"{auth_key}::{stem}".encode("utf-8")).hexdigest()[:16]
                        cid = f"va_const_work_{h}"
                        c_info["cluster_id"] = cid
                        if cid not in constituent_work_clusters:
                            constituent_work_clusters[cid] = WorkCluster(
                                cluster_id=cid,
                                work_type=c_info.get("const_work_type") or "music",
                                canonical_work_curie=f"reconciliator:{cid}",
                                canonical_title=title,
                                canonical_author=auth_key or None,
                                authority_status="provisional",
                                canonical_access_point=f"{creator} - {title}".strip(" -") or title,
                            )
                        if cid not in r.constituent_cluster_ids:
                            r.constituent_cluster_ids.append(cid)
                elif "analytic" in r.keys:
                    for a_key in r.keys["analytic"]:
                        try:
                            a_title, a_creator = json.loads(a_key)
                        except Exception:
                            a_title, a_creator = a_key, None
                        auth_key = compute_author_key(a_creator)
                        stem = compute_title_stem(a_title)
                        h = hashlib.sha256(f"{auth_key}::{stem}".encode("utf-8")).hexdigest()[:16]
                        cid = f"va_const_work_{h}"
                        if cid not in constituent_work_clusters:
                            constituent_work_clusters[cid] = WorkCluster(
                                cluster_id=cid,
                                work_type="music",
                                canonical_work_curie=f"reconciliator:{cid}",
                                canonical_title=a_title,
                                canonical_author=auth_key or None,
                                authority_status="provisional",
                                canonical_access_point=(
                                    f"{a_creator or ''} - {a_title}".strip(" -") or a_title
                                ),
                            )
                        if cid not in r.constituent_cluster_ids:
                            r.constituent_cluster_ids.append(cid)

        # Step 1: Multi-Key Blocking Pass
        blocks: dict[str, list[str]] = defaultdict(list)
        for r in records:
            bkeys = self.generate_blocking_keys(r)
            for bk in bkeys:
                blocks[bk].append(r.record_id)

        # Step 2: Work Graph Clustering & Pairwise Scoring
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
                edges.append(
                    ClusterEdge(
                        source_node=id1,
                        target_node=id2,
                        weight=score,
                        evidence_type=evidence,
                    )
                )

        edge_tuples = [(e.source_node, e.target_node) for e in edges]
        work_components = cluster_edges([r.record_id for r in records], edge_tuples)

        # Step 3: Work Cluster Synthesis & External URI Hoisting
        work_clusters: list[WorkCluster] = []
        expression_clusters: list[ExpressionCluster] = []
        manifestation_clusters: list[ManifestationCluster] = []
        corpus_linkages: list[CorpusLinkage] = []
        all_constituent_linkages: list[CorpusConstituentLinkage] = []
        all_corpus_work_relationships: list[CorpusWorkRelationship] = []
        sammelband_bindings: list[SammelbandBinding] = []
        all_work_events: list[ClusterLifecycleEvent] = []
        all_work_redirections: list[ClusterRedirection] = []

        existing_work_map: dict[str, str] = {}
        try:
            with self.corpus_repo.get_corpus_connection() as conn:
                from sqlalchemy import select

                from reconciliator.database.schema import corpus_linkages_table

                stmt_prev = select(
                    corpus_linkages_table.c.record_id,
                    corpus_linkages_table.c.work_cluster_id,
                )
                for row in conn.execute(stmt_prev).mappings():
                    existing_work_map[row["record_id"]] = row["work_cluster_id"]
        except Exception:
            pass

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

            (
                work_cluster_id,
                auth_status,
                access_point,
                events,
                redirections,
            ) = self.identity_manager.resolve_work_cluster_identity(
                canonical_title=canon_rec.uniform_title or canon_rec.title,
                canonical_author=canon_rec.author_key,
                work_type=canon_rec.work_type,
                member_record_ids=member_ids,
                existing_cluster_map=existing_work_map,
            )
            all_work_events.extend(events)
            all_work_redirections.extend(redirections)

            canonical_work_curie = hoisted_work_curie or self.identity_manager.format_canonical_uri(
                "work", work_cluster_id
            )

            w_cluster = WorkCluster(
                cluster_id=work_cluster_id,
                work_type=canon_rec.work_type,
                canonical_work_curie=canonical_work_curie,
                canonical_title=canon_rec.uniform_title or canon_rec.title,
                canonical_author=canon_rec.author_key,
                authority_status=auth_status,
                canonical_access_point=access_point,
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
                                        source_cluster_id=work_cluster_id,
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
                                    auth_key = compute_author_key(a_creator)
                                    stem = compute_title_stem(a_title)
                                    h = hashlib.sha256(
                                        f"{auth_key}::{stem}".encode("utf-8")
                                    ).hexdigest()[:16]
                                    cid = f"va_const_work_{h}"
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
                                        source_cluster_id=work_cluster_id,
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
        self.corpus_repo.insert_work_clusters_batch(work_clusters)
        self.corpus_repo.insert_expression_clusters_batch(expression_clusters)
        self.corpus_repo.insert_manifestation_clusters_batch(manifestation_clusters)
        self.corpus_repo.insert_corpus_linkages_batch(corpus_linkages)
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
