"""Unit tests for FintoFolder legacy thesauri folding rules and modern ontology mapping."""

from pathlib import Path
import pytest

from reconciliator.finto_folding import FintoFolder
from reconciliator.finto_store import FintoStore
from reconciliator.models import FintoConcept, FintoMapping


@pytest.fixture
def populated_finto_store(tmp_path: Path) -> FintoStore:
    store = FintoStore(tmp_path / "finto.db")
    concepts = [
        FintoConcept(
            uri="http://www.yso.fi/onto/yso/p2591",
            vocabulary="yso",
            pref_label_fi="lääkärit",
            pref_label_sv="läkare",
            target_field="650",
        ),
        FintoConcept(
            uri="http://urn.fi/URN:NBN:fi:au:slm:s123",
            vocabulary="slm",
            pref_label_fi="historialliset romaanit",
            pref_label_sv="historiska romaner",
            target_field="655",
        ),
        FintoConcept(
            uri="http://urn.fi/URN:NBN:fi:au:seko:s789",
            vocabulary="seko",
            pref_label_fi="viulu",
            pref_label_sv="fiol",
            target_field="382",
        ),
        FintoConcept(
            uri="http://www.yso.fi/onto/kauno/k123",
            vocabulary="kauno",
            pref_label_fi="satuhahmot",
            target_field="650",
        ),
    ]
    store.insert_concepts(concepts)

    mappings = [
        FintoMapping(
            legacy_uri="http://www.yso.fi/onto/ysa/Y1002",
            legacy_vocab="ysa",
            legacy_label_fi="lääkärit",
            legacy_label_sv=None,
            target_uri="http://www.yso.fi/onto/yso/p2591",
            target_vocab="yso",
            mapping_type="exact_match",
            target_field="650",
        ),
        FintoMapping(
            legacy_uri="http://urn.fi/URN:NBN:fi:au:kaunokki:k99",
            legacy_vocab="kaunokki",
            legacy_label_fi="historialliset romaanit",
            legacy_label_sv=None,
            target_uri="http://urn.fi/URN:NBN:fi:au:slm:s123",
            target_vocab="slm",
            mapping_type="exact_match",
            target_field="655",
        ),
    ]
    store.insert_mappings(mappings)
    return store


def test_fold_ysa_by_uri(populated_finto_store: FintoStore) -> None:
    folder = FintoFolder(populated_finto_store)
    t_field, t_vocab, t_uri, t_label, m_type = folder.fold_subject_heading(
        heading="lääkärit",
        source_vocab="ysa",
        source_field="650",
        source_uri="http://www.yso.fi/onto/ysa/Y1002",
    )

    assert t_field == "650"
    assert t_vocab == "yso"
    assert t_uri == "http://www.yso.fi/onto/yso/p2591"
    assert t_label == "lääkärit"
    assert m_type == "exact_match"


def test_fold_kaunokki_genre_strictly_to_slm(populated_finto_store: FintoStore) -> None:
    folder = FintoFolder(populated_finto_store)
    # Kaunokki term in 655 must fold to SLM, never KAUNO
    t_field, t_vocab, t_uri, t_label, _ = folder.fold_subject_heading(
        heading="historialliset romaanit",
        source_vocab="kaunokki",
        source_field="655",
    )

    assert t_field == "655"
    assert t_vocab == "slm"
    assert t_uri == "http://urn.fi/URN:NBN:fi:au:slm:s123"


def test_fold_musa_to_seko(populated_finto_store: FintoStore) -> None:
    folder = FintoFolder(populated_finto_store)
    t_field, t_vocab, t_uri, t_label, _ = folder.fold_subject_heading(
        heading="viulu",
        source_vocab="musa",
        source_field="382",
    )

    assert t_field == "382"
    assert t_vocab == "seko"
    assert t_uri == "http://urn.fi/URN:NBN:fi:au:seko:s789"

