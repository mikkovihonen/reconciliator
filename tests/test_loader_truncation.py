"""Unit and integration tests for string length sanitization and telemetry across all loaders.

Covers:
- Controlled Vocabulary loader (concepts, labels, mappings, hierarchy, relator terms)
- Wikidata ingestion (Pass 1 authors against agents_table, Pass 2 WEMI batches)
- Corpus loader in reconciliation pipeline
- Configuration toggles and truncation markers
"""

import logging
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from reconciliator.config import (
    ReconcileConfig,
    VocabIngestConfig,
    WikidataIngestConfig,
)
from reconciliator.database.schema import (
    agents_table,
    concept_hierarchy_table,
    relator_terms_table,
)
from reconciliator.ingestion.sanitizer import (
    sanitize_batch_payload,
    sanitize_entities,
    sanitize_vocab_concepts,
    sanitize_vocab_mappings,
)
from reconciliator.models import ConceptMapping, ManifestationEntity, VocabConcept, WorkEntity


def test_vocab_concepts_truncation(caplog: pytest.LogCaptureFixture) -> None:
    """Verify VocabConcept fields and nested pref/alt labels are sanitized against schema limits."""
    concept = VocabConcept(
        uri="http://example.org/concept/1",
        vocabulary="V" * 100,  # schema limit is 64
        concept_id="C" * 200,  # schema limit is 128
        target_field="T" * 50,  # schema limit is 32
        pref_labels={"fi": "P" * 600, "en": "Short Label"},  # schema limit is 512
        alt_labels={"fi": ["A" * 700, "Valid Alt"]},  # schema limit is 512
    )

    config = VocabIngestConfig(
        truncate_overlong_values=True,
        truncation_marker="...",
    )
    tracker = MagicMock()
    tracker.task_id = "test-vocab-task-1"

    with caplog.at_level(logging.ERROR):
        count = sanitize_vocab_concepts(
            [concept], config=config, task_id="test-vocab-task-1", tracker=tracker
        )

    # 5 fields truncated: vocabulary, concept_id, target_field, pref_labels["fi"], alt_labels["fi"][0]
    assert count == 5
    assert len(concept.vocabulary) == 64
    assert concept.vocabulary.endswith("...")
    assert len(concept.concept_id) == 128
    assert concept.concept_id.endswith("...")
    assert len(concept.target_field) == 32
    assert concept.target_field.endswith("...")
    assert len(concept.pref_labels["fi"]) == 512
    assert concept.pref_labels["fi"].endswith("...")
    assert concept.pref_labels["en"] == "Short Label"
    assert len(concept.alt_labels["fi"][0]) == 512
    assert concept.alt_labels["fi"][0].endswith("...")
    assert concept.alt_labels["fi"][1] == "Valid Alt"

    # Verify task_id telemetry
    assert "[sanitization] truncate" in caplog.text
    assert any(getattr(r, "task_id", None) == "test-vocab-task-1" for r in caplog.records)
    assert tracker.record_error.call_count == 5


def test_vocab_mappings_truncation(caplog: pytest.LogCaptureFixture) -> None:
    """Verify ConceptMapping fields and legacy_labels are sanitized."""
    mapping = ConceptMapping(
        legacy_uri="http://example.org/legacy/1",
        legacy_vocab="LV" * 50,  # limit 64
        legacy_id="LID" * 100,  # limit 128
        target_uri="http://example.org/target/1",
        target_vocab="TV" * 50,  # limit 64
        target_id="TID" * 100,  # limit 128
        target_field="TF" * 30,  # limit 32
        mapping_type="exact_match",
        legacy_labels={"fi": "L" * 600},  # limit 512
    )

    config = VocabIngestConfig(
        truncate_overlong_values=True,
        truncation_marker="",
    )
    tracker = MagicMock()
    tracker.task_id = "vocab-map-task"

    with caplog.at_level(logging.ERROR):
        count = sanitize_vocab_mappings(
            [mapping], config=config, task_id="vocab-map-task", tracker=tracker
        )

    assert count == 6
    assert len(mapping.legacy_vocab) == 64
    assert len(mapping.legacy_id) == 128
    assert len(mapping.target_vocab) == 64
    assert len(mapping.target_id) == 128
    assert len(mapping.target_field) == 32
    assert len(mapping.legacy_labels["fi"]) == 512

    assert "[sanitization] truncate" in caplog.text
    assert any(getattr(r, "task_id", None) == "vocab-map-task" for r in caplog.records)
    assert tracker.record_error.call_count == 6


