"""Tests for cross-format aggregate clustering:
- Jaccard merge bonus (>= 80% constituent overlap)
- Jaccard mismatch veto (<= 30% constituent overlap)
- Container-part anti-collision guard
- Sequence alignment via longest common subsequence (LCS)
- End-to-end cross-format container clustering with constituent micro-clustering
"""

import pytest
from unittest.mock import MagicMock
from reconciliator.models import MatchRecord, WorkCluster
from reconciliator.clustering.corpus import CorpusClusterer, _lcs_length
from reconciliator.config.clustering import ClusterConfig, AggregateClusteringConfig


def test_lcs_length():
    """Verify LCS helper correctly calculates longest common subsequence."""
    assert _lcs_length([], []) == 0
    assert _lcs_length(["a", "b", "c"], ["a", "b", "c"]) == 3
    assert _lcs_length(["a", "b", "c"], ["x", "y", "z"]) == 0
    assert _lcs_length(["a", "b", "c", "d"], ["a", "c", "d"]) == 3
    assert _lcs_length(["a", "b", "c", "d"], ["d", "c", "b", "a"]) == 1


def test_pairwise_aggregate_jaccard_merge_bonus():
    """Verify that records sharing >= 80% constituent works receive merge bonus."""
    config = ClusterConfig(
        aggregates=AggregateClusteringConfig(
            enabled=True,
            jaccard_merge_threshold=0.80,
            merge_bonus_score=0.95,
            sequence_alignment=False,
        )
    )
    clusterer = CorpusClusterer(corpus_repo=MagicMock(), config=config)

    # 4 out of 5 overlap: Jaccard = 4/5 = 0.80 >= 0.80
    r1 = MatchRecord(
        record_id="koha/101",
        format="marcxml",
        title="Laulukokoelma A",
        title_stem="laulukokoelma a",
        constituent_cluster_ids=["cw_1", "cw_2", "cw_3", "cw_4"],
    )
    r2 = MatchRecord(
        record_id="natlibfi/202",
        format="bffi_rdf",
        title="Laulukirja B",  # Different title
        title_stem="laulukirja b",
        constituent_cluster_ids=["cw_1", "cw_2", "cw_3", "cw_4", "cw_5"],
    )

    score, evidence = clusterer.compute_pairwise_score(r1, r2, {}, {})
    assert score == 0.95
    assert evidence == "aggregate_jaccard_merge"


def test_pairwise_aggregate_jaccard_mismatch_veto():
    """Verify that records sharing <= 30% constituent works are hard-vetoed."""
    config = ClusterConfig(
        aggregates=AggregateClusteringConfig(
            enabled=True,
            jaccard_veto_threshold=0.30,
            min_constituents_for_overlap=2,
        )
    )
    clusterer = CorpusClusterer(corpus_repo=MagicMock(), config=config)

    # Sharing 1 out of 6 distinct tracks: Jaccard = 1/6 = 0.166 <= 0.30
    r1 = MatchRecord(
        record_id="koha/101",
        format="marcxml",
        title="Kitaralauluja",
        title_stem="kitaralauluja",
        constituent_cluster_ids=["cw_1", "cw_2", "cw_3"],
    )
    r2 = MatchRecord(
        record_id="natlibfi/202",
        format="bffi_rdf",
        title="Kitaralauluja",  # Identical title, but totally different songs!
        title_stem="kitaralauluja",
        constituent_cluster_ids=["cw_1", "cw_4", "cw_5", "cw_6"],
    )

    score, evidence = clusterer.compute_pairwise_score(r1, r2, {}, {})
    assert score == 0.0
    assert evidence == "aggregate_content_mismatch"


def test_pairwise_container_part_collision_veto():
    """Verify that container records and individual constituent components never merge."""
    config = ClusterConfig(
        aggregates=AggregateClusteringConfig(
            enabled=True,
            min_constituents_for_overlap=2,
        )
    )
    clusterer = CorpusClusterer(corpus_repo=MagicMock(), config=config)

    # Container with 3 songs
    container = MatchRecord(
        record_id="koha/songbook_1",
        format="marcxml",
        title="Suuri toivelaulukirja 1",
        title_stem="suuri toivelaulukirja 1",
        constituent_cluster_ids=["cw_1", "cw_2", "cw_3"],
    )
    # Individual constituent song published as component
    component = MatchRecord(
        record_id="natlibfi/song_1",
        format="bffi_rdf",
        title="Suuri toivelaulukirja 1",  # Same string label accidentally
        title_stem="suuri toivelaulukirja 1",
        is_component=True,
    )

    score, evidence = clusterer.compute_pairwise_score(container, component, {}, {})
    assert score == 0.0
    assert evidence == "container_part_collision"


def test_pairwise_sequence_alignment_distinction():
    """Verify that sequence alignment distinguishes ordered albums from scrambled ones."""
    config = ClusterConfig(
        aggregates=AggregateClusteringConfig(
            enabled=True,
            jaccard_merge_threshold=0.80,
            merge_bonus_score=0.95,
            sequence_alignment=True,
            lcs_threshold=0.70,
        )
    )
    clusterer = CorpusClusterer(corpus_repo=MagicMock(), config=config)

    # Case A: Identical sequence order
    r1 = MatchRecord(
        record_id="src_a/rec_1",
        format="marcxml",
        title="Album Alpha",
        title_stem="album alpha",
        author_key="artist_x",
        constituent_cluster_ids=["t1", "t2", "t3", "t4", "t5"],
    )
    r2_ordered = MatchRecord(
        record_id="src_b/rec_2",
        format="bffi_rdf",
        title="Album Beta",
        title_stem="album beta",
        author_key="artist_x",
        constituent_cluster_ids=["t1", "t2", "t3", "t4", "t5"],
    )

    score_ordered, ev_ordered = clusterer.compute_pairwise_score(r1, r2_ordered, {}, {})
    assert score_ordered == 0.95
    assert ev_ordered == "aggregate_jaccard_lcs_merge"

    # Case B: Completely reversed / scrambled sequence
    r3_scrambled = MatchRecord(
        record_id="src_c/rec_3",
        format="bibframe_rdf",
        title="Album Scrambled",
        title_stem="album scrambled",
        author_key="artist_y",
        constituent_cluster_ids=["t5", "t4", "t3", "t2", "t1"],
    )
    # Jaccard is 1.0, but LCS is 1 out of 5: lcs_ratio = (2*1)/10 = 0.2 < 0.70
    score_scrambled, ev_scrambled = clusterer.compute_pairwise_score(r1, r3_scrambled, {}, {})
    # Should not receive the sequence-aligned bonus
    assert ev_scrambled != "aggregate_jaccard_lcs_merge"
    assert ev_scrambled != "aggregate_jaccard_merge"


def test_blocking_keys_constituent_work():
    """Verify that container records generate constituent_work blocking keys."""
    config = ClusterConfig(
        aggregates=AggregateClusteringConfig(
            enabled=True,
            blocking_family="constituent_work",
        )
    )
    clusterer = CorpusClusterer(corpus_repo=MagicMock(), config=config)
    r = MatchRecord(
        record_id="koha/101",
        format="marcxml",
        title="Songbook",
        title_stem="songbook",
        constituent_cluster_ids=["va_const_work_123", "va_const_work_456"],
    )

    keys = clusterer.generate_blocking_keys(r)
    assert "constituent_work::va_const_work_123" in keys
    assert "constituent_work::va_const_work_456" in keys
