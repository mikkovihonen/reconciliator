"""Unit tests for vocabulary ontology ingestion pipeline."""

from pathlib import Path

from reconciliator.ingestion.vocab import ingest_vocab_file
from reconciliator.vocab_store import VocabStore


def test_ingest_vocab_file(tmp_path: Path) -> None:
    vocab_db = tmp_path / "vocab.db"
    store = VocabStore(vocab_db)
    ttl_path = Path(__file__).parent / "fixtures" / "sample_finto.ttl"

    c_count, m_count = ingest_vocab_file(ttl_path, store)
    assert c_count >= 5
    assert m_count >= 3

    # Check concept lookup
    c = store.find_concept_by_label("lääkärit", vocabulary="yso")
    assert c is not None
    assert c.uri == "http://www.yso.fi/onto/yso/p2591"
    assert c.pref_label_sv == "läkare"
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
