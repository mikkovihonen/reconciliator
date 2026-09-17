"""Unit and integration test suite for Milestone 4: Intra-Corpus Graph Clusterer.

Tests multi-key blocking, pairwise similarity scoring, Union-Find graph clustering,
expression sub-clustering across 42 WEMI domains, manifestation duplicate grouping,
sammelband composite bindings, and external authority URI hoisting.
"""

import tempfile
from pathlib import Path

import pytest

from reconciliator.clusterer import CorpusClusterer
from reconciliator.matcher import WorkMatcher
from reconciliator.models import (
    ExpressionEntity,
    IdentifierEntity,
    ManifestationEntity,
    ManifestationExpressionAssociation,
    MatchRecord,
    WorkEntity,
)
from reconciliator.repository import WEMIRepository
from reconciliator.utils import UnionFind, extract_identifiers_from_target
from tests.conftest import persist_match_records


@pytest.fixture
def temp_dbs():
    with tempfile.TemporaryDirectory() as tmp_dir:
        auth_db_path = Path(tmp_dir) / "authority.db"
        w_repo = WEMIRepository(auth_db_path)
        yield w_repo, w_repo  # (corpus_repo, wemi_repo) - now the same instance


def test_union_find_data_structure():
    """Verify UnionFind connectivity, path compression, and component grouping."""
    uf = UnionFind(["a", "b", "c", "d", "e"])
    assert uf.find("a") == "a"
    assert uf.find("b") == "b"

    uf.union("a", "b")
    assert uf.find("a") == uf.find("b")

    uf.union("b", "c")
    assert uf.find("a") == uf.find("c")

    uf.union("d", "e")
    assert uf.find("d") == uf.find("e")
    assert uf.find("a") != uf.find("d")

    comps = uf.get_components()
    comp_sets = [set(v) for v in comps.values()]
    assert {"a", "b", "c"} in comp_sets
    assert {"d", "e"} in comp_sets


def test_generate_blocking_keys_multi_family(temp_dbs):
    """Verify extraction of blocking keys across various key families and identifiers."""
    c_repo, _ = temp_dbs
    clusterer = CorpusClusterer(corpus_repo=c_repo)

    target = MatchRecord(
        record_id="t_001",
        format="marcxml",
        work_type="patent",
        title="Menetelmä kuitujen käsittelemiseksi",
        title_stem="menetelma kuitujen kasittelemiseksi",
        work_blocking_key="virtanen::menetelma",
        patent_key="FI123456B",
        standard_key="FI123456",
        raw_payload=b"",
    )

    keys = clusterer.generate_blocking_keys(target)
    assert "work::virtanen::menetelma" in keys
    assert "patent::fi123456b" in keys
    assert "standard::fi123456" in keys
    assert any(k.startswith("id::patent") for k in keys)


def test_pairwise_scoring_identifier_exact_and_fuzzy(temp_dbs):
    """Verify pairwise scoring for exact identifiers, domain keys, and fuzzy metadata."""
    c_repo, _ = temp_dbs
    clusterer = CorpusClusterer(corpus_repo=c_repo)

    r1 = MatchRecord(
        record_id="rec_1",
        format="marcxml",
        work_type="text",
        title="Sinuhe egyptiläinen",
        title_stem="sinuhe egyptilainen",
        author_key="waltari",
        work_blocking_key="waltari::sinuhe",
        year=1945,
        standard_key="978-951-0-12345-6",
        raw_payload=b"",
    )
    r2 = MatchRecord(
        record_id="rec_2",
        format="marcxml",
        work_type="text",
        title="Sinuhe egyptiläinen",
        title_stem="sinuhe egyptilainen",
        author_key="waltari",
        work_blocking_key="waltari::sinuhe",
        year=1946,
        standard_key="978-951-0-12345-6",
        raw_payload=b"",
    )

    d_ids1 = extract_identifiers_from_target(r1)
    d_ids2 = extract_identifiers_from_target(r2)

    # 1. Exact ISBN match
    score, evidence = clusterer.compute_pairwise_score(r1, r2, d_ids1, d_ids2)
    assert score == 1.0
    assert "identifier:isbn" in evidence

    # 2. Fuzzy match without common identifier
    r3 = MatchRecord(
        record_id="rec_3",
        format="marcxml",
        work_type="text",
        title="Sinuhe egyptiläinen: viisitoista kirjaa",
        title_stem="sinuhe egyptilainen viisitoista kirjaa",
        author_key="waltari mika",
        work_blocking_key="waltari::sinuhe",
        year=1950,
        raw_payload=b"",
    )
    d_ids3 = extract_identifiers_from_target(r3)
    score_f, ev_f = clusterer.compute_pairwise_score(r1, r3, d_ids1, d_ids3)
    assert score_f >= 0.85
    assert ev_f == "metadata_similarity"


