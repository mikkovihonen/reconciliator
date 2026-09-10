"""Unit tests for FintoStore and SQLite indexed queries."""

from pathlib import Path

import pytest

from reconciliator.finto_store import FintoStore
from reconciliator.models import FintoConcept, FintoMapping


@pytest.fixture
def finto_store(tmp_path: Path) -> FintoStore:
    db_path = tmp_path / "test_finto.db"
    return FintoStore(db_path)


def test_insert_and_query_concepts(finto_store: FintoStore) -> None:
    concept = FintoConcept(
        uri="http://www.yso.fi/onto/yso/p2591",
        vocabulary="yso",
        pref_label_fi="lääkärit",
        pref_label_sv="läkare",
        pref_label_en="physicians",
        alt_labels_fi=["tohtorit"],
        alt_labels_sv=["doktorer"],
        target_field="650",
    )
    finto_store.insert_concepts([concept])

    # Query by URI
    res = finto_store.find_concept_by_uri("http://www.yso.fi/onto/yso/p2591")
    assert res is not None
    assert res.pref_label_fi == "lääkärit"
    assert res.pref_label_sv == "läkare"
    assert "tohtorit" in res.alt_labels_fi

    # Query by Finnish label (case-insensitive)
    by_fi = finto_store.find_concept_by_label("Lääkärit", vocabulary="yso", lang="fi")
    assert by_fi is not None
    assert by_fi.uri == concept.uri

    # Query by Swedish label
    by_sv = finto_store.find_concept_by_label("läkare", lang="sv")
    assert by_sv is not None
    assert by_sv.uri == concept.uri


def test_insert_and_query_mappings(finto_store: FintoStore) -> None:
    mapping = FintoMapping(
        legacy_uri="http://www.yso.fi/onto/ysa/Y1002",
        legacy_vocab="ysa",
        legacy_label_fi="lääkärit",
        legacy_label_sv=None,
        target_uri="http://www.yso.fi/onto/yso/p2591",
        target_vocab="yso",
        mapping_type="exact_match",
        target_field="650",
    )
    finto_store.insert_mappings([mapping])

    # Query by legacy URI
    res_uri = finto_store.find_legacy_mapping_by_uri("http://www.yso.fi/onto/ysa/Y1002")
    assert res_uri is not None
    assert res_uri.target_uri == "http://www.yso.fi/onto/yso/p2591"

    # Query by legacy label
    res_lbl = finto_store.find_legacy_mapping_by_label("lääkärit", legacy_vocab="ysa", lang="fi")
    assert res_lbl is not None
    assert res_lbl.target_vocab == "yso"
