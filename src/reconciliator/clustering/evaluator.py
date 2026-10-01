"""Bibliographical pairwise edge evaluation and compatibility scoring.

Evaluates pairwise edge weights, hard vetoes (volume conflict, numeric title conflict,
aggregate overlap), and multi-tier evidence (standard identifiers, domain keys,
fuzzy metadata similarity) between bibliographic records.
"""

from __future__ import annotations

import re
from typing import Optional

from rapidfuzz import fuzz

from reconciliator._core.clustering import (
    py_evaluate_aggregate_overlap as _rust_evaluate_aggregate_overlap,
)
from reconciliator.config import WEMIClusterConfig
from reconciliator.models import MatchRecord
from reconciliator.normalizers.record import normalize_volume
from reconciliator.utils import clean_alphanumeric


class BibliographicalEdgeEvaluator:
    """Evaluates pairwise edge weights, hard vetoes, and evidence between bibliographic records."""

    def __init__(self, config: Optional[WEMIClusterConfig] = None) -> None:
        self.config = config or WEMIClusterConfig()

    def evaluate(
        self,
        r1: MatchRecord,
        r2: MatchRecord,
        ids1: dict[str, list[str]],
        ids2: dict[str, list[str]],
    ) -> tuple[float, str]:
        """Compute composite pairwise similarity score and evidence between two records.

        Returns:
            Tuple of (score, evidence_or_veto_reason).
        """
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
