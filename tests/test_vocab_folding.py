"""Unit tests for VocabFolder legacy thesauri folding rules and modern ontology mapping."""

from pathlib import Path

import pytest

from reconciliator.models import ConceptMapping, VocabConcept
from reconciliator.vocab_folding import VocabFolder
from reconciliator.vocab_store import VocabStore


@pytest.fixture
def populated_vocab_store(tmp_path: Path) -> VocabStore:
    store = VocabStore(tmp_path / "vocab.db")
    concepts = [
        VocabConcept(
            uri="http://www.yso.fi/onto/yso/p2591",
            vocabulary="yso",
            pref_label_fi="lääkärit",
            pref_label_sv="läkare",
            target_field="650",
        ),
        VocabConcept(
            uri="http://urn.fi/URN:NBN:fi:au:slm:s123",
            vocabulary="slm",
            pref_label_fi="historialliset romaanit",
            pref_label_sv="historiska romaner",
            target_field="655",
        ),
        VocabConcept(
            uri="http://urn.fi/URN:NBN:fi:au:seko:s789",
            vocabulary="seko",
            pref_label_fi="viulu",
            pref_label_sv="fiol",
            target_field="382",
        ),
        VocabConcept(
            uri="http://www.yso.fi/onto/kauno/k123",
            vocabulary="kauno",
            pref_label_fi="satuhahmot",
            target_field="650",
        ),
    ]
    store.insert_concepts(concepts)

    mappings = [
        ConceptMapping(
            legacy_uri="http://www.yso.fi/onto/ysa/Y1002",
            legacy_vocab="ysa",
            legacy_label_fi="lääkärit",
            legacy_label_sv=None,
            target_uri="http://www.yso.fi/onto/yso/p2591",
            target_vocab="yso",
            mapping_type="exact_match",
            target_field="650",
        ),
        ConceptMapping(
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


def test_fold_ysa_by_uri(populated_vocab_store: VocabStore) -> None:
    folder = VocabFolder(populated_vocab_store)
    t_field, t_vocab, t_uri, t_label, m_type, qid = folder.fold_subject_heading(
        heading="lääkärit",
        source_vocab="ysa",
        source_field="650",
        source_uri="http://www.yso.fi/onto/ysa/Y1002",
    )

    assert t_field == "650"
    assert t_vocab == "yso/fin"
    assert t_uri == "http://www.yso.fi/onto/yso/p2591"
    assert t_label == "lääkärit"
    assert m_type == "exact_match"
    assert qid is None


def test_fold_kaunokki_genre_strictly_to_slm(populated_vocab_store: VocabStore) -> None:
    folder = VocabFolder(populated_vocab_store)
    # Kaunokki term in 655 must fold to SLM, never KAUNO
    t_field, t_vocab, t_uri, t_label, _, _ = folder.fold_subject_heading(
        heading="historialliset romaanit",
        source_vocab="kaunokki",
        source_field="655",
    )

    assert t_field == "655"
    assert t_vocab == "slm/fin"
    assert t_uri == "http://urn.fi/URN:NBN:fi:au:slm:s123"


def test_fold_musa_to_seko(populated_vocab_store: VocabStore) -> None:
    folder = VocabFolder(populated_vocab_store)
    t_field, t_vocab, t_uri, t_label, _, _ = folder.fold_subject_heading(
        heading="viulu",
        source_vocab="musa",
        source_field="382",
    )

    assert t_field == "382"
    assert t_vocab == "seko"
    assert t_uri == "http://urn.fi/URN:NBN:fi:au:seko:s789"


def test_fold_sample_legacy_thesauri_fixture(populated_vocab_store: VocabStore) -> None:
    from lxml import etree

    from reconciliator.loaders.marcxml import parse_marc_record_elem
    from reconciliator.normalizers.record import RecordNormalizer

    fixture_path = Path(__file__).parent / "fixtures" / "sample_legacy_thesauri.xml"
    assert fixture_path.exists()

    tree = etree.parse(str(fixture_path))
    root = tree.getroot()
    records = root.findall(".//record") or root.findall(".//{http://www.loc.gov/MARC21/slim}record")
    assert len(records) == 2

    folder = VocabFolder(populated_vocab_store)
    normalizer = RecordNormalizer(vocab_folder=folder)

    # Record 1: has ysa lääkärit and kaunokki historialliset romaanit
    raw1 = parse_marc_record_elem(records[0], source="target")
    norm1 = normalizer.normalize(raw1)
    topical_uris = [s.uri for s in norm1.subjects.topical]
    assert "http://www.yso.fi/onto/yso/p2591" in topical_uris
    genre_uris = [s.uri for s in norm1.subjects.genre_form]
    assert "http://urn.fi/URN:NBN:fi:au:slm:s123" in genre_uris


def test_fold_with_language_suffix(populated_vocab_store: VocabStore) -> None:
    folder = VocabFolder(populated_vocab_store)
    # ysa/fin must normalize to ysa and fold to yso
    t_field, t_vocab, t_uri, t_label, m_type, _ = folder.fold_subject_heading(
        heading="lääkärit",
        source_vocab="ysa/fin",
        source_field="650",
    )
    assert t_field == "650"
    assert t_vocab == "yso/fin"
    assert t_uri == "http://www.yso.fi/onto/yso/p2591"


def test_fold_existing_canonical_uri(populated_vocab_store: VocabStore) -> None:
    folder = VocabFolder(populated_vocab_store)
    # When record already has a canonical YSO URI, return it directly
    t_field, t_vocab, t_uri, t_label, m_type, _ = folder.fold_subject_heading(
        heading="lääkärit",
        source_vocab="yso/fin",
        source_field="650",
        source_uri="http://www.yso.fi/onto/yso/p2591",
    )
    assert t_field == "650"
    assert t_vocab == "yso/fin"
    assert t_uri == "http://www.yso.fi/onto/yso/p2591"
    assert m_type == "existing_canonical"
