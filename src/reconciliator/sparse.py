"""Sparse lexical reranker, BM25 scoring, and hard metadata discriminators."""

from __future__ import annotations

import re
from typing import Optional

from rank_bm25 import BM25Okapi

from reconciliator.config import SparseRerankConfig
from reconciliator.models import BibliographicRecord, ReconciliationMatch


def tokenize_text(text: str) -> list[str]:
    """Tokenize text into lowercase alphanumeric tokens."""
    if not text:
        return []
    return re.findall(r"\w+", text.lower())


class SparseReranker:
    """Configurable candidate reranker applying BM25 scoring, volume checks, and surname validation."""

    def __init__(self, config: SparseRerankConfig) -> None:
        self.config = config

    def check_volume_match(
        self, target_volume: Optional[str], candidate_volume: Optional[str]
    ) -> bool:
        """
        Check volume/part designation compatibility.
        Rejects conflicting designations (e.g. 'osa 1' vs 'osa 2').
        """
        if not self.config.require_volume_match:
            return True
        if not target_volume or not candidate_volume:
            return True
        return target_volume.strip().lower() == candidate_volume.strip().lower()

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

    def compute_bm25_score(self, target_text: str, candidate_text: str) -> float:
        """Compute normalized BM25 lexical similarity in [0.0, 1.0]."""
        target_tokens = tokenize_text(target_text)
        cand_tokens = tokenize_text(candidate_text)

        if not target_tokens or not cand_tokens:
            return 0.0

        bm25 = BM25Okapi([cand_tokens])
        raw_score = bm25.get_scores(target_tokens)[0]

        # Max possible score reference for normalization
        max_possible = bm25.get_scores(cand_tokens)[0]
        if max_possible > 0:
            norm_score = raw_score / max_possible
        else:
            norm_score = 0.0

        return max(0.0, min(1.0, float(norm_score)))

    def evaluate_candidate(
        self,
        target_record: BibliographicRecord,
        candidate_record: BibliographicRecord,
        dense_score: float,
        acceptance_threshold: float = 0.85,
    ) -> ReconciliationMatch:
        """Evaluate a candidate record through short-circuit or hybrid dense+sparse scoring."""
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
                    sparse_score=None,
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
                sparse_score=None,
                volume_match=True,
                surname_match=surname_match,
                year_diff=year_diff,
                accepted=True,
            )

        # If sparse rerank is disabled, lexical reranking and hard discriminators are bypassed
        if not self.config.enabled:
            accepted = dense_score >= acceptance_threshold
            return ReconciliationMatch(
                candidate_record_id=candidate_record.source_id,
                source=candidate_record.source,
                match_tier="tier2_dense",
                match_score=dense_score,
                dense_score=dense_score,
                sparse_score=None,
                volume_match=vol_match,
                surname_match=surname_match,
                year_diff=year_diff,
                accepted=accepted,
            )

        # Stage 2 BM25 reranking
        raw_sparse = self.compute_bm25_score(
            f"{target_record.title} {target_record.publisher or ''}",
            f"{candidate_record.title} {candidate_record.publisher or ''}",
        )
        sparse_score = max(0.0, raw_sparse - year_penalty)

        hybrid_score = (self.config.dense_weight * dense_score) + (
            self.config.sparse_weight * sparse_score
        )

        hard_pass = vol_match and surname_match and year_valid
        accepted = hard_pass and (hybrid_score >= acceptance_threshold)

        return ReconciliationMatch(
            candidate_record_id=candidate_record.source_id,
            source=candidate_record.source,
            match_tier="tier3_hybrid_rerank",
            match_score=round(hybrid_score, 4),
            dense_score=round(dense_score, 4),
            sparse_score=round(sparse_score, 4),
            volume_match=vol_match,
            surname_match=surname_match,
            year_diff=year_diff,
            accepted=accepted,
        )
