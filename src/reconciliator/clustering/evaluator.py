"""Bibliographical pairwise edge evaluation and compatibility scoring.

Evaluates pairwise edge weights, hard vetoes (volume conflict, numeric title conflict,
aggregate overlap), and multi-tier evidence (standard identifiers, domain keys,
fuzzy metadata similarity) between bibliographic records.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

import numpy as np
from rapidfuzz import fuzz

from reconciliator._core.clustering import (
    py_evaluate_aggregate_overlap as _rust_evaluate_aggregate_overlap,
)
from reconciliator.clustering.common.evaluator import (
    CompositeEdgeEvaluator,
    FeatureScorer,
    ModulatorRule,
    ShortcutRule,
    VetoRule,
)
from reconciliator.config import WEMIClusterConfig
from reconciliator.models import MatchRecord
from reconciliator.normalizers.record import normalize_volume
from reconciliator.utils import clean_alphanumeric

if TYPE_CHECKING:
    from reconciliator.database import WEMIRepository
    from reconciliator.embedding.protocol import VectorEmbedder


@dataclass
class BibliographicalEvaluationContext:
    """Context holding extracted identifiers and optional embedding services."""

    ids1: dict[str, list[str]]
    ids2: dict[str, list[str]]
    embedder: Optional[VectorEmbedder] = None
    repo: Optional[WEMIRepository] = None


class BibliographicalIdentifierShortcut(
    ShortcutRule[MatchRecord, BibliographicalEvaluationContext]
):
    """Deterministic shortcut rule for exact standard identifiers and domain keys."""

    def __init__(self, config: WEMIClusterConfig) -> None:
        self.config = config

    def evaluate_shortcut(
        self,
        r1: MatchRecord,
        r2: MatchRecord,
        context: BibliographicalEvaluationContext,
    ) -> Optional[tuple[float, str]]:
        # 1. Exact standard identifier match
        for scheme in self.config.common_schemes:
            if scheme in context.ids1 and scheme in context.ids2:
                v1_set = {clean_alphanumeric(x) for x in context.ids1[scheme]}
                v2_set = {clean_alphanumeric(x) for x in context.ids2[scheme]}
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

        return None


class BibliographicalVetoRule(VetoRule[MatchRecord, BibliographicalEvaluationContext]):
    """Hard vetoes: volume designation conflict, numeric title conflict, aggregate overlap."""

    def __init__(self, config: WEMIClusterConfig) -> None:
        self.config = config

    def evaluate_veto(
        self,
        r1: MatchRecord,
        r2: MatchRecord,
        context: BibliographicalEvaluationContext,
    ) -> Optional[tuple[float, str]]:
        # Explicit volume designation compatibility
        if r1.volume_designation and r2.volume_designation:
            v1 = (
                normalize_volume(r1.volume_designation)
                or r1.volume_designation.strip().lower()
            )
            v2 = (
                normalize_volume(r2.volume_designation)
                or r2.volume_designation.strip().lower()
            )
            if v1 != v2:
                return 0.0, "volume_conflict"

        # Numeric title conflict (excluding publication years)
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

        # Aggregate constituent overlap and container-part collision
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

        return None


class BibliographicalMetadataScorer(
    FeatureScorer[MatchRecord, BibliographicalEvaluationContext]
):
    """Computes composite metadata similarity (title, author, year, work type)."""

    def __init__(self, config: WEMIClusterConfig) -> None:
        self.config = config

    def score_feature(
        self,
        r1: MatchRecord,
        r2: MatchRecord,
        context: BibliographicalEvaluationContext,
    ) -> tuple[float, float]:
        pub_years = {str(y) for y in (r1.year, r2.year) if y and 1000 <= y <= 9999}
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
        elif {r1.work_type, r2.work_type}.issubset(
            {"text", "audio_nonmusic", "tactile"}
        ):
            type_sim = 1.0
        elif {r1.work_type, r2.work_type}.issubset({"text", "article", "chapter"}):
            type_sim = scoring.cross_format_type_similarity
        else:
            type_sim = scoring.default_type_similarity

        if r1.author_key and r2.author_key:
            w = scoring.with_author
            score = (
                w.title * title_sim
                + w.author * author_sim
                + w.year * year_sim
                + w.type * type_sim
            )
        else:
            w = scoring.without_author
            score = w.title * title_sim + w.year * year_sim + w.type * type_sim

        return round(score, 4), 1.0


class BibliographicalSemanticHomonymModulator(
    ModulatorRule[MatchRecord, BibliographicalEvaluationContext]
):
    """Homonym divergence guard: vetoes matches if high title similarity but divergent embeddings."""

    def __init__(self, veto_threshold: float = 0.40) -> None:
        self.veto_threshold = veto_threshold

    def modulate(
        self,
        score: float,
        r1: MatchRecord,
        r2: MatchRecord,
        context: BibliographicalEvaluationContext,
    ) -> tuple[float, str]:
        if context.embedder is None or context.repo is None:
            return score, "metadata_similarity"

        # Check if titles are very similar but author keys differ or are missing
        t1 = r1.uniform_title or r1.title or r1.title_stem
        t2 = r2.uniform_title or r2.title or r2.title_stem
        if not t1 or not t2:
            return score, "metadata_similarity"

        title_sim = fuzz.token_set_ratio(t1, t2) / 100.0
        if title_sim >= 0.90:
            author_shared = bool(
                r1.author_key
                and r2.author_key
                and r1.author_key.strip().lower() == r2.author_key.strip().lower()
            )
            if not author_shared:
                try:
                    text1 = f"Title: {t1} | Author: {r1.author_key or ''} | Type: {r1.work_type}"
                    text2 = f"Title: {t2} | Author: {r2.author_key or ''} | Type: {r2.work_type}"
                    vecs = context.repo.embed_texts_cached(
                        embedder=context.embedder,
                        texts=[text1, text2],
                        entity_type="work",
                    )
                    if len(vecs) == 2:
                        v1, v2 = vecs[0], vecs[1]
                        norm1 = float(np.linalg.norm(v1))
                        norm2 = float(np.linalg.norm(v2))
                        if norm1 > 0 and norm2 > 0:
                            cos_sim = float(np.dot(v1, v2) / (norm1 * norm2))
                            if cos_sim < self.veto_threshold:
                                return 0.0, "veto_semantic_homonym_divergence"
                except Exception:
                    pass

        return score, "metadata_similarity"


class BibliographicalEdgeEvaluator:
    """Evaluates pairwise edge weights, hard vetoes, and evidence between bibliographic records."""

    def __init__(
        self,
        config: Optional[WEMIClusterConfig] = None,
        embedder: Optional[VectorEmbedder] = None,
        repo: Optional[WEMIRepository] = None,
    ) -> None:
        self.config = config or WEMIClusterConfig()
        self.embedder = embedder
        self.repo = repo

        self.pipeline: CompositeEdgeEvaluator[
            MatchRecord, BibliographicalEvaluationContext
        ] = CompositeEdgeEvaluator(
            shortcuts=[BibliographicalIdentifierShortcut(self.config)],
            vetoes=[BibliographicalVetoRule(self.config)],
            scorers=[BibliographicalMetadataScorer(self.config)],
            modulators=[BibliographicalSemanticHomonymModulator()],
            default_method="metadata_similarity",
        )

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
        context = BibliographicalEvaluationContext(
            ids1=ids1,
            ids2=ids2,
            embedder=self.embedder,
            repo=self.repo,
        )
        return self.pipeline.evaluate(r1, r2, context)
