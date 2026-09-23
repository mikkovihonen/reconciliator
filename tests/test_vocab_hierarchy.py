"""Unit and integration tests for generic value vocabulary hierarchy reasoning."""

from __future__ import annotations

from pathlib import Path

from reconciliator.config import HierarchyConfig
from reconciliator.ingestion.vocab import parse_turtle_simple
from reconciliator.vocab import ConceptGraph, HierarchyService, VocabStore


def test_concept_graph_basic_traversal() -> None:
    """Test directed graph building, parents, children, ancestors, and descendants."""
    graph = ConceptGraph()
    # Hierarchy:
    # soittimet (instruments)
    #   ^
    # jousisoittimet (bowed string instruments)
    #   ^
    # viulut (violins)
    #   ^
    # barokkiviulut (baroque violins)
    graph.add_edge("jousisoittimet", "soittimet")
    graph.add_edge("viulut", "jousisoittimet")
    graph.add_edge("barokkiviulut", "viulut")

    assert graph.get_parents("viulut") == {"jousisoittimet"}
    assert graph.get_children("jousisoittimet") == {"viulut"}

    anc = graph.get_ancestors("barokkiviulut", max_depth=5)
    assert anc == {"viulut": 1, "jousisoittimet": 2, "soittimet": 3}

    desc = graph.get_descendants("soittimet", max_depth=5)
    assert desc == {"jousisoittimet": 1, "viulut": 2, "barokkiviulut": 3}


def test_concept_graph_cycle_safety() -> None:
    """Ensure graph traversal terminates safely even in malformed cyclic graphs."""
    graph = ConceptGraph()
    graph.add_edge("a", "b")
    graph.add_edge("b", "c")
    graph.add_edge("c", "a")  # Cycle!

    # Must terminate and not hang
    anc = graph.get_ancestors("a", max_depth=10)
    assert "b" in anc
    assert "c" in anc


def test_lowest_common_ancestor_and_distance() -> None:
    """Test LCA resolution and distance computation between concepts in DAG."""
    graph = ConceptGraph()
    #          soittimet
    #         /         \
    #   jousisoittimet   puhallinsoittimet
    #      /        \            \
    #   viulut    alttoviulut   huilut
    graph.add_edge("jousisoittimet", "soittimet")
    graph.add_edge("puhallinsoittimet", "soittimet")
    graph.add_edge("viulut", "jousisoittimet")
    graph.add_edge("alttoviulut", "jousisoittimet")
    graph.add_edge("huilut", "puhallinsoittimet")

    # 1. Identity
    lca, da, db = graph.find_lowest_common_ancestor("viulut", "viulut")
    assert lca == "viulut"
    assert da == 0 and db == 0
    assert graph.compute_distance("viulut", "viulut") == 0

    # 2. Parent-child
    lca, da, db = graph.find_lowest_common_ancestor("viulut", "jousisoittimet")
    assert lca == "jousisoittimet"
    assert da == 1 and db == 0
    assert graph.compute_distance("viulut", "jousisoittimet") == 1

    # 3. Siblings (viulut and alttoviulut share jousisoittimet)
    lca, da, db = graph.find_lowest_common_ancestor("viulut", "alttoviulut")
    assert lca == "jousisoittimet"
    assert da == 1 and db == 1
    assert graph.compute_distance("viulut", "alttoviulut") == 2

    # 4. Cousins (viulut and huilut share soittimet)
    lca, da, db = graph.find_lowest_common_ancestor("viulut", "huilut")
    assert lca == "soittimet"
    assert da == 2 and db == 2
    assert graph.compute_distance("viulut", "huilut") == 4

    # 5. Unconnected concept
    lca, da, db = graph.find_lowest_common_ancestor("viulut", "kirjat")
    assert lca is None
    assert graph.compute_distance("viulut", "kirjat") is None


def test_path_decay_and_wu_palmer_similarity() -> None:
    """Test path decay and Wu-Palmer similarity calculations."""
    graph = ConceptGraph()
    graph.add_edge("jousisoittimet", "soittimet")
    graph.add_edge("viulut", "jousisoittimet")
    graph.add_edge("alttoviulut", "jousisoittimet")

    # Path decay similarity: decay_factor^distance
    assert graph.compute_path_decay_similarity("viulut", "viulut", decay_factor=0.85) == 1.0
    assert (
        abs(
            graph.compute_path_decay_similarity("viulut", "jousisoittimet", decay_factor=0.85)
            - 0.85
        )
        < 1e-6
    )
    assert (
        abs(
            graph.compute_path_decay_similarity("viulut", "alttoviulut", decay_factor=0.85)
            - (0.85**2)
        )
        < 1e-6
    )
    assert graph.compute_path_decay_similarity("viulut", "unconnected", decay_factor=0.85) == 0.0

    # Wu-Palmer similarity
    wup_identity = graph.compute_wu_palmer_similarity("viulut", "viulut")
    assert wup_identity == 1.0

    wup_parent = graph.compute_wu_palmer_similarity("viulut", "jousisoittimet")
    assert 0.0 < wup_parent < 1.0


