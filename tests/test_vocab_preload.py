"""Tests for in-memory vocabulary preloading and process-level shared index singleton."""

import threading
from pathlib import Path
from unittest.mock import patch

from reconciliator.config import VocabPreloadConfig, VocabulariesConfig
from reconciliator.models import VocabConcept
from reconciliator.vocab.store import (
    VocabStore,
    get_shared_vocab_index,
)


def test_vocab_preload_config_defaults():
    """Verify VocabPreloadConfig defaults and integration with VocabulariesConfig."""
    cfg = VocabPreloadConfig()
    assert cfg.enabled is True
    assert cfg.preload_concepts is True
    assert cfg.preload_pref_labels is True
    assert cfg.preload_aliases is True

    vocab_cfg = VocabulariesConfig()
    assert isinstance(vocab_cfg.preload, VocabPreloadConfig)
    assert vocab_cfg.preload.enabled is True


def test_vocab_preload_config_custom():
    """Verify custom overrides for VocabPreloadConfig."""
    cfg = VocabPreloadConfig(
        enabled=False,
        preload_concepts=False,
        preload_pref_labels=False,
        preload_aliases=False,
    )
    assert cfg.enabled is False
    assert cfg.preload_concepts is False
    assert cfg.preload_pref_labels is False
    assert cfg.preload_aliases is False


def test_shared_vocab_index_singleton(tmp_path: Path):
    """Verify that multiple VocabStore instances for the same db_path share the exact same VocabMemoryIndex."""
    db_file = tmp_path / "shared_test.db"
    store1 = VocabStore(db_file)
    store2 = VocabStore(db_file)

    assert store1._index is store2._index
    assert store1._index is get_shared_vocab_index(store1.db_path)

    # Different database gets a different index
    db_file2 = tmp_path / "other_test.db"
    store3 = VocabStore(db_file2)
    assert store3._index is not store1._index


def test_vocab_store_preload_and_memory_lookups(tmp_path: Path):
    """Verify that preload() populates in-memory maps and subsequent lookups resolve in memory."""
    db_file = tmp_path / "preload_data.db"
    cfg = VocabulariesConfig(preload=VocabPreloadConfig(enabled=False))
    store = VocabStore(db_file, config=cfg)

    # Insert sample concepts
    c1 = VocabConcept(
        uri="http://www.yso.fi/onto/yso/p1234",
        vocabulary="yso",
        concept_id="p1234",
        pref_labels={"fi": "historia", "en": "history"},
        alt_labels={"fi": ["historiatiede"]},
        target_field="650",
    )
    c2 = VocabConcept(
        uri="http://www.yso.fi/onto/yso-paikat/p5678",
        vocabulary="yso-paikat",
        concept_id="p5678",
        pref_labels={"fi": "Helsinki", "sv": "Helsingfors"},
        target_field="651",
    )
    store.insert_concepts_batch([c1, c2])

    # Insert sample aliases
    store.insert_aliases(
        [
            {
                "folded_label": "historiatiede",
                "raw_label": "historiatiede",
                "lang": "fi",
                "concept_id": "p1234",
                "vocabulary": "yso",
                "target_field": "650",
                "is_preferred": 0,
                "source": "vocab",
            }
        ]
    )

    # Clear caches and preload
    store.clear_caches()
    assert store._index.is_preloaded is False
    assert len(store._index.concepts_by_id) == 0

    # Call preload
    store.preload()
    assert store._index.is_preloaded is True
    assert ("yso", "p1234") in store._index.concepts_by_id
    assert ("yso-paikat", "p5678") in store._index.concepts_by_id
    assert "http://www.yso.fi/onto/yso/p1234" in store._index.concepts_by_uri
    assert ("historia", "yso", "fi") in store._index.pref_labels
    assert ("historiatiede", "650") in store._index.alias_index

    # Now verify lookups resolve directly from memory without SQL
    with patch.object(store.engine, "connect", side_effect=AssertionError("Should not hit DB!")):
        # 1. find_concept_by_id
        res_id = store.find_concept_by_id("yso", "p1234")
        assert res_id is not None
        assert res_id.concept_id == "p1234"
        assert res_id.pref_labels["fi"] == "historia"

        # 2. find_concept_by_uri
        res_uri = store.find_concept_by_uri("http://www.yso.fi/onto/yso/p1234")
        assert res_uri is not None
        assert res_uri.concept_id == "p1234"

        # 3. find_concept_by_label
        res_lbl = store.find_concept_by_label("historia", vocabulary="yso")
        assert res_lbl is not None
        assert res_lbl.concept_id == "p1234"

        # 4. find_concept_by_alias
        res_alias, _qid = store.find_concept_by_alias("historiatiede", target_field="650")
        assert res_alias is not None
        assert res_alias.concept_id == "p1234"

        # Non-existent label returns None immediately without hitting DB
        assert store.find_concept_by_label("olematon_termi", vocabulary="yso") is None


def test_clear_caches_resets_shared_index(tmp_path: Path):
    """Verify that clear_caches() clears the shared index and LRU caches."""
    db_file = tmp_path / "clear_test.db"
    store = VocabStore(db_file)
    store._index.is_preloaded = True
    store._index.concepts_by_id[("yso", "p1")] = VocabConcept(
        vocabulary="yso", concept_id="p1", pref_labels={}
    )

    store.clear_caches()
    assert store._index.is_preloaded is False
    assert len(store._index.concepts_by_id) == 0


def test_concurrent_preload_thread_safety(tmp_path: Path):
    """Verify thread-safe concurrent initialization of preload()."""
    db_file = tmp_path / "concurrent.db"
    cfg = VocabulariesConfig(preload=VocabPreloadConfig(enabled=False))
    store = VocabStore(db_file, config=cfg)

    # Insert a concept
    store.insert_concepts_batch(
        [
            VocabConcept(
                vocabulary="yso",
                concept_id="p999",
                pref_labels={"fi": "testi"},
            )
        ]
    )

    errors = []

    def worker():
        try:
            local_store = VocabStore(db_file, config=cfg)
            local_store.preload()
            assert local_store._index.is_preloaded is True
            res = local_store.find_concept_by_id("yso", "p999")
            assert res is not None
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(errors) == 0
    assert store._index.is_preloaded is True