def test_expression_partition_keys(temp_dbs):
    """Verify extract_expression_key across text, audio, score, and games."""
    c_repo, _ = temp_dbs
    clusterer = CorpusClusterer(corpus_repo=c_repo)

    # Text monograph
    t_text = MatchRecord(
        record_id="t1",
        format="marcxml",
        work_type="text",
        title="Book",
        title_stem="book",
        work_blocking_key="author::book",
        language="swe",
        translators_signature="thomas",
        raw_payload=b"",
    )
    ct, lang, sub = clusterer.extract_expression_key(t_text)
    assert ct == "text"
    assert lang == "swe"
    assert sub == "thomas"

    # Audiobook
    t_audio = MatchRecord(
        record_id="t2",
        format="marcxml",
        work_type="audio_nonmusic",
        title="Book Audio",
        title_stem="book",
        work_blocking_key="author::book",
        language="fin",
        translators_signature="narrator_erik",
        raw_payload=b"",
    )
    ct, lang, sub = clusterer.extract_expression_key(t_audio)
    assert ct == "spoken_word"
    assert lang == "fin"
    assert sub == "narrator_erik"

    # Printed Music Score
    t_score = MatchRecord(
        record_id="t3",
        format="marcxml",
        work_type="music",
        title="Finlandia Score",
        title_stem="finlandia score",
        work_blocking_key="sibelius::finlandia",
        volume_designation="full score",
        raw_payload=b"",
    )
    ct, lang, sub = clusterer.extract_expression_key(t_score)
    assert ct == "notated_music"
    assert sub == "full score"

    # Board Game
    t_bg = MatchRecord(
        record_id="t4",
        format="marcxml",
        work_type="board_game",
        title="Catan",
        title_stem="catan",
        work_blocking_key="teuber::catan",
        volume_designation="anniversary edition",
        raw_payload=b"",
    )
    ct, lang, sub = clusterer.extract_expression_key(t_bg)
    assert ct == "three_dimensional"
    assert sub == "anniversary edition"


def test_cluster_corpus_empty(temp_dbs):
    """Verify cluster_corpus gracefully handles empty repository."""
    c_repo, _ = temp_dbs
    clusterer = CorpusClusterer(corpus_repo=c_repo)
    res = clusterer.cluster_corpus()
    assert res.records_clustered == 0
    assert res.work_clusters_count == 0


