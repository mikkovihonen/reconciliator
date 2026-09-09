"""Unit tests for configuration loading and Pydantic validation."""

from pathlib import Path
import pytest
from reconciliator.config import (
    AppConfig,
    EmbeddingConfig,
    ShortCircuitConfig,
    SparseRerankConfig,
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
    assert config.reconcile.sparse_rerank.enabled is True
    assert config.reconcile.sparse_rerank.dense_weight == 0.6
    assert config.reconcile.sparse_rerank.sparse_weight == 0.4
    assert config.reconcile.sparse_rerank.short_circuit.threshold == 0.98


def test_missing_config_raises_file_not_found() -> None:
    with pytest.raises(FileNotFoundError):
        load_config(Path("non_existent_config.yaml"))


def test_sparse_rerank_weights_must_sum_to_one() -> None:
    with pytest.raises(ValueError, match="must sum to 1.0"):
        SparseRerankConfig(dense_weight=0.7, sparse_weight=0.5)


def test_short_circuit_threshold_range() -> None:
    with pytest.raises(ValueError, match="must be between 0.0 and 1.0"):
        ShortCircuitConfig(threshold=1.5)

    with pytest.raises(ValueError, match="must be between 0.0 and 1.0"):
        ShortCircuitConfig(threshold=-0.1)


def test_embedding_batch_size_positive() -> None:
    with pytest.raises(ValueError, match="batch_size must be a positive integer"):
        EmbeddingConfig(batch_size=0)

