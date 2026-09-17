"""Unit tests for VocabFolder legacy thesauri folding rules and modern ontology mapping."""

from pathlib import Path

import pytest

from reconciliator.config import VocabNormalizationConfig
from reconciliator.models import ConceptMapping, VocabConcept
from reconciliator.vocab_folding import VocabFolder, format_target_vocab
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


def test_format_target_vocab_custom_config() -> None:
    custom_cfg = VocabNormalizationConfig(
        language_subcode_vocabularies={"custom_onto": "fin"},
        thesaurus_languages={"sv_thes": "sv"},
    )
    # Target vocab in configured language_subcode_vocabularies gets language subcode
    assert format_target_vocab("custom_onto", "sv_thes", config=custom_cfg) == "custom_onto/swe"
    assert (
        format_target_vocab("custom_onto", "unknown_thes", config=custom_cfg) == "custom_onto/fin"
    )

    # Default yso is NOT forced if explicitly omitted from language_subcode_vocabularies
    assert format_target_vocab("yso", "unknown_thes", config=custom_cfg) == "yso"


def test_vocab_folding_fully_custom_configuration(tmp_path: Path) -> None:
    store = VocabStore(tmp_path / "custom_vocab.db")
    custom_cfg = VocabNormalizationConfig(
        legacy_to_modern={"old_thes": "new_ont", "old_genre_thes": "genre_ont"},
        vocab_target_fields={"genre_ont": "655", "perf_ont": "382"},
        default_field_vocabularies={
            "650": "default_topical",
            "655": "genre_ont",
            "382": "perf_ont",
        },
        field_to_vocabularies={
            "650": ["default_topical", "new_ont"],
            "655": ["genre_ont"],
            "382": ["perf_ont"],
        },
        language_subcode_vocabularies={"new_ont": "fin", "genre_ont": "fin"},
    )

    concepts = [
        VocabConcept(
            uri="http://example.org/new/1",
            vocabulary="new_ont",
            pref_label_fi="Mukautettu käsite",
            target_field="650",
        ),
        VocabConcept(
            uri="http://example.org/genre/2",
            vocabulary="genre_ont",
            pref_label_fi="Mukautettu lajityyppi",
            target_field=None,  # No explicit target_field, should be inferred from vocab_target_fields
        ),
    ]
    store.insert_concepts(concepts)

    mappings = [
        ConceptMapping(
            legacy_uri="http://example.org/old/1",
            legacy_vocab="old_thes",
            legacy_label_fi="Mukautettu käsite",
            legacy_label_sv=None,
            target_uri="http://example.org/new/1",
            target_vocab="new_ont",
            mapping_type="exact_match",
            target_field="650",
        ),
    ]
    store.insert_mappings(mappings)
    store.rebuild_aliases_from_concepts()

    folder = VocabFolder(store, config=custom_cfg)

    # 1. Fold by legacy URI
    t_field, t_vocab, t_uri, t_label, m_type, _ = folder.fold_subject_heading(
        heading="Mukautettu käsite",
        source_vocab="old_thes",
        source_field="650",
        source_uri="http://example.org/old/1",
    )
    assert t_field == "650"
    assert t_vocab == "new_ont/fin"
    assert t_uri == "http://example.org/new/1"
    assert m_type == "exact_match"

    # 2. Concept lookup where vocab_target_fields redirects genre_ont from 650 to 655
    t_field, t_vocab, t_uri, t_label, m_type, _ = folder.fold_subject_heading(
        heading="Mukautettu lajityyppi",
        source_vocab="old_genre_thes",
        source_field="650",
    )
    assert t_field == "655"
    assert t_vocab == "genre_ont/fin"
    assert t_uri == "http://example.org/genre/2"

    # 3. Unmapped fallback with no store: uses custom default_topical, NOT hardcoded yso
    folder_no_store = VocabFolder(config=custom_cfg)
    t_field, t_vocab, t_uri, t_label, m_type, _ = folder_no_store.fold_subject_heading(
        heading="Tuntematon",
        source_vocab="unregistered_legacy",
        source_field="650",
    )
    assert t_field == "650"
    assert t_vocab == "default_topical"
    assert m_type == "none"

    # 4. Unmapped fallback in 655 enforces field default genre_ont
    t_field, t_vocab, t_uri, t_label, m_type, _ = folder_no_store.fold_subject_heading(
        heading="Tuntematon laji",
        source_vocab="old_thes",  # old_thes maps to new_ont, but new_ont is prohibited in 655
        source_field="655",
    )
    assert t_field == "655"
    assert t_vocab == "genre_ont/fin"


def test_vocab_store_rebuild_aliases_dynamic_legacy(tmp_path: Path) -> None:
    store = VocabStore(tmp_path / "alias_test.db")
    concepts = [
        VocabConcept(
            uri="http://example.org/dyn_legacy/1",
            vocabulary="dyn_legacy",
            pref_label_fi="Vanha termi",
        ),
        VocabConcept(
            uri="http://example.org/dyn_modern/1",
            vocabulary="dyn_modern",
            pref_label_fi="Uusi termi",
        ),
    ]
    store.insert_concepts(concepts)

    # Rebuild aliases filtering out dyn_legacy
    count = store.rebuild_aliases_from_concepts(legacy_vocabs={"dyn_legacy"})
    assert count == 1

    with store.get_connection() as conn:
        rows = conn.exec_driver_sql("SELECT * FROM concept_aliases;").mappings().fetchall()
        assert len(rows) == 1
        assert rows[0]["vocabulary"] == "dyn_modern"
        assert rows[0]["concept_id"] in ("1", "http://example.org/dyn_modern/1")


