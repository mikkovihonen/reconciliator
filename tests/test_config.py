import logging
from pathlib import Path

import pytest

from reconciliator.config import (
    AppConfig,
    EmbeddingConfig,
    LoggingConfig,
    RerankConfig,
    ShortCircuitConfig,
    configure_logging,
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
    assert cfg.slice_days == 30
    assert cfg.batch_size == 500
    assert cfg.batch_queue_size == 4

    custom = OAIIngestConfig(
        workers=8,
        slice_days=7,
        batch_size=250,
        batch_queue_size=8,
    )
    assert custom.workers == 8
    assert custom.slice_days == 7
    assert custom.batch_size == 250
    assert custom.batch_queue_size == 8

    with pytest.raises(ValueError, match="workers must be at least 1"):
        OAIIngestConfig(workers=0)

    with pytest.raises(ValueError, match="batch_queue_size must be at least 1"):
        OAIIngestConfig(batch_queue_size=0)

    with pytest.raises(ValueError, match="slice_days must be at least 1"):
        OAIIngestConfig(slice_days=0)

    with pytest.raises(ValueError, match="batch_size must be at least 1"):
        OAIIngestConfig(batch_size=0)


def test_logging_config_defaults_and_validation() -> None:
    cfg = LoggingConfig()
    assert cfg.level == "INFO"
    assert cfg.log_file is None
    assert cfg.capture_warnings is False
    assert "%(levelname)s" in cfg.format
    assert "%Y-%m-%d" in cfg.date_format

    # Case-insensitive normalization
    cfg_debug = LoggingConfig(level="debug")  # type: ignore[arg-type]
    assert cfg_debug.level == "DEBUG"

    cfg_warn = LoggingConfig(level="warn")  # type: ignore[arg-type]
    assert cfg_warn.level == "WARNING"

    with pytest.raises(ValueError):
        LoggingConfig(level="INVALID_LEVEL")  # type: ignore[arg-type]


def test_logging_config_yaml_loading_and_path_resolution(tmp_path: Path) -> None:
    cfg_file = tmp_path / "app_config.yaml"
    cfg_file.write_text(
        """
storage:
  data_dir: "data"
logging:
  level: "DEBUG"
  format: "[%(levelname)s] %(name)s: %(message)s"
  date_format: "%H:%M:%S"
  log_file: "logs/reconciliator.log"
  capture_warnings: true
""",
        encoding="utf-8",
    )

    loaded = load_config(cfg_file)
    assert loaded.logging.level == "DEBUG"
    assert loaded.logging.format == "[%(levelname)s] %(name)s: %(message)s"
    assert loaded.logging.date_format == "%H:%M:%S"
    assert loaded.logging.capture_warnings is True
    assert loaded.logging.log_file is not None
    assert loaded.logging.log_file.is_absolute()
    assert loaded.logging.log_file == (tmp_path / "logs" / "reconciliator.log").resolve()


def test_configure_logging(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    log_file = tmp_path / "test_output.log"
    cfg = LoggingConfig(
        level="DEBUG",
        format="[TEST_LOG] %(message)s",
        log_file=log_file,
    )

    try:
        configure_logging(cfg)
        test_logger = logging.getLogger("test_subsystem")
        test_logger.debug("Hello debug logging world")

        # Root level verified
        assert logging.getLogger().level == logging.DEBUG

        # Log file written
        assert log_file.exists()
        content = log_file.read_text(encoding="utf-8")
        assert "[TEST_LOG] Hello debug logging world" in content

        # Pytest caplog handler preserved
        with caplog.at_level(logging.DEBUG):
            test_logger.debug("Testing caplog preservation")
            assert "Testing caplog preservation" in caplog.text
    finally:
        # Reset logging back to clean state
        configure_logging(LoggingConfig(level="INFO"))