def test_cluster_corpus_monograph_reprints_and_translations(temp_dbs):
    """Verify clustering of print original, reprint, and translation under 1 Work."""
    c_repo, _ = temp_dbs
    clusterer = CorpusClusterer(corpus_repo=c_repo)

    r1 = MatchRecord(
        record_id="rec_wsoy_1945",
        format="marcxml",
        work_type="text",
        title="Sinuhe egyptiläinen",
        title_stem="sinuhe egyptilainen",
        author_key="waltari",
        work_blocking_key="waltari::sinuhe",
        language="fin",
        year=1945,
        standard_key="WSOY-1945",
        raw_payload=b"",
    )
    r2 = MatchRecord(
        record_id="rec_wsoy_1960",
        format="marcxml",
        work_type="text",
        title="Sinuhe egyptiläinen",
        title_stem="sinuhe egyptilainen",
        author_key="waltari",
        work_blocking_key="waltari::sinuhe",
        language="fin",
        year=1945,
        standard_key="WSOY-1945",  # Same publication carrier -> manifestation duplicate
        raw_payload=b"",
    )
    r3 = MatchRecord(
        record_id="rec_putnam_1949",
        format="marcxml",
        work_type="text",
        title="The Egyptian",
        title_stem="the egyptian",
        uniform_title="Sinuhe egyptiläinen",
        author_key="waltari",
        work_blocking_key="waltari::sinuhe",
        language="eng",
        translators_signature="walford naomi",
        year=1949,
        standard_key="PUTNAM-1949",
        raw_payload=b"",
    )

    persist_match_records(c_repo, [r1, r2, r3])

    res = clusterer.cluster_corpus()
    assert res.records_clustered == 3
    assert res.work_clusters_count == 1
    assert res.expression_clusters_count == 2  # 1 Finnish, 1 English
    assert res.manifestation_clusters_count == 2  # 1 WSOY-1945 duplicate group, 1 Putnam-1949

    works = c_repo.get_all_work_clusters()
    assert len(works) == 1
    w = works[0]
    assert "sinuhe" in w.canonical_title.lower()

    exps = c_repo.get_expression_clusters_for_work(w.cluster_id)
    assert len(exps) == 2
    languages = {e.language for e in exps}
    assert languages == {"fin", "eng"}

    # Check linkages
    l1 = c_repo.get_corpus_linkage("rec_wsoy_1945")
    l2 = c_repo.get_corpus_linkage("rec_wsoy_1960")
    l3 = c_repo.get_corpus_linkage("rec_putnam_1949")
    assert l1 is not None and l2 is not None and l3 is not None
    assert l1.work_cluster_id == w.cluster_id
    assert l2.work_cluster_id == w.cluster_id
    assert l3.work_cluster_id == w.cluster_id
    # r1 and r2 share manifestation cluster
    assert l1.manifestation_cluster_id == l2.manifestation_cluster_id
    assert l3.manifestation_cluster_id != l1.manifestation_cluster_id


def test_cluster_corpus_cross_format_audiobook_print(temp_dbs):
    """Verify audiobook and print book join same WorkCluster, but distinct ExpressionClusters."""
    c_repo, _ = temp_dbs
    clusterer = CorpusClusterer(corpus_repo=c_repo)

    r_print = MatchRecord(
        record_id="tuntematon_print",
        format="marcxml",
        work_type="text",
        title="Tuntematon sotilas",
        title_stem="tuntematon sotilas",
        author_key="linna vaino",
        work_blocking_key="linna::tuntematon",
        language="fin",
        year=1954,
        raw_payload=b"",
    )
    r_audio = MatchRecord(
        record_id="tuntematon_audio",
        format="marcxml",
        work_type="audio_nonmusic",
        title="Tuntematon sotilas",
        title_stem="tuntematon sotilas",
        author_key="linna vaino",
        work_blocking_key="linna::tuntematon",
        language="fin",
        translators_signature="aho esko",  # Narrator
        year=1985,
        raw_payload=b"",
    )

    persist_match_records(c_repo, [r_print, r_audio])

    res = clusterer.cluster_corpus()
    assert res.records_clustered == 2
    assert res.work_clusters_count == 1
    assert res.expression_clusters_count == 2

    w = c_repo.get_all_work_clusters()[0]
    exps = c_repo.get_expression_clusters_for_work(w.cluster_id)
    content_types = {e.content_type for e in exps}
    assert content_types == {"text", "spoken_word"}


def test_cluster_corpus_notated_music_and_recorded_sound(temp_dbs):
    """Verify notated score and sound recording cluster under the same Work, distinct Expressions."""
    c_repo, _ = temp_dbs
    clusterer = CorpusClusterer(corpus_repo=c_repo)

    r_score = MatchRecord(
        record_id="finlandia_score",
        format="marcxml",
        work_type="music",
        title="Finlandia, op. 26: Partituuri",
        title_stem="finlandia op 26 partituuri",
        uniform_title="Finlandia, op. 26",
        author_key="sibelius jean",
        work_blocking_key="sibelius::finlandia",
        music_work_key="sibelius::finlandia::op26",
        volume_designation="partituuri",
        raw_payload=b"",
    )
    r_audio = MatchRecord(
        record_id="finlandia_cd",
        format="marcxml",
        work_type="music",
        title="Finlandia: Orchestral Works",
        title_stem="finlandia op 26",
        uniform_title="Finlandia, op. 26",
        author_key="sibelius jean",
        work_blocking_key="sibelius::finlandia",
        music_work_key="sibelius::finlandia::op26",
        year=1984,
        raw_payload=b"",
    )

    persist_match_records(c_repo, [r_score, r_audio])

    res = clusterer.cluster_corpus()
    assert res.records_clustered == 2
    assert res.work_clusters_count == 1
    assert res.expression_clusters_count == 2

    w = c_repo.get_all_work_clusters()[0]
    exps = c_repo.get_expression_clusters_for_work(w.cluster_id)
    content_types = {e.content_type for e in exps}
    assert content_types == {"notated_music", "audio_music"}