def test_sanitize_entities_hierarchy_and_relators() -> None:
    """Verify generic sanitize_entities works for concept_hierarchy and relator_terms."""
    edges = [
        {
            "vocabulary": "V" * 100,  # limit 64
            "concept_id": "C" * 200,  # limit 128
            "broader_concept_id": "B" * 200,  # limit 128
            "relation_type": "broader",
        }
    ]
    relators = [
        {
            "folded_term": "F" * 200,  # limit 128
            "raw_term": "R" * 400,  # limit 256
            "lang": "fi",
            "code": "aut",
            "uri": "http://example.org/relator/aut",
        }
    ]

    config = VocabIngestConfig(truncate_overlong_values=True)
    tracker = MagicMock()

    c_edges = sanitize_entities(
        edges, concept_hierarchy_table, config=config, task_id="edge-task", tracker=tracker
    )
    assert c_edges == 3
    assert len(edges[0]["vocabulary"]) == 64
    assert len(edges[0]["concept_id"]) == 128
    assert len(edges[0]["broader_concept_id"]) == 128

    c_rel = sanitize_entities(
        relators, relator_terms_table, config=config, task_id="rel-task", tracker=tracker
    )
    assert c_rel == 2
    assert len(relators[0]["folded_term"]) == 128
    assert len(relators[0]["raw_term"]) == 256


def test_wikidata_author_sanitization() -> None:
    """Verify Wikidata author prescan sanitization against agents_table."""
    authors = [
        {
            "qid": "Q12345",
            "name": "N" * 150,
            "surname": "S" * 100,
            "family_name": "S" * 100,
            "identifiers": {"isni": "00000001"},
        }
    ]

    # Test with field_length_overrides on agents
    config = WikidataIngestConfig(
        truncate_overlong_values=True,
        field_length_overrides={"agents.name": 80, "surname": 50},
        truncation_marker="...",
    )
    tracker = MagicMock()
    tracker.task_id = "wiki-author-task"

    count = sanitize_entities(
        authors, agents_table, config=config, task_id="wiki-author-task", tracker=tracker
    )
    assert count == 2
    assert len(authors[0]["name"]) == 80
    assert authors[0]["name"].endswith("...")
    assert len(authors[0]["surname"]) == 50
    assert authors[0]["surname"].endswith("...")


def test_wikidata_wemi_batch_sanitization() -> None:
    """Verify Wikidata Pass 2 WEMI decomposition batches are sanitized."""
    work = WorkEntity(
        id=1,
        source="wikidata",
        source_id="Q999",
        work_type="text",
        title="T" * 200,
        title_normalized="t",
    )
    manifestation = ManifestationEntity(
        id=1,
        source="wikidata",
        source_id="Q999-m",
        title="MT" * 100,
        title_normalized="mt",
        publication_place="P" * 120,
    )

    payload = {
        "works": [work],
        "manifestations": [manifestation],
        "record_origin": "wikidata",
    }
    config = WikidataIngestConfig(
        truncate_overlong_values=True,
        field_length_overrides={
            "works.title": 50,
            "manifestations.publication_place": 60,
        },
    )
    tracker = MagicMock()
    tracker.task_id = "wiki-wemi-task"

    count = sanitize_batch_payload(
        payload, config=config, task_id="wiki-wemi-task", tracker=tracker
    )
    assert count == 2
    assert len(work.title) == 50
    assert len(manifestation.publication_place) == 60
    assert tracker.record_error.call_count == 2


def test_reconcile_corpus_sanitization() -> None:
    """Verify ReconcileConfig corpus loader sanitization on WEMI batch payload."""
    work = WorkEntity(
        id=1,
        source="corpus",
        source_id="item-001",
        work_type="text",
        title="C" * 300,
        title_normalized="c",
    )
    payload = {
        "works": [work],
        "record_origin": "corpus",
    }
    config = ReconcileConfig(
        target_file="dummy.xml",
        truncate_overlong_values=True,
        default_max_field_length=150,
        truncation_marker="[CUT]",
    )
    tracker = MagicMock()
    tracker.task_id = "reconcile-corpus-task"

    count = sanitize_batch_payload(
        payload, config=config, task_id="reconcile-corpus-task", tracker=tracker
    )
    assert count == 1
    assert len(work.title) == 150
    assert work.title.endswith("[CUT]")
    tracker.record_error.assert_called_once()


