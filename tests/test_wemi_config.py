"""Unit tests for WEMI configuration schemas, template paths, thresholds, and defaults."""

from pathlib import Path

from reconciliator.config import (
    ClusterConfig,
    EnrichmentTogglesConfig,
    MultiplexClusterThresholds,
    OAIIngestConfig,
    ReconcileConfig,
    ReportSetPathsConfig,
    StorageConfig,
    TemplateSetPathsConfig,
    WikidataIngestConfig,
)


def test_template_set_paths_config():
    paths = TemplateSetPathsConfig()
    assert paths.work == Path("reconciliator/templates/work_serialization.j2")
    assert paths.expression == Path("reconciliator/templates/expression_serialization.j2")
    assert paths.manifestation == Path("reconciliator/templates/manifestation_serialization.j2")

    custom = TemplateSetPathsConfig(
        work=Path("custom/work.j2"),
        expression=Path("custom/exp.j2"),
        manifestation=Path("custom/manif.j2"),
    )
    assert custom.work == Path("custom/work.j2")
    assert custom.expression == Path("custom/exp.j2")


def test_multiplex_cluster_thresholds():
    thresholds = MultiplexClusterThresholds()
    assert thresholds.work == 0.85
    assert thresholds.expression == 0.88
    assert thresholds.manifestation == 0.92

    custom_thresh = MultiplexClusterThresholds(work=0.80, expression=0.85, manifestation=0.90)
    assert custom_thresh.work == 0.80


def test_cluster_config():
    cfg = ClusterConfig()
    assert cfg.similarity_threshold == 0.88
    assert cfg.thresholds.work == 0.85
    assert cfg.max_cluster_size == 500
    assert cfg.message_passing is True
    assert cfg.reconcile_items is False


def test_storage_config_wemi():
    storage = StorageConfig()
    assert storage.authority_db == Path("data/authority.db")
    # Corpus staging shares authority.db; there is no separate corpus file.
    assert storage.work_index == Path("data/work.index")
    assert storage.expression_index == Path("data/expression.index")
    assert storage.manifestation_index == Path("data/manifestation.index")


def test_enrichment_toggles_config():
    toggles = EnrichmentTogglesConfig()
    assert toggles.inject_work_links is True
    assert toggles.inject_expression_links is True
    assert toggles.inject_hub_links is True
    assert toggles.inject_same_as is True


def test_oai_and_wikidata_ingest_config_extensions():
    oai = OAIIngestConfig(
        endpoint="https://example.org/oai",
        metadata_prefix="marc21",
        include_items=True,
        target_database=Path("data/custom_corpus.db"),
    )
    assert oai.include_items is True
    assert oai.target_database == Path("data/custom_corpus.db")

    wikidata = WikidataIngestConfig(
        dump_path=Path("dumps/latest.json.gz"),
        pass1_only=True,
        extract_works=True,
    )
    assert wikidata.pass1_only is True
    assert wikidata.extract_works is True


def test_reconcile_config_with_wemi():
    cfg = ReconcileConfig(
        target_file=Path("data/target.xml"),
        agent_reports=ReportSetPathsConfig(
            detailed_file=Path("reports/agent_detailed.tsv"),
            summary_file=Path("reports/agent_summary.tsv"),
        ),
        subject_reports=ReportSetPathsConfig(
            detailed_file=Path("reports/subj_detailed.tsv"),
            summary_file=Path("reports/subj_summary.tsv"),
        ),
        templates=TemplateSetPathsConfig(),
        cluster=ClusterConfig(),
        enrichment_toggles=EnrichmentTogglesConfig(),
    )
    assert cfg.templates is not None
    assert cfg.templates.work == Path("reconciliator/templates/work_serialization.j2")
    assert cfg.cluster.thresholds.work == 0.85
    assert cfg.enrichment_toggles.inject_work_links is True
    assert cfg.work_reports is None
    assert cfg.expression_reports is None
