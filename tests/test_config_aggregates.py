"""Tests for aggregate clustering and constituent configuration."""

from __future__ import annotations

from reconciliator.config import (
    AggregateClusteringConfig,
    ConstituentsConfig,
    ReconcileConfig,
    WEMIClusterConfig,
)


def test_aggregate_clustering_config_defaults():
    cfg = AggregateClusteringConfig()
    assert cfg.enabled is True
    assert cfg.min_constituents_for_overlap == 3
    assert cfg.jaccard_merge_threshold == 0.80
    assert cfg.jaccard_veto_threshold == 0.30
    assert cfg.merge_bonus_score == 0.95
    assert cfg.sequence_alignment is True
    assert cfg.lcs_threshold == 0.70
    assert cfg.blocking_family == "constituent_work"


def test_cluster_config_has_aggregates():
    cluster_cfg = WEMIClusterConfig()
    assert isinstance(cluster_cfg.aggregates, AggregateClusteringConfig)
    assert cluster_cfg.aggregates.enabled is True


def test_constituents_config_defaults():
    const_cfg = ConstituentsConfig()
    assert const_cfg.profile is None
    assert const_cfg.bibframe_profile == "bibframe_haspart"
    assert const_cfg.bffi_profile == "bffi_haspart"
    assert const_cfg.mode == "keys_only"
    assert const_cfg.index_works is False


def test_full_reconcile_config_loads_aggregates():
    config = ReconcileConfig(target_file="test.xml")
    assert config.cluster.aggregates.enabled is True
    assert config.cluster.aggregates.min_constituents_for_overlap == 3
    assert config.constituents.bffi_profile == "bffi_haspart"
    assert config.constituents.bibframe_profile == "bibframe_haspart"
