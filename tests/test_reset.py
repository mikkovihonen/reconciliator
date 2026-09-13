"""Unit tests for reset mechanics and schema reinitialization."""

from pathlib import Path

from typer.testing import CliRunner

from reconciliator.cli import app
from reconciliator.config import StorageConfig
from reconciliator.db import get_db_connection, init_authority_db, reset_all_storage


def test_reset_all_storage(tmp_path: Path) -> None:
    storage = StorageConfig(
        data_dir=tmp_path,
        authority_db=tmp_path / "auth.db",
        vocab_db=tmp_path / "vocab.db",
        progress_db=tmp_path / "prog.db",
        corpus_db=tmp_path / "corpus.db",
        index_path=tmp_path / "auth.faiss",
    )

    # Initialize and populate a test row
    with get_db_connection(storage.authority_db) as conn:
        init_authority_db(conn)
        conn.execute("INSERT INTO db_metadata (key, value) VALUES ('test', '123');")

    storage.index_path.write_bytes(b"dummy index content")

    # Perform full reset
    reset_all_storage(storage)

    # Verify files exist and fresh schema is empty
    assert storage.authority_db.exists()
    assert storage.vocab_db.exists()
    assert storage.progress_db.exists()
    assert not storage.index_path.exists()

    with get_db_connection(storage.authority_db) as conn:
        row = conn.execute("SELECT * FROM db_metadata WHERE key = 'test';").fetchone()
        assert row is None


def test_cli_reset_all_flag(tmp_path: Path) -> None:
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        f"""
storage:
  data_dir: "{tmp_path}"
  authority_db: "{tmp_path}/cli_auth.db"
  vocab_db: "{tmp_path}/cli_vocab.db"
  progress_db: "{tmp_path}/cli_prog.db"
  corpus_db: "{tmp_path}/cli_corpus.db"
  index_path: "{tmp_path}/cli_auth.faiss"
""",
        encoding="utf-8",
    )

    runner = CliRunner()
    result = runner.invoke(app, ["reset", "--config", str(cfg_file), "--all", "--yes"])
    assert result.exit_code == 0
    assert "Successfully reset specified storage" in result.output
    assert (tmp_path / "cli_auth.db").exists()
    assert (tmp_path / "cli_vocab.db").exists()
    assert (tmp_path / "cli_prog.db").exists()


def test_cli_reset_vocab_flag(tmp_path: Path) -> None:
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        f"""
storage:
  data_dir: "{tmp_path}"
  authority_db: "{tmp_path}/cli_auth.db"
  vocab_db: "{tmp_path}/cli_vocab.db"
  progress_db: "{tmp_path}/cli_prog.db"
  corpus_db: "{tmp_path}/cli_corpus.db"
  index_path: "{tmp_path}/cli_auth.faiss"
""",
        encoding="utf-8",
    )

    runner = CliRunner()
    result = runner.invoke(app, ["reset", "--config", str(cfg_file), "--vocab", "--yes"])
    assert result.exit_code == 0
    assert "Successfully reset specified storage" in result.output
    assert (tmp_path / "cli_vocab.db").exists()


def test_cli_reset_corpus_flag(tmp_path: Path) -> None:
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        f"""
storage:
  data_dir: "{tmp_path}"
  authority_db: "{tmp_path}/cli_auth.db"
  vocab_db: "{tmp_path}/cli_vocab.db"
  progress_db: "{tmp_path}/cli_prog.db"
  corpus_db: "{tmp_path}/cli_corpus.db"
  index_path: "{tmp_path}/cli_auth.faiss"
""",
        encoding="utf-8",
    )

    runner = CliRunner()
    result = runner.invoke(app, ["reset", "--config", str(cfg_file), "--corpus", "--yes"])
    assert result.exit_code == 0
    assert "Successfully reset specified storage" in result.output
    assert (tmp_path / "cli_corpus.db").exists()


def test_cli_reset_daemon_flag(tmp_path: Path, monkeypatch) -> None:
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        f"""
storage:
  data_dir: "{tmp_path}"
  authority_db: "{tmp_path}/cli_auth.db"
  vocab_db: "{tmp_path}/cli_vocab.db"
  progress_db: "{tmp_path}/cli_prog.db"
  corpus_db: "{tmp_path}/cli_corpus.db"
  index_path: "{tmp_path}/cli_auth.faiss"
""",
        encoding="utf-8",
    )

    called_reset_all = {}

    def mock_reset_all_storage(**kwargs):
        called_reset_all.update(kwargs)

    monkeypatch.setattr("reconciliator.cli.reset_all_storage", mock_reset_all_storage)

    runner = CliRunner()
    result = runner.invoke(app, ["reset", "--config", str(cfg_file), "--daemon", "--yes"])
    assert result.exit_code == 0
    assert "Successfully reset specified storage" in result.output
    assert called_reset_all.get("reset_daemon") is True
    assert called_reset_all.get("reset_authority") is False


def test_reset_all_storage_daemon_shutdown(tmp_path: Path, monkeypatch) -> None:
    storage = StorageConfig(
        data_dir=tmp_path,
        authority_db=tmp_path / "auth.db",
        vocab_db=tmp_path / "vocab.db",
        progress_db=tmp_path / "prog.db",
        corpus_db=tmp_path / "corpus.db",
        index_path=tmp_path / "auth.faiss",
    )

    shutdown_calls = []

    class MockLifecycle:
        def __init__(self, sock_path):
            self.sock_path = sock_path

        def is_running(self):
            return True

        def shutdown(self, save: bool = True):
            shutdown_calls.append(save)
            return True

    monkeypatch.setattr("reconciliator.indexer.ipc.client.DaemonLifecycleManager", MockLifecycle)

    reset_all_storage(storage, reset_daemon=True)
    assert shutdown_calls == [False]


