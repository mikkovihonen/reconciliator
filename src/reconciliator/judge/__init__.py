"""Autoregressive LLM Judge with KV-branching and zero-copy truncation."""

from reconciliator.judge.factory import create_judge_backend
from reconciliator.judge.pipeline import TwoStageEntityResolver
from reconciliator.judge.protocol import (
    CandidateEvaluation,
    LLMJudgeBackend,
    TargetEncoding,
)
from reconciliator.judge.torch_backend import PytorchJudgeBackend

__all__ = [
    "CandidateEvaluation",
    "LLMJudgeBackend",
    "PytorchJudgeBackend",
    "TargetEncoding",
    "TwoStageEntityResolver",
    "create_judge_backend",
]

