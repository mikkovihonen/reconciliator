"""Cascading dual-route reranker dynamically delegating between heuristics and LLM Judge."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Optional

from reconciliator.rerank.base import RerankScorer

if TYPE_CHECKING:
    from reconciliator.config import CascadeRouterConfig

logger = logging.getLogger(__name__)


class CascadeScorer:
    """
    Tiered cascade reranker that executes fast heuristic scoring first,
    escalating only ambiguous, borderline, or close-contender cases to the
    KV-branching LLM Judge.
    """

    def __init__(
        self,
        heuristic_scorer: RerankScorer,
        judge_scorer: Optional[RerankScorer],
        config: CascadeRouterConfig,
    ) -> None:
        self.heuristic_scorer = heuristic_scorer
        self.judge_scorer = judge_scorer
        self.config = config
        self.heuristic_engine = config.heuristic_engine
        self.last_route: str = "heuristic"
        self.last_routes: list[str] = []

    def should_escalate_single(self, heuristic_score: float) -> bool:
        """Determine if an individual heuristic score falls into the uncertainty band."""
        return (
            self.config.uncertainty_band_min <= heuristic_score <= self.config.uncertainty_band_max
        )

    def score(
        self,
        query: str,
        document: str,
        past_key_values: Optional[Any] = None,
        **kwargs: Any,
    ) -> float:
        """
        Score a single candidate record against query.
        Uses fast heuristic first; escalates to LLM judge if score is within uncertainty band.
        """
        heur_score = self.heuristic_scorer.score(query, document)

        if self.judge_scorer is not None and self.should_escalate_single(heur_score):
            logger.debug(
                "CascadeRouter: Score %.4f in uncertainty band [%.2f, %.2f], escalating to LLM judge",
                heur_score,
                self.config.uncertainty_band_min,
                self.config.uncertainty_band_max,
            )
            judge_kwargs: dict[str, Any] = {}
            if past_key_values is not None:
                judge_kwargs["past_key_values"] = past_key_values
            try:
                judge_score = self.judge_scorer.score(query, document, **judge_kwargs)
            except TypeError:
                judge_score = self.judge_scorer.score(query, document)
            self.last_route = "judge"
            return judge_score

        self.last_route = "heuristic"
        return heur_score

    def score_batch(
        self,
        query: str,
        documents: list[str],
        past_key_values: Optional[Any] = None,
        target_encoding: Optional[Any] = None,
        **kwargs: Any,
    ) -> list[float]:
        """
        Score a set of candidate documents against a target query with margin-aware routing.
        """
        if not documents:
            self.last_routes = []
            return []

        # 1. Fast heuristic scoring pass across all candidates
        heur_scores = self.heuristic_scorer.score_batch(query, documents)
        routes = ["heuristic"] * len(documents)
        final_scores = list(heur_scores)

        if self.judge_scorer is None or len(documents) == 0:
            self.last_routes = routes
            return final_scores

        # 2. Analyze score distribution and contender margins
        indexed_scores = sorted(enumerate(heur_scores), key=lambda x: x[1], reverse=True)
        top_idx, top_score = indexed_scores[0]

        escalate_indices: set[int] = set()

        # Condition A: Decisively low score (below low threshold) -> Accept heuristic rejection
        if top_score < self.config.low_confidence_threshold:
            logger.debug(
                "CascadeRouter: Top candidate score %.4f < %.2f; all rejected by heuristic",
                top_score,
                self.config.low_confidence_threshold,
            )
        # Condition B: High confidence check
        elif top_score >= self.config.high_confidence_threshold:
            if len(indexed_scores) >= 2:
                second_score = indexed_scores[1][1]
                margin = top_score - second_score
                if margin < self.config.ambiguity_margin:
                    # Top candidate is strong, but close competitor exists within margin
                    logger.debug(
                        "CascadeRouter: Narrow margin %.4f < %.2f between top candidates; escalating contenders",
                        margin,
                        self.config.ambiguity_margin,
                    )
                    escalate_indices.add(top_idx)
                    for idx, score in indexed_scores[1:]:
                        if (top_score - score) <= self.config.ambiguity_margin:
                            escalate_indices.add(idx)
                else:
                    logger.debug(
                        "CascadeRouter: Decisive winner (score=%.4f, margin=%.4f); bypassing LLM judge",
                        top_score,
                        margin,
                    )
            else:
                # Single candidate with high confidence
                logger.debug(
                    "CascadeRouter: Single candidate with high confidence %.4f; bypassing LLM judge",
                    top_score,
                )
        # Condition C: Top score falls in the uncertainty band [min, max]
        elif self.config.uncertainty_band_min <= top_score <= self.config.uncertainty_band_max:
            logger.debug(
                "CascadeRouter: Top score %.4f in uncertainty band [%.2f, %.2f]; escalating contenders",
                top_score,
                self.config.uncertainty_band_min,
                self.config.uncertainty_band_max,
            )
            escalate_indices.add(top_idx)
            for idx, score in indexed_scores[1:]:
                # Escalate candidates either within uncertainty band or close to top score
                if (
                    self.config.uncertainty_band_min <= score <= self.config.uncertainty_band_max
                    or (top_score - score) <= self.config.ambiguity_margin
                ):
                    escalate_indices.add(idx)

        # Apply maximum escalation cap
        if len(escalate_indices) > self.config.max_escalated_candidates:
            # Keep top-scored candidates up to the cap
            sorted_escalated = sorted(escalate_indices, key=lambda i: heur_scores[i], reverse=True)[
                : self.config.max_escalated_candidates
            ]
            escalate_indices = set(sorted_escalated)

        # 3. GPU forward pass only on escalated candidate subset
        if escalate_indices:
            sorted_indices = sorted(escalate_indices)
            docs_to_evaluate = [documents[i] for i in sorted_indices]
            logger.info(
                "CascadeRouter: Evaluating %d of %d candidates with KV-branching LLM Judge",
                len(docs_to_evaluate),
                len(documents),
            )
            judge_kwargs: dict[str, Any] = {}
            if past_key_values is not None:
                judge_kwargs["past_key_values"] = past_key_values
            if target_encoding is not None:
                judge_kwargs["target_encoding"] = target_encoding
            try:
                judge_results = self.judge_scorer.score_batch(
                    query, docs_to_evaluate, **judge_kwargs
                )
            except TypeError:
                judge_results = self.judge_scorer.score_batch(query, docs_to_evaluate)
            for orig_idx, judge_score in zip(sorted_indices, judge_results):
                final_scores[orig_idx] = judge_score
                routes[orig_idx] = "judge"

        self.last_routes = routes
        return final_scores
