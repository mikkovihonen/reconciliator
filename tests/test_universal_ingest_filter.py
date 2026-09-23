"""Unit tests for universal IngestFilterConfig and publication type filtering."""

from __future__ import annotations

from pathlib import Path

from reconciliator.config import (
    AppConfig,
    IngestFilterConfig,
    IngestionConfig,
    OAIIngestConfig,
    ReconcileConfig,
    WikidataIngestConfig,
)
from reconciliator.ingestion.oai import _WEMIBatchAccumulator
from reconciliator.ingestion.wikidata import decompose_wikidata_entity, parse_wikidata_entity
from reconciliator.loaders.base import DecomposedRecord
from reconciliator.models import (
    ManifestationEntity,
    MatchRecord,
    WorkEntity,
)
from reconciliator.reconciliation.matcher import WorkMatcher


def test_ingest_filter_config_defaults() -> None:
    cfg = IngestFilterConfig()
    # Default is permissive (empty lists / None years)
    assert cfg.target_work_types == []
    assert cfg.exclude_work_types == []
    assert cfg.target_languages == []
    assert cfg.from_year is None
    assert cfg.until_year is None

    # Blank accept tests
    assert cfg.accepts(work_type="book", language="fin", year=1995) is True
    assert cfg.accepts(work_type=None, language=None, year=None) is True


def test_ingest_filter_criteria() -> None:
    # 1. Target work types filter
    cfg_target = IngestFilterConfig(target_work_types=["music", "audio_nonmusic"])
    assert cfg_target.accepts(work_type="music") is True
    assert cfg_target.accepts(work_type="audio_nonmusic") is True
    assert cfg_target.accepts(work_type="book") is False
    # If work_type is unknown, it is not rejected by target filter
    assert cfg_target.accepts(work_type=None) is True

    # 2. Exclude work types filter
    cfg_exclude = IngestFilterConfig(exclude_work_types=["comic", "periodical"])
    assert cfg_exclude.accepts(work_type="book") is True
    assert cfg_exclude.accepts(work_type="comic") is False
    assert cfg_exclude.accepts(work_type="periodical") is False

    # 3. Both target and exclude
    cfg_combo = IngestFilterConfig(
        target_work_types=["music", "audio_nonmusic"],
        exclude_work_types=["music"],
    )
    # Exclude takes precedence or rather criteria are ANDed: target must match AND exclude must not match
    assert cfg_combo.accepts(work_type="music") is False
    assert cfg_combo.accepts(work_type="audio_nonmusic") is True
    assert cfg_combo.accepts(work_type="book") is False

    # 4. Target languages filter
    cfg_lang = IngestFilterConfig(target_languages=["fin", "swe"])
    assert cfg_lang.accepts(work_type="text", language="fin") is True
    assert cfg_lang.accepts(work_type="text", language="swe") is True
    assert cfg_lang.accepts(work_type="text", language="eng") is False
    # If language is unknown, it is not rejected
    assert cfg_lang.accepts(work_type="text", language=None) is True

    # 5. Year ranges filter
    cfg_year = IngestFilterConfig(from_year=1900, until_year=2000)
    assert cfg_year.accepts(work_type="text", year=1950) is True
    assert cfg_year.accepts(work_type="text", year=1900) is True
    assert cfg_year.accepts(work_type="text", year=2000) is True
    assert cfg_year.accepts(work_type="text", year=1899) is False
    assert cfg_year.accepts(work_type="text", year=2001) is False
    assert cfg_year.accepts(work_type="text", year=None) is True


def test_app_config_ingest_filter_propagation() -> None:
    # When global ingest config has filters and source configs do not override, propagate
    app_cfg = AppConfig(
        ingestion=IngestionConfig(
            filter=IngestFilterConfig(
                target_work_types=["music", "audio_nonmusic"],
                target_languages=["fin"],
                from_year=1980,
                until_year=2020,
            ),
            oai={
                "source_a": OAIIngestConfig(),
                "source_b": OAIIngestConfig(target_work_types=["comic"]),
            },
            wikidata=WikidataIngestConfig(),
        ),
        reconcile=ReconcileConfig(target_file=Path("test.xml")),
    )

    # source_a inherits global
    assert app_cfg.ingestion.oai["source_a"].target_work_types == ["music", "audio_nonmusic"]
    assert app_cfg.ingestion.oai["source_a"].target_languages == ["fin"]
    assert app_cfg.ingestion.oai["source_a"].from_year == 1980

    # source_b retains its specific override
    assert app_cfg.ingestion.oai["source_b"].target_work_types == ["comic"]

    # wikidata inherits global
    assert app_cfg.ingestion.wikidata.target_work_types == ["music", "audio_nonmusic"]
    assert app_cfg.ingestion.wikidata.target_languages == ["fin"]

    # reconcile inherits global
    assert app_cfg.reconcile.target_work_types == ["music", "audio_nonmusic"]
    assert app_cfg.reconcile.from_year == 1980