def test_truncation_disabled_across_configs() -> None:
    """Verify all loaders respect truncate_overlong_values=False."""
    # Vocab
    c = VocabConcept(
        uri="u1",
        vocabulary="V" * 100,
        concept_id="C" * 200,
        pref_labels={"fi": "P" * 600},
    )
    v_cfg = VocabIngestConfig(truncate_overlong_values=False)
    assert sanitize_vocab_concepts([c], config=v_cfg, task_id="t1") == 0
    assert len(c.vocabulary) == 100
    assert len(c.concept_id) == 200
    assert len(c.pref_labels["fi"]) == 600

    # Wikidata / Reconcile
    w = WorkEntity(
        id=1, source="s", source_id="1", work_type="text", title="T" * 500, title_normalized="t"
    )
    payload = {"works": [w]}
    r_cfg = ReconcileConfig(
        target_file="dummy.xml", truncate_overlong_values=False, default_max_field_length=100
    )
    assert sanitize_batch_payload(payload, config=r_cfg, task_id="t2") == 0
    assert len(w.title) == 500


def test_ingest_vocab_file_truncation_integration(tmp_path: Path) -> None:
    """Integration test: ingest_vocab_file truncates overlong values, saves to VocabStore, and logs error telemetry."""
    from reconciliator.ingestion.vocab import ingest_vocab_file
    from reconciliator.vocab import VocabStore

    overlong_label = "L" * 600
    ttl_content = f"""
    @prefix skos: <http://www.w3.org/2004/02/skos/core#> .

    <http://www.yso.fi/onto/yso/p12345> a skos:Concept ;
        skos:prefLabel "{overlong_label}"@fi ;
        skos:prefLabel "Normal Label"@en .
    """
    ttl_path = tmp_path / "overlong.ttl"
    ttl_path.write_text(ttl_content, encoding="utf-8")

    db_path = tmp_path / "vocab.db"
    store = VocabStore(db_path)

    config = VocabIngestConfig(
        truncate_overlong_values=True,
        truncation_marker="...",
    )
    tracker = MagicMock()
    tracker.task_id = "vocab-ingest-integration-task"

    c_count, _ = ingest_vocab_file(ttl_path, store, tracker=tracker, config=config)
    assert c_count == 1

    concept = store.find_concept_by_uri("http://www.yso.fi/onto/yso/p12345")
    assert concept is not None
    assert len(concept.pref_labels["fi"]) == 512
    assert concept.pref_labels["fi"].endswith("...")
    assert concept.pref_labels["en"] == "Normal Label"

    tracker.record_error.assert_called()
    call_args = tracker.record_error.call_args[1]
    assert call_args["metadata"]["last_truncated_task_id"] == "vocab-ingest-integration-task"

    store.close()


def test_wikidata_author_prescan_truncation_integration(tmp_path: Path) -> None:
    """Integration test: _prescan_authors truncates overlong fields and logs error telemetry."""
    import gzip

    import orjson

    from reconciliator.database import WEMIRepository
    from reconciliator.ingestion.wikidata import _prescan_authors

    overlong_name = "N" * 200
    author_entity = {
        "id": "Q77777",
        "labels": {"fi": {"value": overlong_name}, "en": {"value": overlong_name}},
        "claims": {
            "P31": [{"mainsnak": {"datavalue": {"value": {"id": "Q5"}}}}],
            "P3595": [{"mainsnak": {"datavalue": {"value": "000099999"}}}],
        },
    }

    dump_path = tmp_path / "authors_overlong.json.gz"
    with gzip.open(dump_path, "wb") as f:
        f.write(orjson.dumps(author_entity) + b"\n")

    auth_db = tmp_path / "auth.db"
    w_repo = WEMIRepository(db_path=auth_db)

    config = WikidataIngestConfig(
        truncate_overlong_values=True,
        field_length_overrides={"agents.name": 80},
        truncation_marker="...",
    )
    tracker = MagicMock()
    tracker.task_id = "wiki-prescan-integration-task"

    persisted = _prescan_authors(dump_path, w_repo, tracker=tracker, config=config)
    assert persisted == 1

    author = w_repo.get_wikidata_author("Q77777")
    assert author is not None
    assert len(author["name"]) == 80
    assert author["name"].endswith("...")

    tracker.record_error.assert_called()
