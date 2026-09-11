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
