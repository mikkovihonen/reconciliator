"""Unit tests for VocabStore and SQLite indexed queries."""

from pathlib import Path

import pytest

from reconciliator.models import ConceptMapping, VocabConcept
from reconciliator.vocab import VocabStore


@pytest.fixture
def vocab_store(tmp_path: Path) -> VocabStore:
    db_path = tmp_path / "test_vocab.db"
    return VocabStore(db_path)


def test_insert_and_query_concepts(vocab_store: VocabStore) -> None:
    concept = VocabConcept(
        uri="http://www.yso.fi/onto/yso/p2591",
        vocabulary="yso",
        pref_labels={"fi": "lääkärit", "sv": "läkare", "en": "physicians"},
        alt_labels={"fi": ["tohtorit"], "sv": ["doktorer"]},
        target_field="650",
    )
    vocab_store.insert_concepts([concept])

    # Query by URI
    res = vocab_store.find_concept_by_uri("http://www.yso.fi/onto/yso/p2591")
    assert res is not None
    assert res.pref_labels["fi"] == "lääkärit"
    assert res.pref_labels["sv"] == "läkare"
    assert "tohtorit" in res.alt_labels["fi"]

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
        legacy_labels={"fi": "lääkärit"},
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
    with vocab_store.get_connection() as conn:
        assert conn is not None

    concept = VocabConcept(
        uri="http://www.yso.fi/onto/yso/p1234",
        vocabulary="yso",
        pref_labels={"fi": "testi", "sv": "test", "en": "test"},
        alt_labels={},
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


def test_multilingual_concept_storage_and_query(vocab_store: VocabStore) -> None:
    """Test storing and querying concepts with arbitrary languages beyond fi/sv/en."""
    concept = VocabConcept(
        uri="http://www.yso.fi/onto/yso/p9001",
        vocabulary="yso",
        pref_labels={
            "fi": "kissa",
            "sv": "katt",
            "en": "cat",
            "de": "Katze",
            "sme": "bussá",
        },
        alt_labels={
            "fi": ["kisu", "katti"],
            "de": ["Hauskatze"],
        },
        target_field="650",
    )
    vocab_store.insert_concepts([concept])

    # Lookup by URI
    res = vocab_store.find_concept_by_uri("http://www.yso.fi/onto/yso/p9001")
    assert res is not None
    assert res.pref_labels["de"] == "Katze"
    assert res.pref_labels["sme"] == "bussá"
    assert res.alt_labels["de"] == ["Hauskatze"]

    # Lookup by German label
    by_de = vocab_store.find_concept_by_label("katze", lang="de")
    assert by_de is not None
    assert by_de.uri == "http://www.yso.fi/onto/yso/p9001"

    # Lookup by Northern Sami label
    by_sme = vocab_store.find_concept_by_label("bussá", lang="sme")
    assert by_sme is not None
    assert by_sme.uri == "http://www.yso.fi/onto/yso/p9001"


def test_language_filtering_on_insert(tmp_path: Path) -> None:
    """Test restricting ingested languages via configuration."""
    from reconciliator.config import VocabulariesConfig

    cfg = VocabulariesConfig(
        ingest_pref_label_languages=["fi", "sv"],
        ingest_alt_label_languages=["fi"],
    )
    store = VocabStore(tmp_path / "filtered_vocab.db", config=cfg)

    concept = VocabConcept(
        uri="http://www.yso.fi/onto/yso/p9002",
        vocabulary="yso",
        pref_labels={
            "fi": "koira",
            "sv": "hund",
            "en": "dog",
            "de": "Hund",
        },
        alt_labels={
            "fi": ["hauva"],
            "sv": ["vovve"],
        },
        target_field="650",
    )
    store.insert_concepts([concept])

    res = store.find_concept_by_uri("http://www.yso.fi/onto/yso/p9002")
    assert res is not None
    # Allowed languages present
    assert "fi" in res.pref_labels
    assert "sv" in res.pref_labels
    assert "fi" in res.alt_labels
    # Filtered languages absent
    assert "en" not in res.pref_labels
    assert "de" not in res.pref_labels
    assert "sv" not in res.alt_labels


def test_rebuild_aliases_multilingual(vocab_store: VocabStore) -> None:
    """Test rebuild_aliases_from_concepts generates search aliases for all stored languages."""
    concept = VocabConcept(
        uri="http://www.yso.fi/onto/yso/p9003",
        vocabulary="yso",
        pref_labels={
            "fi": "hevonen",
            "sv": "häst",
            "en": "horse",
            "de": "Pferd",
        },
        alt_labels={
            "de": ["Gaul"],
        },
        target_field="650",
    )
    vocab_store.insert_concepts([concept])
    num_aliases = vocab_store.rebuild_aliases_from_concepts()
    assert num_aliases >= 5

    # Check German alias lookup
    c_de, _ = vocab_store.find_concept_by_alias("pferd")
    assert c_de is not None
    assert c_de.uri == "http://www.yso.fi/onto/yso/p9003"

    # Check German alt alias lookup
    c_gaul, _ = vocab_store.find_concept_by_alias("gaul")
    assert c_gaul is not None
    assert c_gaul.uri == "http://www.yso.fi/onto/yso/p9003"


def test_find_concept_by_uri_variants_and_canonical_id(vocab_store: VocabStore) -> None:
    """Test find_concept_by_uri resolves canonical IDs, HTTPS, bare domains, and trailing slashes."""
    concept = VocabConcept(
        uri="http://www.yso.fi/onto/yso/p2591",
        vocabulary="yso",
        pref_labels={"fi": "lääkärit"},
        target_field="650",
    )
    vocab_store.insert_concepts([concept])

    # Canonical ID (yso:p2591)
    res_cid = vocab_store.find_concept_by_uri("yso:p2591")
    assert res_cid is not None
    assert res_cid.uri == "http://www.yso.fi/onto/yso/p2591"

    # MARC control notation ((FI-YSO)p2591)
    res_marc = vocab_store.find_concept_by_uri("(FI-YSO)p2591")
    assert res_marc is not None
    assert res_marc.uri == "http://www.yso.fi/onto/yso/p2591"

    # HTTPS and trailing slash
    res_https = vocab_store.find_concept_by_uri("https://www.yso.fi/onto/yso/p2591/")
    assert res_https is not None
    assert res_https.uri == "http://www.yso.fi/onto/yso/p2591"


def test_find_concept_by_alias_auto_folding(vocab_store: VocabStore) -> None:
    """Test find_concept_by_alias auto-folds raw, uppercase, or accented input labels."""
    concept = VocabConcept(
        uri="http://www.yso.fi/onto/yso/p9003",
        vocabulary="yso",
        pref_labels={
            "fi": "hevonen",
            "sv": "häst",
        },
        target_field="650",
    )
    vocab_store.insert_concepts([concept])
    vocab_store.rebuild_aliases_from_concepts()

    # Pass uppercase / raw label
    c_raw, _ = vocab_store.find_concept_by_alias("Hevonen")
    assert c_raw is not None
    assert c_raw.uri == concept.uri

    # Pass uppercase accented Swedish label
    c_sv, _ = vocab_store.find_concept_by_alias("HÄST")
    assert c_sv is not None
    assert c_sv.uri == concept.uri


def test_find_concepts_by_uris_batch(vocab_store: VocabStore) -> None:
    """Test batch concept lookup via find_concepts_by_uris."""
    c1 = VocabConcept(
        uri="http://www.yso.fi/onto/yso/p1",
        vocabulary="yso",
        pref_labels={"fi": "termi1"},
        target_field="650",
    )
    c2 = VocabConcept(
        uri="http://www.yso.fi/onto/yso/p2",
        vocabulary="yso",
        pref_labels={"fi": "termi2"},
        target_field="650",
    )
    vocab_store.insert_concepts([c1, c2])

    uris = ["http://www.yso.fi/onto/yso/p1", "yso:p2", "http://nonexistent.org/unknown"]
    results = vocab_store.find_concepts_by_uris(uris)

    assert len(results) == 2
    assert "http://www.yso.fi/onto/yso/p1" in results
    assert "yso:p2" in results
    assert results["yso:p2"].uri == "http://www.yso.fi/onto/yso/p2"


def test_insert_large_concepts_batch_exceeding_postgres_param_limit(
    vocab_store: VocabStore,
) -> None:
    """Ensure insert_concepts_batch chunks large concept batches exceeding 65,535 query parameters."""
    # 17,000 concepts * 4 parameters = 68,000 parameters (exceeding Postgres limit of 65,535)
    concepts = [
        VocabConcept(
            uri=f"http://www.yso.fi/onto/yso/p{i}",
            vocabulary="yso",
            pref_labels={"fi": f"käsite_{i}"},
            target_field="650",
        )
        for i in range(17000)
    ]
    vocab_store.insert_concepts_batch(concepts)

    c_first = vocab_store.find_concept_by_id("yso", "p0")
    assert c_first is not None
    assert c_first.pref_labels["fi"] == "käsite_0"

    c_last = vocab_store.find_concept_by_id("yso", "p16999")
    assert c_last is not None
    assert c_last.pref_labels["fi"] == "käsite_16999"


def test_insert_concepts_batch_with_duplicates_and_label_merging(vocab_store: VocabStore) -> None:
    """Ensure duplicate concepts within the same batch are merged and do not violate PostgreSQL ON CONFLICT cardinality."""
    c1 = VocabConcept(
        uri="http://www.yso.fi/onto/yso/p100",
        vocabulary="yso",
        pref_labels={"fi": "kirjat"},
        alt_labels={"fi": ["opukset"]},
        target_field="650",
    )
    c2 = VocabConcept(
        uri="http://www.yso.fi/onto/yso/p100",
        vocabulary="yso",
        pref_labels={"sv": "böcker", "en": "books"},
        alt_labels={"sv": ["volymer"]},
        target_field="650",
    )
    # Both concepts have identical (vocabulary, concept_id) = ('yso', 'p100')
    vocab_store.insert_concepts_batch([c1, c2])

    res = vocab_store.find_concept_by_id("yso", "p100")
    assert res is not None
    assert res.pref_labels["fi"] == "kirjat"
    assert res.pref_labels["sv"] == "böcker"
    assert res.pref_labels["en"] == "books"
    assert "opukset" in res.alt_labels["fi"]
    assert "volymer" in res.alt_labels["sv"]
