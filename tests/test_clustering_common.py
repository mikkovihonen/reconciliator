"""Tests for shared clustering algorithms, candidate blocker, canonical selection, and edge evaluation pipeline."""

from unittest.mock import MagicMock

import pytest

from reconciliator.clustering.agent.scoring import score_agent_pair
from reconciliator.clustering.bigclust.graph import (
    SparseSimilarityGraph,
    build_sparse_similarity_graph,
)
from reconciliator.clustering.common.blocker import InvertedIndexBlocker
from reconciliator.clustering.common.canonical import CanonicalRepresentativeSelector
from reconciliator.clustering.common.evaluator import (
    CompositeEdgeEvaluator,
    FeatureScorer,
    ModulatorRule,
    ShortcutRule,
    VetoRule,
)
from reconciliator.clustering.evaluator import BibliographicalEdgeEvaluator
from reconciliator.clustering.graph import MultiplexGraphClusterer
from reconciliator.config import AgentClusterConfig, WEMIClusterConfig
from reconciliator.models import MatchRecord


def test_inverted_index_blocker_pair_generation():
    """Verify InvertedIndexBlocker groups by keys and respects bucket size bounds."""
    records = [
        {"id": 1, "keys": ["group_a", "shared"]},
        {"id": 2, "keys": ["group_a", "shared"]},
        {"id": 3, "keys": ["group_b", "shared"]},
        {"id": 4, "keys": ["group_b", "shared"]},
        {"id": 5, "keys": ["shared"]},  # "shared" has 5 items
    ]

    # Max bucket size 2: "shared" (5 items) ignored, "group_a" (2 items) and "group_b" (2 items) retained
    blocker = InvertedIndexBlocker[dict, int](
        key_extractor=lambda r: r["keys"],
        id_extractor=lambda r: r["id"],
        min_bucket_size=2,
        max_bucket_size=2,
    )

    pairs = blocker.generate_candidate_pairs(records)
    assert (1, 2) in pairs
    assert (3, 4) in pairs
    # Pair (1, 3) would only come from "shared" which exceeds max_bucket_size 2
    assert (1, 3) not in pairs
    assert len(pairs) == 2


def test_inverted_index_blocker_string_ids():
    """Verify InvertedIndexBlocker works with string IDs and canonical sorting."""
    records = [
        MatchRecord(record_id="rec_b", format="marcxml", title="Book 1", title_stem="book 1"),
        MatchRecord(record_id="rec_a", format="marcxml", title="Book 1", title_stem="book 1"),
    ]
    blocker = InvertedIndexBlocker[MatchRecord, str](
        key_extractor=lambda r: [r.title or ""],
        id_extractor=lambda r: r.record_id,
        min_bucket_size=2,
        max_bucket_size=10,
    )
    pairs = blocker.generate_candidate_pairs(records)
    assert pairs == {("rec_a", "rec_b")}


def test_canonical_representative_selector_precedence_and_tie_breaker():
    """Verify CanonicalRepresentativeSelector orders by source authority hierarchy and tie breakers."""
    selector = CanonicalRepresentativeSelector[dict](
        source_precedence=["asteri", "melinda", "wikidata"],
        source_extractor=lambda r: r["source"],
        tie_breaker=lambda r: (
            0 if r.get("birth_year") is not None else 1,
            -len(r.get("name", "")),
            r.get("id"),
        ),
    )

    r_wikidata = {"id": 1, "source": "wikidata", "name": "Aleksis Kivi", "birth_year": 1834}
    r_asteri = {"id": 2, "source": "asteri", "name": "Kivi, Aleksis", "birth_year": None}
    r_melinda = {"id": 3, "source": "melinda", "name": "Kivi, Aleksis", "birth_year": 1834}

    # Asteri is highest in source precedence despite missing birth_year
    records = [r_wikidata, r_melinda, r_asteri]
    canonical = selector.select_canonical(records)
    assert canonical["source"] == "asteri"

    # Between same source, birth_year presence breaks tie
    r_melinda_no_dates = {"id": 4, "source": "melinda", "name": "Kivi, Aleksis", "birth_year": None}
    melinda_records = [r_melinda_no_dates, r_melinda]
    assert selector.select_canonical(melinda_records)["id"] == 3


def test_canonical_representative_selector_empty_error():
    """Verify ValueError is raised on empty candidate list."""
    selector = CanonicalRepresentativeSelector[dict]()
    with pytest.raises(ValueError, match="Cannot select canonical representative"):
        selector.select_canonical([])