def test_language_subcode_suffixes_configuration() -> None:
    # Default config supports fi/fin -> fin, sv/swe -> swe, en/eng -> eng
    assert format_target_vocab("yso", lang="en") == "yso/eng"
    assert format_target_vocab("yso", lang="eng") == "yso/eng"
    assert format_target_vocab("yso", lang="sv") == "yso/swe"
    assert format_target_vocab("yso", lang="swe") == "yso/swe"
    assert format_target_vocab("yso", lang="fi") == "yso/fin"
    assert format_target_vocab("yso", lang="fin") == "yso/fin"

    # Custom language_subcode_suffixes
    cfg = VocabNormalizationConfig(
        language_subcode_suffixes={"de": "ger", "deu": "ger"},
    )
    assert format_target_vocab("yso", lang="de", config=cfg) == "yso/ger"
    assert format_target_vocab("yso", lang="deu", config=cfg) == "yso/ger"


def test_concept_preferred_label_selection_multilingual(tmp_path: Path) -> None:
    store = VocabStore(tmp_path / "multilingual_vocab.db")
    concept = VocabConcept(
        uri="http://www.yso.fi/onto/yso/p2591",
        vocabulary="yso",
        pref_label_fi="lääkärit",
        pref_label_sv="läkare",
        pref_label_en="physicians",
        target_field="650",
    )
    store.insert_concepts([concept])

    folder = VocabFolder(store)

    # 1. English record
    t_field, t_vocab, t_uri, t_label, _, _ = folder.fold_subject_heading(
        heading="physicians",
        source_vocab="yso",
        source_field="650",
        source_uri="http://www.yso.fi/onto/yso/p2591",
        lang="en",
    )
    assert t_label == "physicians"
    assert t_vocab == "yso/eng"

    # 2. English record with 3-letter MARC code
    t_field, t_vocab, t_uri, t_label, _, _ = folder.fold_subject_heading(
        heading="physicians",
        source_vocab="yso",
        source_field="650",
        lang="eng",
    )
    assert t_label == "physicians"
    assert t_vocab == "yso/eng"

    # 3. Swedish record
    t_field, t_vocab, t_uri, t_label, _, _ = folder.fold_subject_heading(
        heading="läkare",
        source_vocab="yso",
        source_field="650",
        lang="swe",
    )
    assert t_label == "läkare"
    assert t_vocab == "yso/swe"


def test_preferred_label_languages_custom_fallback(tmp_path: Path) -> None:
    store = VocabStore(tmp_path / "fallback_vocab.db")
    concept = VocabConcept(
        uri="http://www.yso.fi/onto/yso/p999",
        vocabulary="yso",
        pref_label_fi="kirjat",
        pref_label_sv="böcker",
        pref_label_en=None,
        target_field="650",
    )
    store.insert_concepts([concept])

    # Default fallback priority: fi -> sv -> en
    folder_default = VocabFolder(store)
    _, _, _, label_default, _, _ = folder_default.fold_subject_heading(
        heading="kirjat",
        source_vocab="yso",
        source_field="650",
        source_uri="http://www.yso.fi/onto/yso/p999",
        lang="de",  # German has no label, triggers fallback
    )
    assert label_default == "kirjat"

    # Custom fallback priority: sv -> fi -> en
    cfg_custom = VocabNormalizationConfig(
        preferred_label_languages=["sv", "fi", "en"],
    )
    folder_custom = VocabFolder(store, config=cfg_custom)
    _, _, _, label_custom, _, _ = folder_custom.fold_subject_heading(
        heading="kirjat",
        source_vocab="yso",
        source_field="650",
        source_uri="http://www.yso.fi/onto/yso/p999",
        lang="de",
    )
    assert label_custom == "böcker"


def test_vocab_store_3_letter_language_codes(tmp_path: Path) -> None:
    store = VocabStore(tmp_path / "store_test.db")
    concept = VocabConcept(
        uri="http://www.yso.fi/onto/yso/p2591",
        vocabulary="yso",
        pref_label_fi="lääkärit",
        pref_label_sv="läkare",
        pref_label_en="physicians",
    )
    store.insert_concepts([concept])

    # Check 3-letter codes query correctly
    c_swe = store.find_concept_by_label("läkare", vocabulary="yso", lang="swe")
    assert c_swe is not None
    assert c_swe.uri == "http://www.yso.fi/onto/yso/p2591"

    c_eng = store.find_concept_by_label("physicians", vocabulary="yso", lang="eng")
    assert c_eng is not None
    assert c_eng.uri == "http://www.yso.fi/onto/yso/p2591"


def test_dynamic_default_vocab_target_field() -> None:
    # 1. Default config uses 650
    folder_default = VocabFolder()
    t_field, _, _, _, _, _ = folder_default.fold_subject_heading("test", source_field=None)
    assert t_field == "650"

    # 2. Custom config overriding default_vocab_target_field dynamically
    custom_cfg = VocabNormalizationConfig(
        default_vocab_target_field="699",
        default_field_vocabularies={"699": "yso"},
    )
    folder_custom = VocabFolder(config=custom_cfg)
    t_field_custom, _, _, _, _, _ = folder_custom.fold_subject_heading("test", source_field=None)
    assert t_field_custom == "699"
