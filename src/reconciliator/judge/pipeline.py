"""Two-stage entity resolution pipeline integrating coarse retrieval with KV-branching LLM Judge."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Optional

from reconciliator.judge.protocol import LLMJudgeBackend, TargetEncoding
from reconciliator.models import BibliographicRecord, ReconciliationMatch

if TYPE_CHECKING:
    from reconciliator.config import RerankConfig
    from reconciliator.rerank.evaluator import CandidateEvaluator

logger = logging.getLogger(__name__)


class TwoStageEntityResolver:
    """
    Orchestrates the two-stage entity resolution lifecycle:
    1. Single-pass encoding of target entity (dense vector + locked base KV-cache)
    2. Optional pre-veto filtering via deterministic hard discriminators
    3. KV-branching suffix evaluation with greedy argmax decoding
    4. Zero-copy in-place truncation
    """

    def __init__(
        self,
        backend: LLMJudgeBackend,
        config: Optional[RerankConfig] = None,
        evaluator: Optional[CandidateEvaluator] = None,
        apply_pre_veto: bool = True,
    ) -> None:
        self.backend = backend
        self.config = config
        self.evaluator = evaluator
        self.apply_pre_veto = apply_pre_veto

    def encode_target(
        self,
        record: BibliographicRecord | str,
        retain_cache: bool = True,
    ) -> TargetEncoding:
        """Single-pass target encoding generating dense vector and base KV cache."""
        if isinstance(record, BibliographicRecord):
            text = (
                record.serialization
                or f"{record.title} {record.volume_designation or ''} {record.publisher or ''}"
            )
        else:
            text = str(record)

        return self.backend.encode_target(text, retain_cache=retain_cache)

    def evaluate_candidate(
        self,
        target_record: BibliographicRecord,
        candidate_record: BibliographicRecord,
        base_cache: Any,
        base_seq_len: int,
        dense_score: float,
        acceptance_threshold: float = 0.5,
    ) -> ReconciliationMatch:
        """
        Evaluate candidate against target. Applies pre-veto filtering if enabled,
        followed by KV-branching LLM suffix evaluation.
        """
        vol_match = True
        surname_match = True
        year_valid = True
        year_diff: Optional[int] = None

        if self.evaluator is not None and self.apply_pre_veto:
            vol_match = self.evaluator.check_volume_match(
                target_record.volume_designation, candidate_record.volume_designation
            )
            surname_match = self.evaluator.check_surname_match(target_record, candidate_record)
            year_valid, _, year_diff = self.evaluator.evaluate_year(
                target_record.publication_year, candidate_record.publication_year
            )

            # Pre-veto rejection: bypass GPU inference entirely
            if not (vol_match and surname_match and year_valid):
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

        # Build candidate suffix
        cand_text = candidate_record.serialization or (
            f"{candidate_record.title} {candidate_record.volume_designation or ''} "
            f"{candidate_record.publisher or ''}"
        )
        suffix_template = getattr(
            getattr(self.backend, "config", None),
            "candidate_suffix_template",
            "Candidate: {text}\nDoes this refer to the same work? Answer YES or NO: ",
        )
        candidate_suffix = suffix_template.format(text=cand_text)

        # Branching suffix inference with in-place truncation
        eval_res = self.backend.evaluate_candidate(
            base_cache=base_cache,
            base_seq_len=base_seq_len,
            candidate_suffix=candidate_suffix,
        )

        dense_weight = getattr(self.config, "dense_weight", 0.5) if self.config else 0.5
        rerank_weight = getattr(self.config, "rerank_weight", 0.5) if self.config else 0.5

        fused_score = (dense_weight * dense_score) + (rerank_weight * eval_res.confidence_score)
        accepted = eval_res.is_match and (fused_score >= acceptance_threshold)

        return ReconciliationMatch(
            candidate_record_id=candidate_record.source_id,
            source=candidate_record.source,
            match_tier="tier2_llm_judge",
            match_score=round(fused_score, 4),
            dense_score=round(dense_score, 4),
            rerank_score=round(eval_res.confidence_score, 4),
            volume_match=vol_match,
            surname_match=surname_match,
            year_diff=year_diff,
            accepted=accepted,
        )
