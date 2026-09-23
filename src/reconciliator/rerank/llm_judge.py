"""LLM Judge reranking scorer based on autoregressive single-pass KV-branching."""

from __future__ import annotations

import logging
from typing import Any, Optional

from reconciliator.judge.factory import create_judge_backend
from reconciliator.judge.protocol import LLMJudgeBackend

logger = logging.getLogger(__name__)


class LLMJudgeScorer:
    """
    Rerank scorer implementing RerankScorer protocol backed by an autoregressive LLM.
    Leverages single-pass prompt encoding and KV-cache branching with in-place truncation.
    """

    def __init__(
        self,
        backend: Optional[LLMJudgeBackend] = None,
        config: Optional[Any] = None,
    ) -> None:
        if backend is not None:
            self.backend = backend
        elif config is not None:
            self.backend = create_judge_backend(config)
        else:
            from reconciliator.config import LLMJudgeConfig

            self.backend = create_judge_backend(LLMJudgeConfig())

    def score(self, query: str, document: str) -> float:
        """Compute matching score in [0.0, 1.0] for a query-document pair."""
        if not query or not document:
            return 0.0

        encoding = self.backend.encode_target(query, retain_cache=True)
        suffix_template = getattr(
            getattr(self.backend, "config", None),
            "candidate_suffix_template",
            "Candidate: {text}\nDoes this refer to the same work? Answer YES or NO: ",
        )
        suffix = suffix_template.format(text=document)

        eval_res = self.backend.evaluate_candidate(
            base_cache=encoding.kv_cache,
            base_seq_len=encoding.base_seq_len,
            candidate_suffix=suffix,
        )
        return float(eval_res.confidence_score)

    def score_batch(self, query: str, documents: list[str]) -> list[float]:
        """
        Compute similarity scores for a query across candidate documents.
        Encodes query once, then evaluates all candidate suffixes via KV-branching.
        """
        if not query or not documents:
            return [0.0] * len(documents)

        # Single-pass encoding of the query
        encoding = self.backend.encode_target(query, retain_cache=True)
        suffix_template = getattr(
            getattr(self.backend, "config", None),
            "candidate_suffix_template",
            "Candidate: {text}\nDoes this refer to the same work? Answer YES or NO: ",
        )

        scores: list[float] = []
        for doc in documents:
            if not doc:
                scores.append(0.0)
                continue
            suffix = suffix_template.format(text=doc)
            eval_res = self.backend.evaluate_candidate(
                base_cache=encoding.kv_cache,
                base_seq_len=encoding.base_seq_len,
                candidate_suffix=suffix,
            )
            scores.append(float(eval_res.confidence_score))

        return scores
