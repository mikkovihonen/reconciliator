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
        """Compute matching score in [0.0, 1.0] for a query-document pair with optional symmetric scoring."""
        if not query or not document:
            return 0.0

        encoding = self.backend.encode_target(query, retain_cache=True)
        prior_bias = getattr(encoding, "prior_bias", 0.0)
        suffix_template = getattr(
            getattr(self.backend, "config", None),
            "candidate_suffix_template",
            "Candidate Record: {text}\n\nQuestion: Do Target Record and Candidate Record represent the same work? Answer YES or NO: ",
        )
        suffix = suffix_template.format(text=document)

        eval_res = self.backend.evaluate_candidate(
            base_cache=encoding.kv_cache,
            base_seq_len=encoding.base_seq_len,
            candidate_suffix=suffix,
            prior_bias=prior_bias,
        )
        fwd_score = float(eval_res.confidence_score)

        if getattr(getattr(self.backend, "config", None), "symmetric_scoring", True):
            enc_rev = self.backend.encode_target(document, retain_cache=True)
            bias_rev = getattr(enc_rev, "prior_bias", 0.0)
            suffix_rev = suffix_template.format(text=query)
            eval_rev = self.backend.evaluate_candidate(
                base_cache=enc_rev.kv_cache,
                base_seq_len=enc_rev.base_seq_len,
                candidate_suffix=suffix_rev,
                prior_bias=bias_rev,
            )
            rev_score = float(eval_rev.confidence_score)
            return (fwd_score + rev_score) / 2.0

        return fwd_score

    def score_batch(
        self,
        query: str,
        documents: list[str],
        past_key_values: Optional[Any] = None,
        target_encoding: Optional[Any] = None,
        **kwargs: Any,
    ) -> list[float]:
        """
        Compute similarity scores for a query across candidate documents.
        Encodes query once, evaluates suffixes via KV-branching with prior calibration,
        and applies symmetric dual-direction evaluation on close contenders.
        """
        if not query or not documents:
            return [0.0] * len(documents)

        # Single-pass encoding of the query (or reuse pre-existing target_encoding from coarse search)
        if target_encoding is not None and getattr(target_encoding, "kv_cache", None) is not None:
            encoding = target_encoding
        else:
            encoding = self.backend.encode_target(query, retain_cache=True)
        prior_bias = getattr(encoding, "prior_bias", 0.0)
        suffix_template = getattr(
            getattr(self.backend, "config", None),
            "candidate_suffix_template",
            "Candidate Record: {text}\n\nQuestion: Do Target Record and Candidate Record represent the same work? Answer YES or NO: ",
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
                prior_bias=prior_bias,
            )
            scores.append(float(eval_res.confidence_score))

        # Symmetric dual-direction scoring on close contenders
        cfg = getattr(self.backend, "config", None)
        if getattr(cfg, "symmetric_scoring", True) and len(documents) > 1:
            threshold = getattr(cfg, "symmetric_margin_threshold", 0.05)
            indexed = sorted(enumerate(scores), key=lambda x: x[1], reverse=True)
            top_idx, top_score = indexed[0]
            second_idx, second_score = indexed[1]

            if (top_score - second_score) <= threshold and top_score > 0.0:
                # Contenders to symmetrically re-score
                contenders = [top_idx]
                for idx, score in indexed[1:]:
                    if (top_score - score) <= threshold:
                        contenders.append(idx)
                    else:
                        break

                for idx in contenders:
                    cand_doc = documents[idx]
                    enc_rev = self.backend.encode_target(cand_doc, retain_cache=True)
                    bias_rev = getattr(enc_rev, "prior_bias", 0.0)
                    suffix_rev = suffix_template.format(text=query)
                    eval_rev = self.backend.evaluate_candidate(
                        base_cache=enc_rev.kv_cache,
                        base_seq_len=enc_rev.base_seq_len,
                        candidate_suffix=suffix_rev,
                        prior_bias=bias_rev,
                    )
                    rev_score = float(eval_rev.confidence_score)
                    scores[idx] = (scores[idx] + rev_score) / 2.0

        return scores