def test_wemi_batch_accumulator_filtering() -> None:
    filter_cfg = IngestFilterConfig(
        target_work_types=["music"],
        target_languages=["fin"],
        from_year=1980,
    )
    acc = _WEMIBatchAccumulator(source_name="test_src", filter_config=filter_cfg)

    # 1. Matching record
    matching_work = WorkEntity(
        title="Test Symphony",
        title_normalized="test symphony",
        work_type="music",
        original_language="fin",
        source="test_src",
        source_id="rec_1",
    )
    matching_manif = ManifestationEntity(
        title="Test Symphony",
        title_normalized="test symphony",
        publication_year=1990,
        source="test_src",
        source_id="rec_1",
    )
    matching_record = DecomposedRecord(works=[matching_work], manifestations=[matching_manif])
    added = acc.add_record(matching_record)
    assert added is True
    assert acc.pending == 1

    # 2. Non-matching work_type
    text_work = WorkEntity(
        title="Test Book",
        title_normalized="test book",
        work_type="text",
        original_language="fin",
        source="test_src",
        source_id="rec_2",
    )
    text_record = DecomposedRecord(works=[text_work], manifestations=[matching_manif])
    added_text = acc.add_record(text_record)
    assert added_text is False
    assert acc.pending == 1

    # 3. Non-matching year
    old_manif = ManifestationEntity(
        title="Old Symphony",
        title_normalized="old symphony",
        publication_year=1920,
        source="test_src",
        source_id="rec_3",
    )
    old_record = DecomposedRecord(works=[matching_work], manifestations=[old_manif])
    added_old = acc.add_record(old_record)
    assert added_old is False
    assert acc.pending == 1


def test_work_matcher_filtering_target_record() -> None:
    reconcile_cfg = ReconcileConfig(
        target_file=Path("test.xml"),
        target_work_types=["text"],
        target_languages=["fin"],
        from_year=1950,
    )
    matcher = WorkMatcher(wemi_repo=None, config=reconcile_cfg)  # type: ignore[arg-type]

    # Matching record
    _rec_ok = MatchRecord(
        record_id="target_1",
        format="marcxml",
        work_type="text",
        language="fin",
        year=1975,
        title="Valid Title",
        title_stem="valid title",
    )
    # Target that is filtered out by work_type
    rec_filtered_type = MatchRecord(
        record_id="target_2",
        format="marcxml",
        work_type="music",
        language="fin",
        year=1975,
        title="Music Score",
        title_stem="music score",
    )
    # Target that is filtered out by year
    rec_filtered_year = MatchRecord(
        record_id="target_3",
        format="marcxml",
        work_type="text",
        language="fin",
        year=1900,
        title="Old Book",
        title_stem="old book",
    )

    res_type = matcher.match_target(rec_filtered_type)
    assert res_type.match_tier == "skipped_filtered_work_type"
    assert res_type.confidence_score == 0.0

    res_year = matcher.match_target(rec_filtered_year)
    assert res_year.match_tier == "skipped_filtered_work_type"
    assert res_year.confidence_score == 0.0


def test_wikidata_filtering_via_config() -> None:
    # Test that WikidataIngestConfig with target_work_types filters entities in decompose_wikidata_entity
    wd_cfg = WikidataIngestConfig(
        target_work_types=["music"],
    )

    # Book entity (Q7725634 -> text)
    book_entity = {
        "id": "Q9001",
        "labels": {"fi": {"value": "Kirja"}},
        "claims": {
            "P31": [{"mainsnak": {"datavalue": {"value": {"id": "Q7725634"}}}}],
            "P407": [{"mainsnak": {"datavalue": {"value": {"id": "Q1412"}}}}],
        },
    }
    # Musical work entity (Q2188189 -> music)
    music_entity = {
        "id": "Q9002",
        "labels": {"fi": {"value": "Sinfonia"}},
        "claims": {
            "P31": [{"mainsnak": {"datavalue": {"value": {"id": "Q2188189"}}}}],
            "P407": [{"mainsnak": {"datavalue": {"value": {"id": "Q1412"}}}}],
        },
    }

    # Book should be rejected because config requires music
    decomp_book = decompose_wikidata_entity(book_entity, config=wd_cfg)
    assert decomp_book is None

    # Music should be accepted
    decomp_music = decompose_wikidata_entity(music_entity, config=wd_cfg)
    assert decomp_music is not None
    assert decomp_music[0].work_type == "music"
    assert decomp_music[0].source_id == "Q9002"

    # parse_wikidata_entity should also reject book when configured for music
    parsed_book = parse_wikidata_entity(book_entity, config=wd_cfg)
    assert parsed_book is None

    parsed_music = parse_wikidata_entity(music_entity, config=wd_cfg)
    assert parsed_music is not None
    assert parsed_music["work_type"] == "music"
    assert parsed_music["source_id"] == "Q9002"
