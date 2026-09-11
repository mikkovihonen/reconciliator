"""Unit tests for VocabStore and SQLite indexed queries."""

from pathlib import Path

import pytest

from reconciliator.models import ConceptMapping, VocabConcept
from reconciliator.vocab_store import VocabStore


@pytest.fixture
def vocab_store(tmp_path: Path) -> VocabStore:
    db_path = tmp_path / "test_vocab.db"
    return VocabStore(db_path)


def test_insert_and_query_concepts(vocab_store: VocabStore) -> None:
    concept = VocabConcept(
        uri="http://www.yso.fi/onto/yso/p2591",
        vocabulary="yso",
        pref_label_fi="lääkärit",
        pref_label_sv="läkare",
        pref_label_en="physicians",
        alt_labels_fi=["tohtorit"],
        alt_labels_sv=["doktorer"],
        target_field="650",
    )
    vocab_store.insert_concepts([concept])

    # Query by URI
    res = vocab_store.find_concept_by_uri("http://www.yso.fi/onto/yso/p2591")
    assert res is not None
    assert res.pref_label_fi == "lääkärit"
    assert res.pref_label_sv == "läkare"
    assert "tohtorit" in res.alt_labels_fi

    # Query by Finnish label (case-insensitive)
    by_fi = vocab_store.find_concept_by_label("Lääkärit", vocabulary="yso", lang="fi")
    assert by_fi is not None
    assert by_fi.uri == concept.uri

    # Query by Swedish label
    by_sv = vocab_store.find_concept_by_label("läkare", lang="sv")
    assert by_sv is not None
    assert by_sv.uri == concept.uri


def test_insert_and_query_mappings(vocab_store: VocabStore) -> None:
    mapping = ConceptMapping(
        legacy_uri="http://www.yso.fi/onto/ysa/Y1002",
        legacy_vocab="ysa",
        legacy_label_fi="lääkärit",
        legacy_label_sv=None,
        target_uri="http://www.yso.fi/onto/yso/p2591",
        target_vocab="yso",
        mapping_type="exact_match",
        target_field="650",
    )
    vocab_store.insert_mappings([mapping])

    # Query by legacy URI
    res_uri = vocab_store.find_legacy_mapping_by_uri("http://www.yso.fi/onto/ysa/Y1002")
    assert res_uri is not None
    assert res_uri.target_uri == "http://www.yso.fi/onto/yso/p2591"

    # Query by legacy label
    res_lbl = vocab_store.find_legacy_mapping_by_label("lääkärit", legacy_vocab="ysa", lang="fi")
    assert res_lbl is not None
    assert res_lbl.target_vocab == "yso"


def test_vocab_store_caching_and_connection_reuse(vocab_store: VocabStore) -> None:
    conn1 = vocab_store.get_connection()
    conn2 = vocab_store.get_connection()
    assert conn1 is conn2, "Expected connection to be reused on same thread"

    concept = VocabConcept(
        uri="http://www.yso.fi/onto/yso/p1234",
        vocabulary="yso",
        pref_label_fi="testi",
        pref_label_sv="test",
        pref_label_en="test",
        alt_labels_fi=[],
        alt_labels_sv=[],
        target_field="650",
    )
    vocab_store.insert_concepts([concept])

    # First lookup: cache miss
    info_before = vocab_store.find_concept_by_uri.cache_info()
    res1 = vocab_store.find_concept_by_uri("http://www.yso.fi/onto/yso/p1234")
    assert res1 is not None

    # Second lookup: cache hit
    res2 = vocab_store.find_concept_by_uri("http://www.yso.fi/onto/yso/p1234")
    assert res2 is not None
    info_after = vocab_store.find_concept_by_uri.cache_info()
    assert info_after.hits == info_before.hits + 1

    # Invalidate cache via insert
    vocab_store.clear_caches()
    info_cleared = vocab_store.find_concept_by_uri.cache_info()
    assert info_cleared.currsize == 0

    vocab_store.close()
