"""Verification tests for native Rust reconciliator._core extension module."""

import pytest
from reconciliator import _core


def test_rust_ping():
    assert hasattr(_core, "rust_ping")
    assert _core.rust_ping() == "pong from rust"


def test_rust_fold_diacritics():
    norm = _core.normalizers
    assert norm.py_fold_diacritics("Åland") == "Åland"
    assert norm.py_fold_diacritics("Helsingfors och Åbo") == "Helsingfors och Åbo"
    assert norm.py_fold_diacritics("Äänekoski") == "Äänekoski"
    assert norm.py_fold_diacritics("Örebro") == "Örebro"
    assert norm.py_fold_diacritics("Hämeenlinna") == "Hämeenlinna"

    # Foreign diacritics folded
    assert norm.py_fold_diacritics("München") == "Munchen"
    assert norm.py_fold_diacritics("Curaçao") == "Curacao"
    assert norm.py_fold_diacritics("Saint-Barthélemy") == "Saint-Barthelemy"
    assert norm.py_fold_diacritics("São Paulo") == "Sao Paulo"
    assert norm.py_fold_diacritics("Göran & François") == "Göran & Francois"
    assert norm.py_fold_diacritics("Müller") == "Muller"
    assert norm.py_fold_diacritics("Café") == "Cafe"

    # Custom preserved diacritics
    assert norm.py_fold_diacritics("Müller", "üÜ") == "Müller"
    assert norm.py_fold_diacritics("Hämeenlinna", "üÜ") == "Hameenlinna"


def test_rust_isbn_and_alphanumeric():
    norm = _core.normalizers
    assert norm.py_clean_alphanumeric("978-951-0-00001-0") == "9789510000010"
    assert norm.py_isbn_10_to_13("0306406152") == "9780306406157"
    assert norm.py_isbn_10_to_13("0-306-40615-2") == "9780306406157"
    assert norm.py_isbn_10_to_13("invalid") is None
    assert norm.py_validate_isbn_13("9780306406157") is True
    assert norm.py_validate_isbn_13("9780306406158") is False


def test_rust_blocking_keys():
    norm = _core.normalizers
    h = norm.py_deterministic_key_hash("test-key")
    assert len(h) == 32
    assert norm.py_deterministic_key_hash("test-key") == h

    work_key = norm.py_compute_work_blocking_key("Waltari", "Sinuhe egyptilainen")
    assert len(work_key) == 32

    host_key = norm.py_compute_host_citation_key("0028-0836", "2020", "105", "Smith")
    assert host_key == "00280836::2020::105::smith"


def test_rust_clustering_lcs_and_jaccard():
    clust = _core.clustering
    assert clust.py_lcs_length([], []) == 0
    assert clust.py_lcs_length(["a", "b", "c"], ["a", "b", "c"]) == 3
    assert clust.py_lcs_length(["a", "b", "c"], ["x", "y", "z"]) == 0
    assert clust.py_lcs_length(["a", "b", "c", "d"], ["a", "c", "d"]) == 3
    assert clust.py_lcs_length(["a", "b", "c", "d"], ["d", "c", "b", "a"]) == 1

    c1 = ["t1", "t2", "t3", "t4"]
    c2 = ["t1", "t2", "t3", "t4", "t5"]
    assert abs(clust.py_jaccard_similarity(c1, c2) - 0.8) < 1e-6


def test_rust_clustering_aggregate_overlap():
    clust = _core.clustering
    c1 = ["cw_1", "cw_2", "cw_3", "cw_4"]
    c2 = ["cw_1", "cw_2", "cw_3", "cw_4", "cw_5"]

    # Jaccard merge bonus (0.80 overlap)
    res = clust.py_evaluate_aggregate_overlap(c1, c2, sequence_alignment=False)
    assert res is not None
    score, ev = res
    assert score == 0.95
    assert ev == "aggregate_jaccard_merge"

    # Anti-collision
    res_col = clust.py_evaluate_aggregate_overlap(c1, ["cw_1"], is_component2=True)
    assert res_col == (0.0, "container_part_collision")


def test_rust_connected_components():
    clust = _core.clustering
    # 5 nodes: 0-1, 1-2, 3-4
    edges = [(0, 1), (1, 2), (3, 4)]
    components = clust.py_cluster_connected_components(5, edges)
    # Expect 2 components: {0, 1, 2} and {3, 4}
    comp_sizes = sorted(len(v) for v in components.values())
    assert comp_sizes == [2, 3]


