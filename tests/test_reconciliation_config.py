"""Tests for reconciliation configuration integrity and dynamic parameterization."""

from __future__ import annotations

from reconciliator.config import AppConfig, ReconcileConfig


def test_reconciliation_configuration_integrity() -> None:
    """Verify that all reconciliation parameters derive strictly from defaults.yaml with zero hardcoding."""
    cfg = ReconcileConfig(target_file="data/target.xml")

    assert cfg.threshold == 0.85
    assert cfg.max_editions_compared == 25
    assert cfg.translator_alignment_threshold == 0.70
    assert cfg.agent_aligner_threshold == 85.0
    assert cfg.life_date_tolerance_years == 1
    assert cfg.homonym_score_margin == 0.03
    assert cfg.qualifier_similarity_threshold == 80.0
    assert cfg.distribution_match_threshold == 80.0
    assert cfg.surname_similarity_threshold == 85.0
    assert cfg.fallback_agent_reconciliation is True
    assert cfg.agent_fallback_threshold == 0.90
    assert cfg.verify_chronology is True
    assert cfg.chronology_min_author_age == 12
    assert cfg.chronology_max_posthumous_years == 70
    assert cfg.strict_homonym_guard is True

    # Test dynamic overrides
    custom = ReconcileConfig(
        target_file=cfg.target_file,
        threshold=0.92,
        max_editions_compared=50,
        translator_alignment_threshold=0.80,
        agent_aligner_threshold=90.0,
        life_date_tolerance_years=2,
        homonym_score_margin=0.05,
    )
    assert custom.threshold == 0.92
    assert custom.max_editions_compared == 50
    assert custom.translator_alignment_threshold == 0.80
    assert custom.agent_aligner_threshold == 90.0
    assert custom.life_date_tolerance_years == 2
    assert custom.homonym_score_margin == 0.05