def test_hierarchy_service_concept_expansion(tmp_path: Path) -> None:
    """Test expanding concept IDs to include broader terms up to max_depth."""
    store = VocabStore(tmp_path / "vocab.db")
    edges = [
        {"vocabulary": "seko", "concept_id": "s_barokkiviulut", "broader_concept_id": "s_viulut"},
        {"vocabulary": "seko", "concept_id": "s_viulut", "broader_concept_id": "s_jousisoittimet"},
        {
            "vocabulary": "seko",
            "concept_id": "s_jousisoittimet",
            "broader_concept_id": "s_soittimet",
        },
    ]
    store.insert_hierarchy_batch(edges)

    cfg = HierarchyConfig(max_expansion_depth=2)
    service = HierarchyService(store, config=cfg)

    # Expanding s_barokkiviulut up to depth 2 -> includes s_viulut and s_jousisoittimet, but NOT s_soittimet
    expanded = service.expand_concepts("seko", ["s_barokkiviulut"], max_depth=2)
    assert expanded == {"s_barokkiviulut", "s_viulut", "s_jousisoittimet"}

    # Depth 1 expansion
    expanded_1 = service.expand_concepts("seko", ["s_barokkiviulut"], max_depth=1)
    assert expanded_1 == {"s_barokkiviulut", "s_viulut"}


def test_hierarchy_service_concepts_similarity(tmp_path: Path) -> None:
    """Test set similarity evaluation between agent concept footprints."""
    store = VocabStore(tmp_path / "vocab.db")
    edges = [
        {
            "vocabulary": "yso",
            "concept_id": "p_oopperalaulajat",
            "broader_concept_id": "p_laulajat",
        },
        {"vocabulary": "yso", "concept_id": "p_laulajat", "broader_concept_id": "p_muusikot"},
        {"vocabulary": "yso", "concept_id": "p_pianistit", "broader_concept_id": "p_muusikot"},
        {"vocabulary": "slm", "concept_id": "s_ooppera", "broader_concept_id": "s_musiikki"},
    ]
    store.insert_hierarchy_batch(edges)

    cfg = HierarchyConfig(enabled=True, path_decay_factor=0.85, min_similarity_threshold=0.50)
    service = HierarchyService(store, config=cfg)

    # 1. Exact match in sets -> 1.0
    assert (
        service.concepts_similarity({"yso:p_laulajat"}, {"yso:p_laulajat", "yso:p_tohtorit"}) == 1.0
    )

    # 2. Parent-child match (oopperalaulajat <-> laulajat, dist 1 -> 0.85)
    sim = service.concepts_similarity({"yso:p_oopperalaulajat"}, {"yso:p_laulajat"})
    assert abs(sim - 0.85) < 1e-4

    # 3. Sibling match (laulajat <-> pianistit, dist 2 -> 0.7225)
    sim_sib = service.concepts_similarity({"yso:p_laulajat"}, {"yso:p_pianistit"})
    assert abs(sim_sib - 0.7225) < 1e-4

    # 4. Multi-vocabulary isolation: YSO concept must NOT match SLM concept
    assert service.concepts_similarity({"yso:p_oopperalaulajat"}, {"slm:s_ooppera"}) == 0.0


def test_parse_turtle_hierarchy_edges() -> None:
    """Test that SKOS broader and narrower statements in Turtle are extracted into hierarchy edges."""
    sample_ttl = """
    @prefix skos: <http://www.w3.org/2004/02/skos/core#> .
    @prefix yso: <http://www.yso.fi/onto/yso/> .

    yso:p100 a skos:Concept ;
        skos:prefLabel "soittimet"@fi ;
        skos:narrower yso:p200 .

    yso:p200 a skos:Concept ;
        skos:prefLabel "jousisoittimet"@fi ;
        skos:broader yso:p100 ;
        skos:narrower yso:p300 .

    yso:p300 a skos:Concept ;
        skos:prefLabel "viulut"@fi ;
        skos:broader yso:p200 .
    """
    concepts, mappings, edges = parse_turtle_simple(sample_ttl)
    assert len(concepts) == 3

    # Edges should include directed narrower->broader:
    # (p200 -> p100 from p100's narrower and p200's broader)
    # (p300 -> p200 from p200's narrower and p300's broader)
    edge_pairs = {(e["concept_id"], e["broader_concept_id"]) for e in edges}
    assert ("p200", "p100") in edge_pairs
    assert ("p300", "p200") in edge_pairs


def test_store_recursive_cte_broader_and_narrower(tmp_path: Path) -> None:
    """Test SQL recursive CTE queries directly executed in PostgreSQL store."""
    store = VocabStore(tmp_path / "cte_vocab.db")
    edges = [
        {"vocabulary": "yso", "concept_id": "p3", "broader_concept_id": "p2"},
        {"vocabulary": "yso", "concept_id": "p2", "broader_concept_id": "p1"},
        {"vocabulary": "yso", "concept_id": "p1", "broader_concept_id": "p0"},
    ]
    store.insert_hierarchy_batch(edges)

    # 1. Broader CTE from p3
    broader_rows = store.get_broader_concepts("yso", "p3", max_depth=5)
    assert broader_rows == [("p2", 1), ("p1", 2), ("p0", 3)]

    # 2. Narrower CTE from p0
    narrower_rows = store.get_narrower_concepts("yso", "p0", max_depth=5)
    assert narrower_rows == [("p1", 1), ("p2", 2), ("p3", 3)]

    # 3. Max depth limit respected
    broader_limited = store.get_broader_concepts("yso", "p3", max_depth=1)
    assert broader_limited == [("p2", 1)]