def test_cluster_corpus_sammelband_bound_with(temp_dbs):
    """Verify sammelband items form separate Works and Expressions but track composite binding."""
    c_repo, _ = temp_dbs
    clusterer = CorpusClusterer(corpus_repo=c_repo)

    r1 = MatchRecord(
        record_id="agricola_item_1",
        format="marcxml",
        work_type="early_print",
        title="Rucouskiria Bibliasta",
        title_stem="rucouskiria bibliasta",
        author_key="agricola michael",
        work_blocking_key="agricola::rucouskiria",
        sammelband_item_id="BINDING_VAULT_042",
        year=1544,
        raw_payload=b"",
    )
    r2 = MatchRecord(
        record_id="agricola_item_2",
        format="marcxml",
        work_type="early_print",
        title="Se Wsi Testamenti",
        title_stem="se wsi testamenti",
        author_key="agricola michael",
        work_blocking_key="agricola::sewsitestamenti",
        sammelband_item_id="BINDING_VAULT_042",
        year=1548,
        raw_payload=b"",
    )

    persist_match_records(c_repo, [r1, r2])

    res = clusterer.cluster_corpus()
    assert res.records_clustered == 2
    assert res.work_clusters_count == 2
    assert res.expression_clusters_count == 2

    bindings = c_repo.get_sammelband_bindings("BINDING_VAULT_042")
    assert len(bindings) == 2
    assert bindings[0].record_id == "agricola_item_1"
    assert bindings[0].sequence_in_binding == 1
    assert bindings[1].record_id == "agricola_item_2"
    assert bindings[1].sequence_in_binding == 2


def test_cluster_corpus_external_uri_hoisting(temp_dbs):
    """Verify clusterer hoists canonical external authority URI when WorkMatcher matches a record."""
    c_repo, w_repo = temp_dbs

    # Authority database setup. The source has to be registered: corpus and
    # authority share one database now, so "which rows may be matched against"
    # is answered by the source registry, not by which file a row came from.
    w_repo.register_source("melinda", is_authoritative=True)
    auth_work = WorkEntity(
        record_origin="melinda",
        source="melinda",
        source_id="kivi_veljekset",
        title="Seitsemän veljestä",
        title_normalized="seitseman veljesta",
        author_key="kivi aleksis",
        work_type="text",
    )
    w_id = w_repo.insert_works_batch([auth_work])[0]

    auth_expr = ExpressionEntity(
        record_origin="melinda",
        work_id=w_id,
        source="melinda",
        source_id="veljekset_fin",
        language="fin",
        content_type="text",
        title="Seitsemän veljestä",
    )
    e_id = w_repo.insert_expressions_batch([auth_expr])[0]

    auth_manif = ManifestationEntity(
        record_origin="melinda",
        source="melinda",
        source_id="veljekset_m1",
        work_id=w_id,
        title="Seitsemän veljestä",
        title_normalized="seitseman veljesta",
    )
    m_id = w_repo.insert_manifestations_batch([auth_manif])[0]

    w_repo.insert_identifiers_batch(
        [
            IdentifierEntity(
                entity_type="manifestation",
                entity_id=m_id,
                scheme="isbn",
                value="978-951-0-99999-9",
                value_normalized="9789510999999",
            )
        ]
    )
    w_repo.insert_manifestation_expressions_batch(
        [ManifestationExpressionAssociation(manifestation_id=m_id, expression_id=e_id)]
    )

    # Corpus targets: 1 has ISBN matching authority, 1 is reprint without ISBN
    r1 = MatchRecord(
        record_id="kivi_ed1",
        format="marcxml",
        work_type="text",
        title="Seitsemän veljestä",
        title_stem="seitseman veljesta",
        author_key="kivi aleksis",
        work_blocking_key="kivi::seitsemanveljesta",
        language="fin",
        standard_key="978-951-0-99999-9",
        raw_payload=b"",
    )
    r2 = MatchRecord(
        record_id="kivi_ed2",
        format="marcxml",
        work_type="text",
        title="Seitsemän veljestä: romaani",
        title_stem="seitseman veljesta romaani",
        author_key="kivi aleksis",
        work_blocking_key="kivi::seitsemanveljesta",
        language="fin",
        raw_payload=b"",
    )

    persist_match_records(c_repo, [r1, r2])

    matcher = WorkMatcher(wemi_repo=w_repo)
    clusterer = CorpusClusterer(corpus_repo=c_repo, wemi_repo=w_repo, matcher=matcher)

    res = clusterer.cluster_corpus()
    assert res.records_clustered == 2
    assert res.work_clusters_count == 1

    w = c_repo.get_all_work_clusters()[0]
    # Verify hoisted external CURIE!
    assert w.canonical_work_curie == "melinda:kivi_veljekset"

    # Verify hoisted expression CURIE!
    exps = c_repo.get_expression_clusters_for_work(w.cluster_id)
    assert len(exps) == 1
    assert exps[0].canonical_expression_curie == "melinda:veljekset_fin"


