"""Unit tests for vocabulary ontology ingestion pipeline."""

from pathlib import Path

from reconciliator.ingestion.vocab import ingest_vocab_file
from reconciliator.vocab_store import VocabStore


def test_ingest_vocab_file(tmp_path: Path) -> None:
    store = VocabStore(tmp_path)
    ttl_path = Path(__file__).parent / "fixtures" / "sample_finto.ttl"

    c_count, m_count = ingest_vocab_file(ttl_path, store)
    assert c_count >= 5
    assert m_count >= 3

    # Check concept lookup
    c = store.find_concept_by_label("lääkärit", vocabulary="yso")
    assert c is not None
    assert c.uri == "http://www.yso.fi/onto/yso/p2591"
    assert c.pref_labels.get("sv") == "läkare"
    assert not c.is_deprecated

    # Check deprecated concept lookup
    dep_c = store.find_concept_by_label("vanha termi", vocabulary="yso")
    assert dep_c is not None
    assert dep_c.uri == "http://www.yso.fi/onto/yso/p9999"
    assert dep_c.is_deprecated

    # Check mapping lookup
    m = store.find_legacy_mapping_by_uri("http://www.yso.fi/onto/ysa/Y1002")
    assert m is not None
    assert m.target_uri == "http://www.yso.fi/onto/yso/p2591"


def test_is_language_allowed_wildcards() -> None:
    """Test wildcard and list behavior in is_language_allowed."""
    from reconciliator.vocab_store import is_language_allowed

    # Wildcards: None, "*", "all", ["*"], ["all"]
    assert is_language_allowed("fi", None) is True
    assert is_language_allowed("de", "*") is True
    assert is_language_allowed("sme", "all") is True
    assert is_language_allowed("fr", ["*"]) is True
    assert is_language_allowed("no", ["all"]) is True

    # Explicit list
    allowed = ["fi", "sv", "en"]
    assert is_language_allowed("fi", allowed) is True
    assert is_language_allowed("FI", allowed) is True
    assert is_language_allowed("sv", allowed) is True
    assert is_language_allowed("de", allowed) is False
    assert is_language_allowed("sme", allowed) is False


def test_ingest_multilingual_and_filtering(tmp_path: Path) -> None:
    """Test parsing and ingesting arbitrary languages, testing both wildcard ingest-all and explicit filtering."""
    from reconciliator.config import VocabIngestConfig
    from reconciliator.ingestion.vocab import parse_turtle_simple

    sample_multilingual_ttl = """
    @prefix skos: <http://www.w3.org/2004/02/skos/core#> .

    <http://www.yso.fi/onto/yso/p7777> a skos:Concept ;
        skos:prefLabel "tietokoneet"@fi ;
        skos:prefLabel "datorer"@sv ;
        skos:prefLabel "computers"@en ;
        skos:prefLabel "Computer"@de ;
        skos:prefLabel "dihtorat"@sme ;
        skos:prefLabel "ordinateurs"@fr ;
        skos:altLabel "tietsikat"@fi ;
        skos:altLabel "Rechner"@de ;
        skos:altLabel "PC"@en .
    """

    # 1. Ingest all (wildcard None / "*")
    concepts_all, _ = parse_turtle_simple(sample_multilingual_ttl, pref_label_languages="*")
    assert len(concepts_all) == 1
    c_all = concepts_all[0]
    assert c_all.pref_labels["de"] == "Computer"
    assert c_all.pref_labels["sme"] == "dihtorat"
    assert c_all.pref_labels["fr"] == "ordinateurs"
    assert c_all.pref_labels["fi"] == "tietokoneet"
    assert len(c_all.pref_labels) == 6
    assert len(c_all.alt_labels) == 3

    # 2. Ingest with explicit language filter
    ttl_file = tmp_path / "multi.ttl"
    ttl_file.write_text(sample_multilingual_ttl, encoding="utf-8")

    db_path = tmp_path / "filtered_vocab.db"
    store = VocabStore(db_path)
    ingest_config = VocabIngestConfig(
        pref_label_languages=["fi", "de"],
        alt_label_languages=["de"],
    )
    c_count, _ = ingest_vocab_file(ttl_file, store, config=ingest_config)
    assert c_count == 1

    stored = store.find_concept_by_uri("http://www.yso.fi/onto/yso/p7777")
    assert stored is not None
    # Only fi and de pref labels
    assert set(stored.pref_labels.keys()) == {"fi", "de"}
    assert stored.pref_labels["de"] == "Computer"
    assert stored.pref_labels["fi"] == "tietokoneet"
    # Only de alt label
    assert set(stored.alt_labels.keys()) == {"de"}
    assert stored.alt_labels["de"] == ["Rechner"]

    store.close()
