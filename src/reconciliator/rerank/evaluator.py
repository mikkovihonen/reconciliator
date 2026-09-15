"""Candidate evaluation, metadata discrimination, and score fusion pipeline."""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

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
    ) -> None:
        self.config = config
        self.scorer: RerankScorer = scorer or create_rerank_scorer(config)

    def check_volume_match(
        self, target_volume: Optional[str], candidate_volume: Optional[str]
    ) -> bool:
        """
        Check volume/part designation compatibility.
        Rejects conflicting designations (e.g. 'osa 1' vs 'osa 2').

        Compares *normalised* designations. Raw string equality made this check
        reject every candidate whose volume was merely spelled differently -
        "osa 2" against "2. osa", or against "Vol. II" - which is the usual case
        across two catalogues. `normalize_volume` canonicalises Finnish, Swedish
        and English prefixes and Roman numerals to one form, and exists for
        exactly this comparison.
        """
        if not self.config.require_volume_match:
            return True
        if not target_volume or not candidate_volume:
            return True

        from reconciliator.normalizers.record import normalize_volume

        target_norm = normalize_volume(target_volume) or target_volume.strip().lower()
        candidate_norm = normalize_volume(candidate_volume) or candidate_volume.strip().lower()
        return target_norm == candidate_norm

    def check_surname_match(
        self, target_record: BibliographicRecord, candidate_record: BibliographicRecord
    ) -> bool:
        """Verify that at least one author surname matches between records."""
        if not self.config.require_surname_match:
            return True

        target_surnames = {a.surname.lower() for a in target_record.agents if a.surname}
        candidate_surnames = {a.surname.lower() for a in candidate_record.agents if a.surname}

        # If either record has no author surnames, don't penalize
        if not target_surnames or not candidate_surnames:
            return True

        return bool(target_surnames & candidate_surnames)

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
    ) -> ReconciliationMatch:
        """Evaluate a candidate record through short-circuit or hybrid dense+rerank scoring."""
        vol_match = self.check_volume_match(
            target_record.volume_designation, candidate_record.volume_designation
        )
        surname_match = self.check_surname_match(target_record, candidate_record)
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
        target_text = f"{target_record.title} {target_record.publisher or ''}"
        candidate_text = f"{candidate_record.title} {candidate_record.publisher or ''}"
        raw_rerank = self.scorer.score(target_text, candidate_text)
        rerank_score = max(0.0, raw_rerank - year_penalty)

        hybrid_score = (self.config.dense_weight * dense_score) + (
            self.config.rerank_weight * rerank_score
        )

        hard_pass = vol_match and surname_match and year_valid
        accepted = hard_pass and (hybrid_score >= acceptance_threshold)

        return ReconciliationMatch(
            candidate_record_id=candidate_record.source_id,
            source=candidate_record.source,
            match_tier="tier3_hybrid_rerank",
            match_score=round(hybrid_score, 4),
            dense_score=round(dense_score, 4),
            rerank_score=round(rerank_score, 4),
            volume_match=vol_match,
            surname_match=surname_match,
            year_diff=year_diff,
            accepted=accepted,
        )
