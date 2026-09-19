"""Tests for database-level FTS configuration and reconcile FTS fallback configuration."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from reconciliator.config import (
    ReconcileConfig,
    ReconcileFTSFallbackConfig,
    ReportSetPathsConfig,
    RepositoryFTSConfig,
    StorageConfig,
)
from reconciliator.indexer import TorchVectorEngine
from reconciliator.matcher import WorkMatcher
from reconciliator.models import AgentEntity, ExpressionEntity, MatchRecord, WorkEntity
from reconciliator.database import WEMIRepository


def test_fts_config_defaults_and_validation():
    # 1. RepositoryFTSConfig
    repo_cfg = RepositoryFTSConfig()
    assert repo_cfg.ranking_function == "bm25"
    assert repo_cfg.column_weights is None
    assert repo_cfg.default_limit == 20
    assert repo_cfg.prefix_search is False

    # Validation: invalid ranking function (fails identifier pattern)
    with pytest.raises(ValidationError):
        RepositoryFTSConfig(ranking_function="invalid algo!")

    # Valid custom ranking function and weights
    custom_repo_cfg = RepositoryFTSConfig(
        ranking_function="bm25",
        column_weights=[2.0, 1.0],
        default_limit=50,
        prefix_search=True,
    )
    assert custom_repo_cfg.column_weights == [2.0, 1.0]
    assert custom_repo_cfg.default_limit == 50
    assert custom_repo_cfg.prefix_search is True

    # 2. ReconcileFTSFallbackConfig
    rec_cfg = ReconcileFTSFallbackConfig()
    assert rec_cfg.enabled is True
    assert rec_cfg.limit == 5
    assert rec_cfg.confidence_score == 0.80

    # Validation: confidence score out of bounds
    with pytest.raises(ValidationError):
        ReconcileFTSFallbackConfig(confidence_score=1.5)
    with pytest.raises(ValidationError):
        ReconcileFTSFallbackConfig(confidence_score=-0.1)

    # Validation: limit < 1
    with pytest.raises(ValidationError):
        ReconcileFTSFallbackConfig(limit=0)

    # 3. StorageConfig integration
    storage_cfg = StorageConfig()
    assert isinstance(storage_cfg.fts, RepositoryFTSConfig)
    assert storage_cfg.fts.default_limit == 20

    dummy_reports = ReportSetPathsConfig(
        detailed_file=Path("detailed.tsv"),
        summary_file=Path("summary.tsv"),
    )
    reconcile_cfg = ReconcileConfig(
        target_file=Path("test.xml"),
        agent_reports=dummy_reports,
        subject_reports=dummy_reports,
    )
    assert isinstance(reconcile_cfg.fts_fallback, ReconcileFTSFallbackConfig)
    assert reconcile_cfg.fts_fallback.confidence_score == 0.80


@pytest.fixture
def test_repo(tmp_path: Path) -> WEMIRepository:
    db_path = tmp_path / "authority.db"
    w_idx = TorchVectorEngine(tmp_path / "work.pt", dimension=4)
    m_idx = TorchVectorEngine(tmp_path / "manif.pt", dimension=4)
    fts_cfg = RepositoryFTSConfig(
        ranking_function="bm25",
        column_weights=[1.0],
        default_limit=1,
        prefix_search=True,
    )
    return WEMIRepository(
        db_path=db_path,
        dimension=4,
        work_indexer=w_idx,
        manifestation_indexer=m_idx,
        fts_config=fts_cfg,
    )


def test_wemi_repository_fts_config(test_repo: WEMIRepository):
    # Insert two works
    w_ids = test_repo.insert_works_batch(
        [
            WorkEntity(
                work_uri="urn:work:1",
                source="test",
                source_id="w1",
                title="Tuntematon sotilas",
                title_normalized="tuntematon sotilas",
                work_type="text",
                serialized_text="Väinö Linna Tuntematon sotilas",
            ),
            WorkEntity(
                work_uri="urn:work:2",
                source="test",
                source_id="w2",
                title="Seitsemän veljestä",
                title_normalized="seitseman veljesta",
                work_type="text",
                serialized_text="Aleksis Kivi Seitsemän veljestä",
            ),
        ]
    )
    assert len(w_ids) == 2

    # 1. Test prefix_search: "Seitse" should match "Seitsemän" when prefix_search=True
    prefix_results = test_repo.search_works_fts("Seitse")
    assert len(prefix_results) == 1
    assert prefix_results[0][0].title == "Seitsemän veljestä"

    # 2. Test override prefix_search=False: "Seitse" without wildcard does not match
    no_prefix_results = test_repo.search_works_fts("Seitse", prefix_search=False)
    assert len(no_prefix_results) == 0

    # 3. Test default_limit configured as 1
    # Both works contain "monograph" in table or matching generic query if multiple matched
    multi_results = test_repo.search_works_fts("sotilas OR veljestä")
    assert len(multi_results) == 1  # default_limit is 1

    # Overriding limit
    overridden_results = test_repo.search_works_fts("sotilas OR veljestä", limit=5)
    assert len(overridden_results) == 2

    # 4. Test custom weights execution in SQL ORDER BY bm25(...)
    weighted_results = test_repo.search_works_fts("sotilas", weights=[2.0])
    assert len(weighted_results) == 1
    assert weighted_results[0][0].title == "Tuntematon sotilas"


def test_work_matcher_fts_fallback_config(tmp_path: Path):
    db_path = tmp_path / "authority.db"
    w_idx = TorchVectorEngine(tmp_path / "work.pt", dimension=4)
    m_idx = TorchVectorEngine(tmp_path / "manif.pt", dimension=4)
    repo = WEMIRepository(
        db_path=db_path,
        dimension=4,
        work_indexer=w_idx,
        manifestation_indexer=m_idx,
    )

    # Insert agent, work and expression
    agent_ids = repo.insert_agents_batch(
        [
            AgentEntity(
                agent_uri="urn:agent:sofi",
                source="test",
                name="Sofi Oksanen",
                name_normalized="sofi oksanen",
                surname="Oksanen",
                is_personal=True,
            )
        ]
    )
    sofi_id = agent_ids[0]

    w_ids = repo.insert_works_batch(
        [
            WorkEntity(
                work_uri="urn:work:100",
                source="test",
                source_id="w100",
                title="Puhdistus",
                title_normalized="puhdistus",
                primary_agent_id=sofi_id,
                work_type="text",
                serialized_text="Sofi Oksanen Puhdistus",
            )
        ]
    )
    work_id = w_ids[0]
    repo.insert_expressions_batch(
        [
            ExpressionEntity(
                expression_uri="urn:exp:100",
                work_id=work_id,
                source="test",
                source_id="e100",
                language="fin",
                content_type="text",
            )
        ]
    )

    # Target has matching title stem "Puhdistus", but different author_key "Virtanen".
    # Tier 1 (IDs) fails.
    # Tier 2 (Author+Title stem) fails because author surname/name does not match Sofi Oksanen.
    # Tier 3 (FTS Lexical Search) matches "Puhdistus" in PostgreSQL full-text index.
    target = MatchRecord(
        record_id="rec1",
        format="marcxml",
        title="Puhdistus",
        title_stem="Puhdistus",
        author_key="Virtanen",
        language="fin",
    )

    dummy_reports = ReportSetPathsConfig(
        detailed_file=Path("detailed.tsv"),
        summary_file=Path("summary.tsv"),
    )

    # Case 1: fts_fallback disabled
    cfg_disabled = ReconcileConfig(
        target_file=Path("test.xml"),
        agent_reports=dummy_reports,
        subject_reports=dummy_reports,
        fts_fallback=ReconcileFTSFallbackConfig(enabled=False),
    )
    matcher_disabled = WorkMatcher(wemi_repo=repo, config=cfg_disabled)
    res_disabled = matcher_disabled.match_target(target)
    assert res_disabled.match_tier == "unmatched"
    assert res_disabled.matched_work is None

    # Case 2: fts_fallback enabled with custom confidence_score
    cfg_enabled = ReconcileConfig(
        target_file=Path("test.xml"),
        agent_reports=dummy_reports,
        subject_reports=dummy_reports,
        fts_fallback=ReconcileFTSFallbackConfig(
            enabled=True,
            limit=2,
            confidence_score=0.88,
        ),
    )
    matcher_enabled = WorkMatcher(wemi_repo=repo, config=cfg_enabled)
    res_enabled = matcher_enabled.match_target(target)
    assert res_enabled is not None
    assert res_enabled.match_tier == "tier3_fts_fallback"
    assert res_enabled.confidence_score == 0.88
    assert res_enabled.matched_work.work_uri == "test:w100"
    assert res_enabled.matched_expression is not None
    assert res_enabled.matched_expression.expression_uri == "test:e100"