class SampleShortcut(ShortcutRule[dict, dict]):
    def evaluate_shortcut(self, r1: dict, r2: dict, context: dict):
        if r1.get("isbn") and r1.get("isbn") == r2.get("isbn"):
            return 1.0, "isbn_exact"
        return None


class SampleVeto(VetoRule[dict, dict]):
    def evaluate_veto(self, r1: dict, r2: dict, context: dict):
        if r1.get("vol") != r2.get("vol"):
            return 0.0, "volume_conflict"
        return None


class SampleScorer(FeatureScorer[dict, dict]):
    def score_feature(self, r1: dict, r2: dict, context: dict):
        # 0.80 similarity with weight 1.0
        return 0.80, 1.0


class SampleModulator(ModulatorRule[dict, dict]):
    def modulate(self, score: float, r1: dict, r2: dict, context: dict):
        if context.get("apply_penalty"):
            return score * 0.5, "penalized"
        return score, "unmodulated"


def test_composite_edge_evaluator_stages():
    """Verify CompositeEdgeEvaluator processes shortcuts, vetoes, scorers, and modulators in order."""
    evaluator = CompositeEdgeEvaluator[dict, dict](
        shortcuts=[SampleShortcut()],
        vetoes=[SampleVeto()],
        scorers=[SampleScorer()],
        modulators=[SampleModulator()],
    )

    # 1. Shortcut matches immediately
    score, method = evaluator.evaluate(
        {"isbn": "123", "vol": "1"}, {"isbn": "123", "vol": "2"}, {}
    )
    assert score == 1.0
    assert method == "isbn_exact"

    # 2. Veto triggers if shortcut doesn't apply
    score, method = evaluator.evaluate(
        {"isbn": "111", "vol": "1"}, {"isbn": "222", "vol": "2"}, {}
    )
    assert score == 0.0
    assert method == "volume_conflict"

    # 3. Feature scorer calculates score without veto
    score, method = evaluator.evaluate(
        {"isbn": "111", "vol": "1"}, {"isbn": "222", "vol": "1"}, {}
    )
    assert score == 0.80
    assert method == "unmodulated"

    # 4. Modulator modifies score
    score, method = evaluator.evaluate(
        {"isbn": "111", "vol": "1"}, {"isbn": "222", "vol": "1"}, {"apply_penalty": True}
    )
    assert score == 0.40
    assert method == "penalized"


def test_bibliographical_edge_evaluator_with_common_pipeline():
    """Verify BibliographicalEdgeEvaluator computes pairwise scores via CompositeEdgeEvaluator."""
    evaluator = BibliographicalEdgeEvaluator(config=WEMIClusterConfig())

    r1 = MatchRecord(
        record_id="1",
        format="marcxml",
        title="Tuntematon sotilas",
        title_stem="tuntematon sotilas",
        author_key="linna vaino",
        year=1954,
    )
    r2 = MatchRecord(
        record_id="2",
        format="marcxml",
        title="Tuntematon sotilas",
        title_stem="tuntematon sotilas",
        author_key="linna vaino",
        year=1954,
    )

    # Identical records without IDs evaluate to high metadata similarity
    score, evidence = evaluator.evaluate(r1, r2, {}, {})
    assert score >= 0.95
    assert evidence == "metadata_similarity"

    # Exact ISBN shortcut returns 1.0 immediately
    score_isbn, evidence_isbn = evaluator.evaluate(
        r1, r2, {"isbn": ["978-951-0-12345-6"]}, {"isbn": ["9789510123456"]}
    )
    assert score_isbn == 1.0
    assert evidence_isbn == "identifier:isbn"

    # Volume designation mismatch returns hard veto
    r1_v1 = MatchRecord(
        record_id="1", format="marcxml", title="Opus", title_stem="opus", volume_designation="Osa 1"
    )
    r2_v2 = MatchRecord(
        record_id="2", format="marcxml", title="Opus", title_stem="opus", volume_designation="Osa 2"
    )
    score_v, reason_v = evaluator.evaluate(r1_v1, r2_v2, {}, {})
    assert score_v == 0.0
    assert reason_v == "volume_conflict"


