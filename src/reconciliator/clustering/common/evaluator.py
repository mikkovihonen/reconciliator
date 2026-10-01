"""Composable rule pipeline for pairwise edge evaluation in entity clustering."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from typing import Generic, Optional, TypeVar

TRecord = TypeVar("TRecord")
TContext = TypeVar("TContext")


class EdgeRule(ABC, Generic[TRecord, TContext]):
    """Base marker class for pairwise edge evaluation rules."""

    pass


class ShortcutRule(EdgeRule[TRecord, TContext]):
    """Deterministic fast-path rule that short-circuits scoring on exact matches."""

    @abstractmethod
    def evaluate_shortcut(
        self, r1: TRecord, r2: TRecord, context: TContext
    ) -> Optional[tuple[float, str]]:
        """Return (score, evidence) if shortcut conditions match, else None."""
        ...


class VetoRule(EdgeRule[TRecord, TContext]):
    """Negative compatibility guard that immediately vetoes a match."""

    @abstractmethod
    def evaluate_veto(
        self, r1: TRecord, r2: TRecord, context: TContext
    ) -> Optional[tuple[float, str]]:
        """Return (0.0, veto_reason) if a fatal discrepancy exists, else None."""
        ...


class FeatureScorer(EdgeRule[TRecord, TContext]):
    """Computes a normalized feature similarity sub-score with an assigned weight."""

    @abstractmethod
    def score_feature(
        self, r1: TRecord, r2: TRecord, context: TContext
    ) -> tuple[float, float]:
        """Return (sub_score, weight)."""
        ...


class ModulatorRule(EdgeRule[TRecord, TContext]):
    """Post-processing rule that modulates composite score (e.g. semantic homonym veto or boost)."""

    @abstractmethod
    def modulate(
        self, score: float, r1: TRecord, r2: TRecord, context: TContext
    ) -> tuple[float, str]:
        """Return (modulated_score, evidence_or_veto_reason)."""
        ...


class CompositeEdgeEvaluator(Generic[TRecord, TContext]):
    """Composable pipeline orchestrator for pairwise entity comparison."""

    def __init__(
        self,
        shortcuts: Sequence[ShortcutRule[TRecord, TContext]] = (),
        vetoes: Sequence[VetoRule[TRecord, TContext]] = (),
        scorers: Sequence[FeatureScorer[TRecord, TContext]] = (),
        modulators: Sequence[ModulatorRule[TRecord, TContext]] = (),
        default_method: str = "composite_similarity",
    ) -> None:
        self.shortcuts = list(shortcuts)
        self.vetoes = list(vetoes)
        self.scorers = list(scorers)
        self.modulators = list(modulators)
        self.default_method = default_method

    def evaluate(
        self, r1: TRecord, r2: TRecord, context: TContext
    ) -> tuple[float, str]:
        """Evaluate pairwise match score and evidence across all pipeline stages.

        Execution stages:
        1. Shortcuts: Exact standard identifiers or authoritative domain keys.
        2. Vetoes: Critical discrepancies (volume conflict, numeric title, birth/death discrepancy).
        3. Feature Scorers: Weighted average of normalized attributes.
        4. Modulators: Semantic vector disambiguation, corroboration boosts, homonym vetoes.
        """
        # 1. Deterministic Shortcuts
        for shortcut in self.shortcuts:
            res = shortcut.evaluate_shortcut(r1, r2, context)
            if res is not None:
                return res

        # 2. Hard Vetoes
        for veto in self.vetoes:
            res = veto.evaluate_veto(r1, r2, context)
            if res is not None:
                return res

        # 3. Weighted Feature Scoring
        total_score = 0.0
        total_weight = 0.0
        for scorer in self.scorers:
            sub_score, weight = scorer.score_feature(r1, r2, context)
            if weight > 0.0:
                total_score += sub_score * weight
                total_weight += weight

        composite = total_score / total_weight if total_weight > 0.0 else 0.0
        method = self.default_method

        # 4. Score Modulators / Semantic Homonym Guards
        for mod in self.modulators:
            composite, method = mod.modulate(composite, r1, r2, context)
            if composite <= 0.0:
                return 0.0, method

        return composite, method
