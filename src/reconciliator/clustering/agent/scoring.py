"""Scoring logic, biographical formatting, and semantic similarity for agent pairs."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Optional

import numpy as np
from rapidfuzz import fuzz, utils

from reconciliator.clustering.common.evaluator import (
    CompositeEdgeEvaluator,
    FeatureScorer,
    ModulatorRule,
    VetoRule,
)
from reconciliator.config import AgentClusterConfig
from reconciliator.database import WEMIRepository

if TYPE_CHECKING:
    from reconciliator.embedding.protocol import VectorEmbedder

logger = logging.getLogger(__name__)


def format_agent_text(
    agent: dict[str, Any],
    attrs: dict[str, list[str]],
    affils: set[str],
    concepts: set[str],
) -> str:
    """Format a rich textual biographical projection of an agent for semantic embedding."""
    parts = []
    name = agent.get("name") or agent.get("name_normalized")
    if name:
        parts.append(f"Name: {name}")
    b, d = agent.get("birth_year"), agent.get("death_year")
    dates = agent.get("dates") or ""
    if b is not None or d is not None:
        parts.append(f"Years: {b or ''}-{d or ''}")
    elif dates:
        parts.append(f"Dates: {dates}")
    if affils:
        parts.append(f"Affiliation: {', '.join(sorted(affils))}")
    if concepts:
        parts.append(f"Field: {', '.join(sorted(concepts))}")
    quals = attrs.get("qualifier", [])
    if quals:
        parts.append(f"Qualifier: {', '.join(sorted(quals))}")
    return " | ".join(parts) if parts else str(agent.get("id"))


def get_agent_semantic_similarity(
    repo: WEMIRepository,
    embedder: Optional[VectorEmbedder],
    text1: str,
    text2: str,
) -> Optional[float]:
    """Calculate cosine similarity between two agent biographical representations."""
    if embedder is None:
        return None
    try:
        vecs = repo.embed_texts_cached(
            embedder=embedder,
            texts=[text1, text2],
            entity_type="agent",
        )
        if len(vecs) == 2:
            v1, v2 = vecs[0], vecs[1]
            norm1 = float(np.linalg.norm(v1))
            norm2 = float(np.linalg.norm(v2))
            if norm1 > 0 and norm2 > 0:
                return float(np.dot(v1, v2) / (norm1 * norm2))
    except Exception as e:
        logger.debug("Failed to calculate agent semantic similarity: %s", e)
    return None


@dataclass
class AgentEvaluationContext:
    """Context holding agent attributes, coauthors, works, and scoring config."""

    attrs1: dict[str, list[str]]
    attrs2: dict[str, list[str]]
    affils1: set[str]
    affils2: set[str]
    concepts1: set[str]
    concepts2: set[str]
    works1: set[Any]
    works2: set[Any]
    coauthors1: set[int]
    coauthors2: set[int]
    config: AgentClusterConfig
    repo: WEMIRepository
    embedder: Optional[VectorEmbedder] = None
    hierarchy_service: Optional[Any] = None
    calculated_name_score: float = 0.0
    has_corroboration: bool = False


class AgentDateDiscrepancyVeto(VetoRule[dict[str, Any], AgentEvaluationContext]):
    """Vetoes agent pairs with fatal birth/death date contradictions."""

    def evaluate_veto(
        self,
        r1: dict[str, Any],
        r2: dict[str, Any],
        context: AgentEvaluationContext,
    ) -> Optional[tuple[float, str]]:
        b1, d1 = r1.get("birth_year"), r1.get("death_year")
        b2, d2 = r2.get("birth_year"), r2.get("death_year")
        config = context.config

        # Major Date Discrepancy Veto
        major_veto = config.major_date_discrepancy_veto_years
        if b1 is not None and b2 is not None and abs(b1 - b2) >= major_veto:
            return 0.0, "veto_birth_year_major_discrepancy"
        if d1 is not None and d2 is not None and abs(d1 - d2) >= major_veto:
            return 0.0, "veto_death_year_major_discrepancy"

        context.has_corroboration = bool(
            (context.works1 & context.works2)
            or (context.coauthors1 & context.coauthors2)
            or (context.affils1 & context.affils2)
            or (context.concepts1 & context.concepts2)
        )

        has_date_diff = (b1 is not None and b2 is not None and b1 != b2) or (
            d1 is not None and d2 is not None and d1 != d2
        )

        if has_date_diff:
            max_diff = max(
                abs(b1 - b2) if b1 is not None and b2 is not None else 0,
                abs(d1 - d2) if d1 is not None and d2 is not None else 0,
            )
            if max_diff <= config.date_tolerance_years:
                if (
                    config.require_corroboration_for_date_discrepancy
                    and not context.has_corroboration
                ):
                    return (
                        0.0,
                        "rejected_minor_date_discrepancy_without_corroboration",
                    )
            else:
                return 0.0, "rejected_date_discrepancy_exceeds_tolerance"

        return None


class AgentFeatureScorer(FeatureScorer[dict[str, Any], AgentEvaluationContext]):
    """Computes weighted multi-attribute similarity across name, dates, works, etc."""

    def score_feature(
        self,
        r1: dict[str, Any],
        r2: dict[str, Any],
        context: AgentEvaluationContext,
    ) -> tuple[float, float]:
        config = context.config
        weights = config.scoring_weights

        # 1. Name score
        names1 = [r1["name_normalized"]] + [
            v.lower() for v in context.attrs1.get("variant_name", [])
        ]
        names2 = [r2["name_normalized"]] + [
            v.lower() for v in context.attrs2.get("variant_name", [])
        ]

        best_name_sim = 0.0
        for n1 in names1:
            for n2 in names2:
                if config.token_matching_algorithm == "token_set_ratio":
                    sim = (
                        fuzz.token_set_ratio(n1, n2, processor=utils.default_process)
                        / 100.0
                    )
                else:
                    sim = (
                        fuzz.ratio(n1, n2, processor=utils.default_process) / 100.0
                    )
                if sim > best_name_sim:
                    best_name_sim = sim
        context.calculated_name_score = best_name_sim

        score_sum = weights.name_weight * best_name_sim
        weight_sum = weights.name_weight

        # 2. Dates score
        b1 = r1.get("birth_year")
        b2 = r2.get("birth_year")
        if b1 is not None and b2 is not None:
            if b1 == b2:
                date_score = 1.0
            elif abs(b1 - b2) <= config.date_tolerance_years:
                date_score = config.date_tolerance_score
            else:
                date_score = 0.0
        elif (b1 is None and b2 is not None) or (b1 is not None and b2 is None):
            date_score = config.date_missing_score
        else:
            date_score = config.date_missing_score
        score_sum += weights.dates_weight * date_score
        weight_sum += weights.dates_weight

        # 3. Work cluster score
        if context.works1 or context.works2:
            if context.works1 and context.works2:
                shared = len(context.works1 & context.works2)
                total = len(context.works1 | context.works2)
                work_score = shared / total if total else 0.0
            else:
                work_score = 0.0
            score_sum += weights.work_cluster_weight * work_score
            weight_sum += weights.work_cluster_weight

        # 4. Coauthor score
        ca1_clean = context.coauthors1 - {r2["id"]}
        ca2_clean = context.coauthors2 - {r1["id"]}
        if ca1_clean and ca2_clean:
            shared_ca = len(ca1_clean & ca2_clean)
            total_ca = len(ca1_clean | ca2_clean)
            coauthor_score = shared_ca / total_ca if total_ca else 0.0
            score_sum += weights.coauthor_weight * coauthor_score
            weight_sum += weights.coauthor_weight

        # 5. Affiliation score
        if context.affils1 and context.affils2:
            if context.affils1 & context.affils2:
                affiliation_score = 1.0
            else:
                affiliation_score = 0.0
                for af1 in context.affils1:
                    for af2 in context.affils2:
                        if (
                            fuzz.token_set_ratio(af1.lower(), af2.lower())
                            >= config.affiliation_fuzzy_threshold
                        ):
                            affiliation_score = config.affiliation_fuzzy_score
                            break
                    if affiliation_score > 0:
                        break
            score_sum += weights.affiliation_weight * affiliation_score
            weight_sum += weights.affiliation_weight

        # 6. Concept score
        if context.concepts1 and context.concepts2:
            if context.concepts1 & context.concepts2:
                concept_score = 1.0
            elif (
                getattr(config, "enable_hierarchical_concepts", True)
                and context.hierarchy_service is not None
            ):
                sim = context.hierarchy_service.concepts_similarity(
                    context.concepts1, context.concepts2
                )
                concept_score = sim * getattr(config, "concept_similarity_weight", 0.85)
            else:
                concept_score = 0.0
            score_sum += weights.concept_weight * concept_score
            weight_sum += weights.concept_weight

        # 7. Qualifier score
        q1 = set(context.attrs1.get("qualifier", []))
        q2 = set(context.attrs2.get("qualifier", []))
        if q1 and q2:
            qualifier_score = 1.0 if (q1 & q2) else 0.0
            score_sum += weights.qualifier_weight * qualifier_score
            weight_sum += weights.qualifier_weight

        composite = score_sum / weight_sum if weight_sum > 0 else 0.0
        return composite, 1.0


class AgentSemanticHomonymModulator(
    ModulatorRule[dict[str, Any], AgentEvaluationContext]
):
    """Semantic embedding disambiguation and homonym protection."""

    def modulate(
        self,
        score: float,
        r1: dict[str, Any],
        r2: dict[str, Any],
        context: AgentEvaluationContext,
    ) -> tuple[float, str]:
        config = context.config
        if (
            getattr(config, "enable_semantic_disambiguation", True)
            and context.embedder is not None
        ):
            text1 = format_agent_text(
                r1, context.attrs1, context.affils1, context.concepts1
            )
            text2 = format_agent_text(
                r2, context.attrs2, context.affils2, context.concepts2
            )
            sem_sim = get_agent_semantic_similarity(
                context.repo, context.embedder, text1, text2
            )
            if sem_sim is not None:
                veto_thresh = getattr(config, "semantic_homonym_veto_threshold", 0.40)
                if (
                    context.calculated_name_score >= 0.90
                    and sem_sim < veto_thresh
                    and not context.has_corroboration
                ):
                    return 0.0, "veto_semantic_homonym_divergence"

                boost_thresh = getattr(config, "semantic_boost_threshold", 0.85)
                if sem_sim >= boost_thresh:
                    modulated_score = min(1.0, score + 0.10 * sem_sim)
                    return modulated_score, "weighted_scoring_semantic_corroborated"

        return score, "weighted_scoring"


_agent_pipeline = CompositeEdgeEvaluator[dict[str, Any], AgentEvaluationContext](
    shortcuts=[],
    vetoes=[AgentDateDiscrepancyVeto()],
    scorers=[AgentFeatureScorer()],
    modulators=[AgentSemanticHomonymModulator()],
    default_method="weighted_scoring",
)


def score_agent_pair(
    a1: dict[str, Any],
    a2: dict[str, Any],
    attrs1: dict[str, list[str]],
    attrs2: dict[str, list[str]],
    affils1: set[str],
    affils2: set[str],
    concepts1: set[str],
    concepts2: set[str],
    works1: set[Any],
    works2: set[Any],
    coauthors1: set[int],
    coauthors2: set[int],
    config: AgentClusterConfig,
    repo: WEMIRepository,
    embedder: Optional[VectorEmbedder] = None,
    hierarchy_service: Optional[Any] = None,
) -> tuple[float, str]:
    """Evaluate match score between two agents using composite rule pipeline."""
    context = AgentEvaluationContext(
        attrs1=attrs1,
        attrs2=attrs2,
        affils1=affils1,
        affils2=affils2,
        concepts1=concepts1,
        concepts2=concepts2,
        works1=works1,
        works2=works2,
        coauthors1=coauthors1,
        coauthors2=coauthors2,
        config=config,
        repo=repo,
        embedder=embedder,
        hierarchy_service=hierarchy_service,
    )
    return _agent_pipeline.evaluate(a1, a2, context)