def test_sparse_similarity_graph_construction_for_bigclust():
    """Verify build_sparse_similarity_graph produces valid PyTorch COO sparse graphs."""
    nodes = ["agent_1", "agent_2", "agent_3"]
    edges = [
        ("agent_1", "agent_2", 0.95),
        ("agent_2", "agent_3", 0.80),
        ("agent_1", "agent_3", 0.20),  # Below threshold
    ]

    graph: SparseSimilarityGraph[str] = build_sparse_similarity_graph(
        nodes=nodes,
        edges=edges,
        min_weight=0.50,
    )

    assert graph.nodes == nodes
    assert graph.edge_index.shape[0] == 2
    # 2 edges above threshold * 2 for symmetrization = 4 directed edges
    assert graph.edge_index.shape[1] == 4
    assert graph.edge_weight.shape[0] == 4

    # Convert to sparse COO tensor
    coo = graph.to_sparse_coo()
    assert coo.shape == (3, 3)
    dense = coo.to_dense()
    assert dense[0, 1] == pytest.approx(0.95, rel=1e-3)
    assert dense[1, 0] == pytest.approx(0.95, rel=1e-3)
    assert dense[1, 2] == pytest.approx(0.80, rel=1e-3)
    assert dense[0, 2] == 0.0  # Filtered out


def test_wemi_deterministic_prelinking_fast_path():
    """Verify MultiplexGraphClusterer uses deterministic pre-linking and skips redundant scoring."""
    mock_repo = MagicMock()
    mock_repo.get_all_cluster_mappings.return_value = {}

    clusterer = MultiplexGraphClusterer(corpus_repo=mock_repo)

    r1 = MatchRecord(
        record_id="rec_1",
        format="marcxml",
        title="Python Programming",
        title_stem="python programming",
        author_key="van rossum",
        year=2020,
        keys={"isbn": ["978-0-123456-78-9"]},
    )
    r2 = MatchRecord(
        record_id="rec_2",
        format="marcxml",
        title="Python Guide (2nd Ed)",  # Diff title
        title_stem="python guide 2nd ed",
        author_key="guido van rossum",
        year=2021,
        keys={"isbn": ["9780123456789"]},
    )
    r3 = MatchRecord(
        record_id="rec_3",
        format="marcxml",
        title="Rust Primer",
        title_stem="rust primer",
        author_key="matsakis",
        year=2022,
        keys={"isbn": ["978-1-987654-32-1"]},
    )

    res = clusterer.cluster_records([r1, r2, r3], persist=False)
    # rec_1 and rec_2 merged via deterministic ISBN pre-linking, leaving 2 clusters out of 3 records
    assert res.work_clusters_count == 2
    assert res.records_clustered == 3


def test_agent_scoring_pipeline_and_major_date_veto():
    """Verify score_agent_pair uses rule pipeline, enforces date tolerances, and applies major date veto."""
    cfg = AgentClusterConfig()
    mock_repo = MagicMock()

    a1 = {"id": 1, "name_normalized": "matti meikalainen", "birth_year": 1950, "death_year": None}
    a2_same_year = {"id": 2, "name_normalized": "matti meikalainen", "birth_year": 1950, "death_year": None}
    a3_major_diff = {"id": 3, "name_normalized": "matti meikalainen", "birth_year": 1980, "death_year": None}

    # Same birth year -> high score
    score_ok, method_ok = score_agent_pair(
        a1, a2_same_year, {}, {}, set(), set(), set(), set(), set(), set(), set(), set(), cfg, mock_repo
    )
    assert score_ok >= 0.85
    assert method_ok == "weighted_scoring"

    # Major birth year discrepancy (>= 2 years) without corroboration -> hard veto
    score_veto, reason_veto = score_agent_pair(
        a1, a3_major_diff, {}, {}, set(), set(), set(), set(), set(), set(), set(), set(), cfg, mock_repo
    )
    assert score_veto == 0.0
    assert reason_veto == "veto_birth_year_major_discrepancy"


def test_bibliographical_homonym_divergence_veto():
    """Verify BibliographicalEdgeEvaluator vetoes identical titles when embeddings diverge."""
    mock_embedder = MagicMock()
    mock_repo = MagicMock()

    # Mock cached embeddings returning orthogonal vectors (cosine similarity 0.0 < 0.40)
    mock_repo.embed_texts_cached.return_value = [
        [1.0, 0.0, 0.0],
        [0.0, 1.0, 0.0],
    ]

    evaluator = BibliographicalEdgeEvaluator(
        config=WEMIClusterConfig(),
        embedder=mock_embedder,
        repo=mock_repo,
    )

    r1 = MatchRecord(
        record_id="b1",
        format="marcxml",
        title="Johdatus fysiikkaan",
        title_stem="johdatus fysiikkaan",
        author_key="virtanen",
        year=1970,
    )
    r2 = MatchRecord(
        record_id="b2",
        format="marcxml",
        title="Johdatus fysiikkaan",
        title_stem="johdatus fysiikkaan",
        author_key="makela",  # Different author
        year=2015,
    )

    score, reason = evaluator.evaluate(r1, r2, {}, {})
    assert score == 0.0
    assert reason == "veto_semantic_homonym_divergence"

