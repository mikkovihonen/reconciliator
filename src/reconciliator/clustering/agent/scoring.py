"""Scoring logic, biographical formatting, and semantic similarity for agent pairs."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Optional

import numpy as np
from rapidfuzz import fuzz, utils

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
    """Evaluate match score between two agents using configured weights and tolerances."""
    # 1. Check Date Discrepancies
    b1, d1 = a1.get("birth_year"), a1.get("death_year")
    b2, d2 = a2.get("birth_year"), a2.get("death_year")

    # Major Date Discrepancy Veto
    major_veto = config.major_date_discrepancy_veto_years
    if b1 is not None and b2 is not None and abs(b1 - b2) >= major_veto:
        return 0.0, "veto_birth_year_major_discrepancy"
    if d1 is not None and d2 is not None and abs(d1 - d2) >= major_veto:
        return 0.0, "veto_death_year_major_discrepancy"

    # Minor Date Discrepancy Corroboration Requirement
    has_corroboration = bool(
        (works1 & works2)
        or (coauthors1 & coauthors2)
        or (affils1 & affils2)
        or (concepts1 & concepts2)
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
            if config.require_corroboration_for_date_discrepancy and not has_corroboration:
                return 0.0, "rejected_minor_date_discrepancy_without_corroboration"
        else:
            return 0.0, "rejected_date_discrepancy_exceeds_tolerance"

    # 2. Feature Scoring
    weights = config.scoring_weights

    # Name score (comparing primary and variants)
    names1 = [a1["name_normalized"]] + [v.lower() for v in attrs1.get("variant_name", [])]
    names2 = [a2["name_normalized"]] + [v.lower() for v in attrs2.get("variant_name", [])]

    best_name_sim = 0.0
    for n1 in names1:
        for n2 in names2:
            if config.token_matching_algorithm == "token_set_ratio":
                sim = fuzz.token_set_ratio(n1, n2, processor=utils.default_process) / 100.0
            else:
                sim = fuzz.ratio(n1, n2, processor=utils.default_process) / 100.0
            if sim > best_name_sim:
                best_name_sim = sim
    name_score = best_name_sim

    score_sum = weights.name_weight * name_score
    weight_sum = weights.name_weight

    # Dates score
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

    # Work cluster score
    if works1 or works2:
        if works1 and works2:
            shared = len(works1 & works2)
            total = len(works1 | works2)
            work_score = shared / total if total else 0.0
        else:
            work_score = 0.0
        score_sum += weights.work_cluster_weight * work_score
        weight_sum += weights.work_cluster_weight

    # Coauthor score (shared third-party coauthors)
    ca1_clean = coauthors1 - {a2["id"]}
    ca2_clean = coauthors2 - {a1["id"]}
    if ca1_clean and ca2_clean:
        shared_ca = len(ca1_clean & ca2_clean)
        total_ca = len(ca1_clean | ca2_clean)
        coauthor_score = shared_ca / total_ca if total_ca else 0.0
        score_sum += weights.coauthor_weight * coauthor_score
        weight_sum += weights.coauthor_weight

    # Affiliation score
    if affils1 and affils2:
        if affils1 & affils2:
            affiliation_score = 1.0
        else:
            affiliation_score = 0.0
            for af1 in affils1:
                for af2 in affils2:
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

    # Concept (occupation/field_of_work) score
    if concepts1 and concepts2:
        if concepts1 & concepts2:
            concept_score = 1.0
        elif (
            getattr(config, "enable_hierarchical_concepts", True)
            and hierarchy_service is not None
        ):
            sim = hierarchy_service.concepts_similarity(concepts1, concepts2)
            concept_score = sim * getattr(config, "concept_similarity_weight", 0.85)
        else:
            concept_score = 0.0
        score_sum += weights.concept_weight * concept_score
        weight_sum += weights.concept_weight

    # Qualifier score
    q1 = set(attrs1.get("qualifier", []))
    q2 = set(attrs2.get("qualifier", []))
    if q1 and q2:
        qualifier_score = 1.0 if (q1 & q2) else 0.0
        score_sum += weights.qualifier_weight * qualifier_score
        weight_sum += weights.qualifier_weight

    composite_score = score_sum / weight_sum if weight_sum > 0 else 0.0

    # Semantic embedding disambiguation & homonym protection
    if getattr(config, "enable_semantic_disambiguation", True) and embedder is not None:
        text1 = format_agent_text(a1, attrs1, affils1, concepts1)
        text2 = format_agent_text(a2, attrs2, affils2, concepts2)
        sem_sim = get_agent_semantic_similarity(repo, embedder, text1, text2)
        if sem_sim is not None:
            veto_thresh = getattr(config, "semantic_homonym_veto_threshold", 0.40)
            if (
                name_score >= 0.90
                and sem_sim < veto_thresh
                and not has_corroboration
            ):
                return 0.0, "veto_semantic_homonym_divergence"

            boost_thresh = getattr(config, "semantic_boost_threshold", 0.85)
            if sem_sim >= boost_thresh:
                composite_score = min(1.0, composite_score + 0.10 * sem_sim)
                return composite_score, "weighted_scoring_semantic_corroborated"

    return composite_score, "weighted_scoring"
