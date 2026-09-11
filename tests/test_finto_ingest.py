"""Unit tests for FINTO ontology ingestion pipeline."""

from pathlib import Path

from reconciliator.finto_store import FintoStore
from reconciliator.ingestion.finto import ingest_finto_file


def test_ingest_finto_file(tmp_path: Path) -> None:
    finto_db = tmp_path / "finto.db"
    store = FintoStore(finto_db)
    ttl_path = Path(__file__).parent / "fixtures" / "sample_finto.ttl"

    c_count, m_count = ingest_finto_file(ttl_path, store)
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
