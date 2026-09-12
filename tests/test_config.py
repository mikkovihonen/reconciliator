"""Unit tests for configuration loading and Pydantic validation."""

from pathlib import Path

import pytest

from reconciliator.config import (
    AppConfig,
    EmbeddingConfig,
    RerankConfig,
    ShortCircuitConfig,
    load_config,
)


def test_load_sample_config() -> None:
    sample_path = Path(__file__).parent / "fixtures" / "sample_config.yaml"
    config = load_config(sample_path)

    assert isinstance(config, AppConfig)
    assert config.storage.authority_db.name == "test_authority.db"
    assert config.storage.authority_db.is_absolute()
    assert config.embedding.device == "cpu"
    assert config.embedding.batch_size == 16
    assert config.reconcile is not None
    assert config.reconcile.threshold == 0.85
    assert config.reconcile.rerank.enabled is True
    assert config.reconcile.rerank.engine == "bm25"
    assert config.reconcile.rerank.dense_weight == 0.6
    assert config.reconcile.rerank.short_circuit.threshold == 0.98
    assert "melinda" in config.ingest_oai
    assert config.ingest_oai["melinda"].source_type == "file"
    assert config.ingest_oai["melinda"].batch_size == 10
    assert config.ingest_oai["melinda"].input_path is not None
    assert config.ingest_oai["melinda"].input_path.is_absolute()


def test_missing_config_raises_file_not_found() -> None:
    with pytest.raises(FileNotFoundError):
        load_config(Path("non_existent_config.yaml"))


def test_rerank_weights_must_sum_to_one() -> None:
    with pytest.raises(ValueError, match="must sum to 1.0"):
        RerankConfig(dense_weight=0.7, rerank_weight=0.5)


def test_short_circuit_threshold_range() -> None:
    with pytest.raises(ValueError, match="must be between 0.0 and 1.0"):
        ShortCircuitConfig(threshold=1.5)

    with pytest.raises(ValueError, match="must be between 0.0 and 1.0"):
        ShortCircuitConfig(threshold=-0.1)


def test_embedding_batch_size_positive() -> None:
    with pytest.raises(ValueError, match="batch_size must be a positive integer"):
        EmbeddingConfig(batch_size=0)


def test_oai_ingest_config_workers_and_queue_defaults_and_validation() -> None:
    from reconciliator.config import OAIIngestConfig

    cfg = OAIIngestConfig()
    assert cfg.workers == 4
    assert cfg.normalization_workers == 4
    assert cfg.queue_size is None
    assert cfg.batch_queue_size == 4

    custom = OAIIngestConfig(
        workers=8,
        normalization_workers=6,
        queue_size=5000,
        batch_queue_size=8,
    )
    assert custom.workers == 8
    assert custom.normalization_workers == 6
    assert custom.queue_size == 5000
    assert custom.batch_queue_size == 8

    with pytest.raises(ValueError, match="workers must be at least 1"):
        OAIIngestConfig(workers=0)

    with pytest.raises(ValueError, match="normalization_workers must be at least 1"):
        OAIIngestConfig(normalization_workers=0)

    with pytest.raises(ValueError, match="batch_queue_size must be at least 1"):
        OAIIngestConfig(batch_queue_size=0)

    with pytest.raises(ValueError, match="queue_size must be at least 1"):
        OAIIngestConfig(queue_size=0)
