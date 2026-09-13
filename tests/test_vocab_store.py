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
    from reconciliator.config import VocabNormalizationConfig

    cfg = VocabNormalizationConfig(
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


def test_legacy_flat_db_migration(tmp_path: Path) -> None:
    """Test automatic seamless migration from legacy flat tables to normalized 1-to-many schema."""
    import sqlite3

    db_path = tmp_path / "legacy_flat.db"
    # 1. Create flat schema manually simulating older version of reconciliator
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    with conn:
        conn.execute("""
            CREATE TABLE concepts (
                uri TEXT PRIMARY KEY,
                vocabulary TEXT NOT NULL,
                pref_label_fi TEXT,
                pref_label_sv TEXT,
                pref_label_en TEXT,
                alt_labels_fi TEXT,
                alt_labels_sv TEXT,
                is_deprecated INTEGER DEFAULT 0,
                target_field TEXT
            );
        """)
        conn.execute("""
            INSERT INTO concepts VALUES (
                'http://www.yso.fi/onto/yso/p111',
                'yso',
                'kirjat',
                'böcker',
                'books',
                '["opus", "teos"]',
                '["volym"]',
                0,
                '650'
            );
        """)
        conn.execute("""
            CREATE TABLE concept_mappings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                legacy_uri TEXT,
                legacy_vocab TEXT NOT NULL,
                legacy_label_fi TEXT,
                legacy_label_sv TEXT,
                target_uri TEXT NOT NULL,
                target_vocab TEXT NOT NULL,
                mapping_type TEXT NOT NULL,
                target_field TEXT NOT NULL
            );
        """)
        conn.execute("""
            INSERT INTO concept_mappings VALUES (
                1,
                'http://www.yso.fi/onto/ysa/Y999',
                'ysa',
                'kirjat',
                'böcker',
                'http://www.yso.fi/onto/yso/p111',
                'yso',
                'exact_match',
                '650'
            );
        """)
    conn.close()

    # 2. Open with modern VocabStore - should auto-migrate
    store = VocabStore(db_path)

    # 3. Verify concept was migrated
    c = store.find_concept_by_uri("http://www.yso.fi/onto/yso/p111")
    assert c is not None
    assert c.pref_label_fi == "kirjat"
    assert c.pref_label_sv == "böcker"
    assert c.pref_label_en == "books"
    assert "opus" in c.alt_labels_fi
    assert "volym" in c.alt_labels_sv

    # 4. Verify lookup by label works
    by_fi = store.find_concept_by_label("Kirjat", vocabulary="yso", lang="fi")
    assert by_fi is not None
    assert by_fi.uri == c.uri

    # 5. Verify mapping was migrated
    m = store.find_legacy_mapping_by_uri("http://www.yso.fi/onto/ysa/Y999")
    assert m is not None
    assert m.legacy_label_fi == "kirjat"
    assert m.legacy_label_sv == "böcker"
    assert m.target_uri == "http://www.yso.fi/onto/yso/p111"

    # 6. Verify table columns are now normalized
    with store.get_connection() as c_conn:
        cols = {row["name"] for row in c_conn.execute("PRAGMA table_info(concepts);").fetchall()}
        assert "pref_label_fi" not in cols
        assert "pref_labels" not in cols
        assert "uri" in cols

        # Check concept_labels has entries
        label_count = c_conn.execute("SELECT COUNT(*) FROM concept_labels;").fetchone()[0]
        assert label_count >= 5  # fi, sv, en pref labels + 2 fi alt + 1 sv alt

    store.close()


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

