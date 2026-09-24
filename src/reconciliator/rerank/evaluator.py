"""Candidate evaluation, metadata discrimination, and score fusion pipeline."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Optional

from reconciliator.models import BibliographicRecord, ReconciliationMatch
from reconciliator.rerank.base import RerankScorer
from reconciliator.rerank.factory import create_rerank_scorer

if TYPE_CHECKING:
    from reconciliator.config import RerankConfig


class CandidateEvaluator:
    """
    Evaluates retrieval candidates using domain metadata discriminators (volume, surname, year),
    dense short-circuiting, and pluggable reranker score fusion.
    """

    def __init__(
        self,
        config: RerankConfig,
        scorer: Optional[RerankScorer] = None,
        judge_backend: Optional[Any] = None,
    ) -> None:
        self.config = config
        self.scorer: RerankScorer = scorer or create_rerank_scorer(
            config, judge_backend=judge_backend
        )

    def check_volume_match(
        self,
        target_volume: Optional[str],
        candidate_volume: Optional[str],
        target_title: Optional[str] = None,
        candidate_title: Optional[str] = None,
        target_year: Optional[int] = None,
        candidate_year: Optional[int] = None,
    ) -> bool:
        """
        Check volume/part designation and numeric title token compatibility.
        Rejects conflicting designations (e.g. 'osa 1' vs 'osa 2') or conflicting numeric title tokens.

        Compares *normalised* designations. Raw string equality made this check
        reject every candidate whose volume was merely spelled differently -
        "osa 2" against "2. osa", or against "Vol. II" - which is the usual case
        across two catalogues. `normalize_volume` canonicalises Finnish, Swedish
        and English prefixes and Roman numerals to one form, and exists for
        exactly this comparison.
        """
        if not self.config.require_volume_match:
            return True

        if target_volume and candidate_volume:
            from reconciliator.normalizers.record import normalize_volume

            target_norm = normalize_volume(target_volume) or target_volume.strip().lower()
            candidate_norm = normalize_volume(candidate_volume) or candidate_volume.strip().lower()
            if target_norm != candidate_norm:
                return False

        if target_title and candidate_title:
            import re

            pub_years = {str(y) for y in (target_year, candidate_year) if y and 1000 <= y <= 9999}
            n1 = set(re.findall(r"\b\d+\b", target_title)) - pub_years
            n2 = set(re.findall(r"\b\d+\b", candidate_title)) - pub_years
            if (n1 - n2) and (n2 - n1):
                return False

        return True

    def check_surname_match(
        self,
        target_record: BibliographicRecord,
        candidate_record: BibliographicRecord,
        author_relationships: Optional[dict[str, set[str]]] = None,
    ) -> bool:
        """Verify that at least one author surname matches between records, or matches via pseudonym/identity relationship."""
        if not self.config.require_surname_match:
            return True

        target_surnames = {a.surname.lower() for a in target_record.agents if a.surname}
        candidate_surnames = {a.surname.lower() for a in candidate_record.agents if a.surname}

        # If either record has no author surnames, don't penalize
        if not target_surnames or not candidate_surnames:
            return True

        if target_surnames & candidate_surnames:
            return True

        if author_relationships:
            for ts in target_surnames:
                if author_relationships.get(ts, set()) & candidate_surnames:
                    return True
            for cs in candidate_surnames:
                if author_relationships.get(cs, set()) & target_surnames:
                    return True

        # Check agent.relationships directly if present on target or candidate agents
        from reconciliator.normalizers.blocking import compute_author_key

        for agent in list(target_record.agents) + list(candidate_record.agents):
            for rel in getattr(agent, "relationships", []) or []:
                rel_type = rel.get("relationship_type", "")
                if rel_type in ("pseudonym_of", "real_identity_of", "alternate_identity_of"):
                    t_name = rel.get("target_name")
                    if t_name:
                        norm_target = compute_author_key(t_name, None)
                        if norm_target:
                            agent_sur = (agent.surname or "").lower()
                            if (
                                agent_sur in target_surnames or not agent_sur
                            ) and norm_target in candidate_surnames:
                                return True
                            if (
                                agent_sur in candidate_surnames or not agent_sur
                            ) and norm_target in target_surnames:
                                return True

        return False

    def evaluate_year(
        self, target_year: Optional[int], candidate_year: Optional[int]
    ) -> tuple[bool, float, Optional[int]]:
        """
        Evaluate publication year proximity.
        Returns (is_valid, penalty_score, year_diff).
        """
        if target_year is None or candidate_year is None:
            return True, 0.0, None

        diff = abs(target_year - candidate_year)
        if diff > self.config.max_year_difference:
            return False, 1.0, diff

        penalty = 0.0
        if diff > self.config.year_penalty_threshold:
            penalty = (diff - self.config.year_penalty_threshold) * 0.05

        return True, min(penalty, 0.5), diff

    def evaluate_candidate(
        self,
        target_record: BibliographicRecord,
        candidate_record: BibliographicRecord,
        dense_score: float,
        acceptance_threshold: float = 0.85,
        author_relationships: Optional[dict[str, set[str]]] = None,
    ) -> ReconciliationMatch:
        """Evaluate a candidate record through short-circuit or hybrid dense+rerank scoring."""
        vol_match = self.check_volume_match(
            target_record.volume_designation,
            candidate_record.volume_designation,
            target_title=target_record.title,
            candidate_title=candidate_record.title,
            target_year=target_record.publication_year,
            candidate_year=candidate_record.publication_year,
        )
        surname_match = self.check_surname_match(
            target_record, candidate_record, author_relationships=author_relationships
        )
        year_valid, year_penalty, year_diff = self.evaluate_year(
            target_record.publication_year, candidate_record.publication_year
        )

        # High-confidence dense short-circuit
        if self.config.short_circuit.enabled and dense_score >= self.config.short_circuit.threshold:
            # Volume match is strictly enforced even on short-circuit!
            if not vol_match:
                return ReconciliationMatch(
                    candidate_record_id=candidate_record.source_id,
                    source=candidate_record.source,
                    match_tier="tier2_dense",
                    match_score=dense_score,
                    dense_score=dense_score,
                    rerank_score=None,
                    volume_match=False,
                    surname_match=surname_match,
                    year_diff=year_diff,
                    accepted=False,
                )

            return ReconciliationMatch(
                candidate_record_id=candidate_record.source_id,
                source=candidate_record.source,
                match_tier="tier2_dense",
                match_score=dense_score,
                dense_score=dense_score,
                rerank_score=None,
                volume_match=True,
                surname_match=surname_match,
                year_diff=year_diff,
                accepted=True,
            )

        # If reranking is disabled, reranking and hard discriminators are bypassed
        if not self.config.enabled:
            accepted = dense_score >= acceptance_threshold
            return ReconciliationMatch(
                candidate_record_id=candidate_record.source_id,
                source=candidate_record.source,
                match_tier="tier2_dense",
                match_score=dense_score,
                dense_score=dense_score,
                rerank_score=None,
                volume_match=vol_match,
                surname_match=surname_match,
                year_diff=year_diff,
                accepted=accepted,
            )

        # Stage 3 Reranking
        # Pre-veto check: if hard constraints fail and pre-veto is enabled, avoid scoring inference
        apply_pre_veto = getattr(self.config, "apply_pre_veto", True)
        hard_pass = vol_match and surname_match and year_valid
        if apply_pre_veto and not hard_pass:
            return ReconciliationMatch(
                candidate_record_id=candidate_record.source_id,
                source=candidate_record.source,
                match_tier="tier2_preveto",
                match_score=round(dense_score, 4),
                dense_score=round(dense_score, 4),
                rerank_score=0.0,
                volume_match=vol_match,
                surname_match=surname_match,
                year_diff=year_diff,
                accepted=False,
            )

        target_text = (
            target_record.serialization or f"{target_record.title} {target_record.publisher or ''}"
        )
        candidate_text = (
            candidate_record.serialization
            or f"{candidate_record.title} {candidate_record.publisher or ''}"
        )
        raw_rerank = self.scorer.score(target_text, candidate_text)
        rerank_score = max(0.0, raw_rerank - year_penalty)

        hybrid_score = (self.config.dense_weight * dense_score) + (
            self.config.rerank_weight * rerank_score
        )

        accepted = hard_pass and (hybrid_score >= acceptance_threshold)
        if self.config.engine == "llm_judge":
            tier_name = "tier2_llm_judge"
        elif self.config.engine == "cascade":
            route = getattr(self.scorer, "last_route", "heuristic")
            tier_name = "tier2_cascade_judge" if route == "judge" else "tier3_cascade_heuristic"
        else:
            tier_name = "tier3_hybrid_rerank"

        return ReconciliationMatch(
            candidate_record_id=candidate_record.source_id,
            source=candidate_record.source,
            match_tier=tier_name,
            match_score=round(hybrid_score, 4),
            dense_score=round(dense_score, 4),
            rerank_score=round(rerank_score, 4),
            volume_match=vol_match,
            surname_match=surname_match,
            year_diff=year_diff,
            accepted=accepted,
        )

    def evaluate_candidates_batch(
        self,
        target_record: BibliographicRecord,
        candidates: list[tuple[BibliographicRecord, float]],
        acceptance_threshold: float = 0.85,
        author_relationships: Optional[dict[str, set[str]]] = None,
        target_encoding: Optional[Any] = None,
    ) -> list[ReconciliationMatch]:
        """
        Evaluate a candidate list against a target record with batch score fusion
        and margin-aware cascade routing.
        """
        if not candidates:
            return []

        # 1. Pre-filter candidates with discriminators and short-circuit
        matches: list[Optional[ReconciliationMatch]] = [None] * len(candidates)
        pending_indices: list[int] = []
        pending_candidates: list[BibliographicRecord] = []
        pending_dense_scores: list[float] = []
        year_penalties: list[float] = []
        hard_passes: list[bool] = []
        year_diffs: list[Optional[int]] = []
        vol_matches: list[bool] = []
        surname_matches: list[bool] = []

        apply_pre_veto = getattr(self.config, "apply_pre_veto", True)

        for i, (cand_rec, dense_score) in enumerate(candidates):
            vol_match = self.check_volume_match(
                target_record.volume_designation,
                cand_rec.volume_designation,
                target_title=target_record.title,
                candidate_title=cand_rec.title,
                target_year=target_record.publication_year,
                candidate_year=cand_rec.publication_year,
            )
            surname_match = self.check_surname_match(
                target_record, cand_rec, author_relationships=author_relationships
            )
            year_valid, year_pen, y_diff = self.evaluate_year(
                target_record.publication_year, cand_rec.publication_year
            )
            hard_pass = vol_match and surname_match and year_valid

            # Short-circuit check
            if (
                self.config.short_circuit.enabled
                and dense_score >= self.config.short_circuit.threshold
            ):
                matches[i] = ReconciliationMatch(
                    candidate_record_id=cand_rec.source_id,
                    source=cand_rec.source,
                    match_tier="tier2_dense",
                    match_score=dense_score,
                    dense_score=dense_score,
                    rerank_score=None,
                    volume_match=vol_match,
                    surname_match=surname_match,
                    year_diff=y_diff,
                    accepted=vol_match,
                )
                continue

            # Disabled reranking bypass
            if not self.config.enabled:
                matches[i] = ReconciliationMatch(
                    candidate_record_id=cand_rec.source_id,
                    source=cand_rec.source,
                    match_tier="tier2_dense",
                    match_score=dense_score,
                    dense_score=dense_score,
                    rerank_score=None,
                    volume_match=vol_match,
                    surname_match=surname_match,
                    year_diff=y_diff,
                    accepted=dense_score >= acceptance_threshold,
                )
                continue

            # Pre-veto check
            if apply_pre_veto and not hard_pass:
                matches[i] = ReconciliationMatch(
                    candidate_record_id=cand_rec.source_id,
                    source=cand_rec.source,
                    match_tier="tier2_preveto",
                    match_score=round(dense_score, 4),
                    dense_score=round(dense_score, 4),
                    rerank_score=0.0,
                    volume_match=vol_match,
                    surname_match=surname_match,
                    year_diff=y_diff,
                    accepted=False,
                )
                continue

            pending_indices.append(i)
            pending_candidates.append(cand_rec)
            pending_dense_scores.append(dense_score)
            year_penalties.append(year_pen)
            hard_passes.append(hard_pass)
            year_diffs.append(y_diff)
            vol_matches.append(vol_match)
            surname_matches.append(surname_match)

        # 2. Batch scoring for candidates passing pre-veto
        if pending_indices:
            target_text = (
                target_record.serialization
                or f"{target_record.title} {target_record.publisher or ''}"
            )
            candidate_texts = [
                c.serialization or f"{c.title} {c.publisher or ''}" for c in pending_candidates
            ]
            scorer_kwargs: dict[str, Any] = {}
            if target_encoding is not None:
                scorer_kwargs["target_encoding"] = target_encoding
            try:
                raw_scores = self.scorer.score_batch(target_text, candidate_texts, **scorer_kwargs)
            except TypeError:
                raw_scores = self.scorer.score_batch(target_text, candidate_texts)
            routes = getattr(self.scorer, "last_routes", ["heuristic"] * len(raw_scores))

            for idx, orig_i in enumerate(pending_indices):
                raw_rerank = raw_scores[idx]
                r_pen = year_penalties[idx]
                rerank_score = max(0.0, raw_rerank - r_pen)
                dense_score = pending_dense_scores[idx]
                cand_rec = pending_candidates[idx]

                hybrid_score = (self.config.dense_weight * dense_score) + (
                    self.config.rerank_weight * rerank_score
                )
                accepted = hard_passes[idx] and (hybrid_score >= acceptance_threshold)

                if self.config.engine == "llm_judge":
                    tier_name = "tier2_llm_judge"
                elif self.config.engine == "cascade":
                    tier_name = (
                        "tier2_cascade_judge"
                        if routes[idx] == "judge"
                        else "tier3_cascade_heuristic"
                    )
                else:
                    tier_name = "tier3_hybrid_rerank"

                matches[orig_i] = ReconciliationMatch(
                    candidate_record_id=cand_rec.source_id,
                    source=cand_rec.source,
                    match_tier=tier_name,
                    match_score=round(hybrid_score, 4),
                    dense_score=round(dense_score, 4),
                    rerank_score=round(rerank_score, 4),
                    volume_match=vol_matches[idx],
                    surname_match=surname_matches[idx],
                    year_diff=year_diffs[idx],
                    accepted=accepted,
                )

        return [m for m in matches if m is not None]