def test_cluster_corpus_specialized_domains(temp_dbs):
    """Verify domain key clustering for patents, software SWHID, and board games."""
    c_repo, _ = temp_dbs
    clusterer = CorpusClusterer(corpus_repo=c_repo)

    # 2 Patent records with same patent_key
    p1 = MatchRecord(
        record_id="pat_1",
        format="marcxml",
        work_type="patent",
        title="Keksintö A",
        title_stem="keksinto a",
        work_blocking_key="patent::fi123",
        patent_key="FI123456B",
        raw_payload=b"",
    )
    p2 = MatchRecord(
        record_id="pat_2",
        format="marcxml",
        work_type="patent",
        title="Menetelmä A",
        title_stem="menetelma a",
        work_blocking_key="patent::fi123",
        patent_key="FI123456B",
        raw_payload=b"",
    )

    # 2 Software records with same SWHID
    s1 = MatchRecord(
        record_id="sw_1",
        format="marcxml",
        work_type="software",
        title="Reconciliator CLI",
        title_stem="reconciliator cli",
        work_blocking_key="sw::rec",
        swhid_key="swh:1:dir:abc123def456",
        raw_payload=b"",
    )
    s2 = MatchRecord(
        record_id="sw_2",
        format="marcxml",
        work_type="software",
        title="Reconciliator Engine",
        title_stem="reconciliator engine",
        work_blocking_key="sw::rec",
        swhid_key="swh:1:dir:abc123def456",
        raw_payload=b"",
    )

    # 2 Board games with same board_game_key
    bg1 = MatchRecord(
        record_id="bg_1",
        format="marcxml",
        work_type="board_game",
        title="Afrikan tähti",
        title_stem="afrikan tahti",
        work_blocking_key="bg::afrikantahti",
        board_game_key="bgg:1234",
        raw_payload=b"",
    )
    bg2 = MatchRecord(
        record_id="bg_2",
        format="marcxml",
        work_type="board_game",
        title="Den afrikanska stjärnan",
        title_stem="den afrikanska stjarnan",
        work_blocking_key="bg::afrikantahti",
        board_game_key="bgg:1234",
        raw_payload=b"",
    )

    persist_match_records(c_repo, [p1, p2, s1, s2, bg1, bg2])

    res = clusterer.cluster_corpus()
    assert res.records_clustered == 6
    assert res.work_clusters_count == 3  # 1 Patent, 1 Software, 1 Board Game

    work_types = {w.work_type for w in c_repo.get_all_work_clusters()}
    assert work_types == {"patent", "software", "board_game"}