def test_cluster_edges_helper():
    from reconciliator.utils.union_find import cluster_edges

    nodes = ["node_a", "node_b", "node_c", "node_d"]
    edges = [("node_a", "node_b"), ("node_c", "node_d")]
    comps = cluster_edges(nodes, edges)
    assert len(comps) == 2
    comp_sets = [set(v) for v in comps.values()]
    assert {"node_a", "node_b"} in comp_sets
    assert {"node_c", "node_d"} in comp_sets


def test_rust_marcxml_parser():
    xml = """<record xmlns="http://www.loc.gov/MARC21/slim">
  <leader>01234nam a2200349 c 4500</leader>
  <controlfield tag="001">123456</controlfield>
  <controlfield tag="003">FI-MELI</controlfield>
  <datafield tag="100" ind1="1" ind2=" ">
    <subfield code="a">Virtanen, Ville,</subfield>
    <subfield code="d">1950-</subfield>
  </datafield>
  <datafield tag="245" ind1="1" ind2="0">
    <subfield code="a">Testikirja :</subfield>
    <subfield code="b">alaotsikko /</subfield>
  </datafield>
</record>"""

    records = _core.parsers.py_parse_marc_xml_str(xml)
    assert len(records) == 1
    r = records[0]
    assert r.leader == "01234nam a2200349 c 4500"
    assert r.get_controlfield("001") == "123456"
    assert r.get_controlfield("003") == "FI-MELI"
    assert r.get_controlfield("999") is None

    assert r.get_subfield("100", "a") == "Virtanen, Ville,"
    assert r.get_subfield("100", "d") == "1950-"
    assert r.get_subfield("245", "a") == "Testikirja :"
    assert r.get_subfield("245", "b") == "alaotsikko /"
    assert r.get_subfields("245", "a") == ["Testikirja :"]

    dfs = r.get_datafields("245")
    assert len(dfs) == 1
    assert dfs[0].ind1 == "1"
    assert dfs[0].ind2 == "0"


def test_rust_label_folding():
    norm = _core.normalizers
    assert norm.py_strip_biographical_dates("Waltari, Mika, 1908-1979") == "Waltari, Mika"
    assert norm.py_strip_biographical_dates("Fleming, Ian (1908-1964)") == "Fleming, Ian"
    assert (
        norm.py_strip_biographical_dates("Bond, James (fiktiivinen hahmo)")
        == "Bond, James (fiktiivinen hahmo)"
    )

    assert norm.py_fold_label("Waltari, Mika, 1908-1979") == "waltari, mika"
    assert norm.py_fold_label("Müller (1900-1975)", "üÜ") == "müller"


def test_fast_marcxml_loader():
    from reconciliator.loaders.marcxml import parse_marc_xml_str_fast

    xml = """<record xmlns="http://www.loc.gov/MARC21/slim">
  <controlfield tag="001">999999</controlfield>
  <datafield tag="245" ind1="0" ind2="0"><subfield code="a">Fast Parser Test</subfield></datafield>
</record>"""
    recs = parse_marc_xml_str_fast(xml)
    assert len(recs) == 1
    assert recs[0].get_controlfield("001") == "999999"
    assert recs[0].get_subfield("245", "a") == "Fast Parser Test"


def test_rust_parse_turtle_content():
    ttl = """
@prefix skos: <http://www.w3.org/2004/02/skos/core#> .
@prefix yso: <http://www.yso.fi/onto/yso/> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .

yso:p100 a skos:Concept ;
    skos:prefLabel "soittimet"@fi, "instruments"@en ;
    skos:altLabel "soittopelit"@fi ;
    skos:narrower yso:p200 ;
    owl:deprecated false .

yso:p200 a skos:Concept ;
    skos:prefLabel "jousisoittimet"@fi ;
    skos:broader yso:p100 ;
    owl:deprecated true .
"""
    concepts, mappings, edges = _core.parsers.py_parse_turtle_content(ttl, default_vocab="yso")
    assert len(concepts) == 2
    c100 = next(c for c in concepts if c.concept_id == "p100")
    assert c100.vocabulary == "yso"
    assert c100.pref_labels["fi"] == "soittimet"
    assert c100.pref_labels["en"] == "instruments"
    assert c100.alt_labels["fi"] == ["soittopelit"]
    assert c100.is_deprecated is False

    c200 = next(c for c in concepts if c.concept_id == "p200")
    assert c200.is_deprecated is True

    assert len(edges) == 2
    edge_pairs = {(e.concept_id, e.broader_concept_id) for e in edges}
    assert ("p200", "p100") in edge_pairs


def test_rust_parse_turtle_file():
    from pathlib import Path
    ttl_path = Path("tests/fixtures/sample_finto.ttl")
    if ttl_path.is_file():
        concepts, mappings, edges = _core.parsers.py_parse_turtle_file(str(ttl_path))
        assert len(concepts) >= 5
        assert len(mappings) >= 3


