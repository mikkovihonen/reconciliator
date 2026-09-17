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
        work_index=tmp_path / "auth.faiss",
    )

    # Initialize and populate a test row
    with get_db_connection(storage.authority_db) as conn:
        init_authority_db(conn)
        conn.exec_driver_sql("INSERT INTO db_metadata (key, value) VALUES ('test', '123');")

    storage.work_index.write_bytes(b"dummy index content")

    # Perform full reset
    reset_all_storage(storage)

    assert not storage.work_index.exists()

    with get_db_connection(storage.database_url) as conn:
        row = conn.exec_driver_sql("SELECT * FROM db_metadata WHERE key = 'test';").fetchone()
        assert row is None
        tables = {
            r[0]
            for r in conn.exec_driver_sql(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema();"
            ).fetchall()
        }
        assert {"works", "manifestations", "db_metadata"} <= tables


def test_reset_all_storage_daemon_shutdown(tmp_path: Path, monkeypatch) -> None:
    storage = StorageConfig(
        data_dir=tmp_path,
        authority_db=tmp_path / "auth.db",
        vocab_db=tmp_path / "vocab.db",
        progress_db=tmp_path / "prog.db",
        corpus_db=tmp_path / "corpus.db",
        work_index=tmp_path / "auth.faiss",
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

    reset_all_storage(storage)
    assert shutdown_calls == [False]


def test_cli_reset_clears_every_store(tmp_path: Path) -> None:
    """Reset is all or nothing: every database and index is rebuilt clean."""
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        f"""
storage:
  data_dir: "{tmp_path}"
  authority_db: "{tmp_path}/cli_auth.db"
  vocab_db: "{tmp_path}/cli_vocab.db"
  progress_db: "{tmp_path}/cli_prog.db"
  work_index: "{tmp_path}/cli_auth.faiss"
""",
        encoding="utf-8",
    )

    # Seed every store with content that must not survive.
    for name in ("cli_auth.db", "cli_vocab.db", "cli_prog.db"):
        (tmp_path / name).write_bytes(b"stale")
    (tmp_path / "cli_auth.faiss").write_bytes(b"stale index")

    runner = CliRunner()
    result = runner.invoke(app, ["reset", "--config", str(cfg_file), "--yes"])
    assert result.exit_code == 0, result.output
    assert "Successfully reset all storage" in result.output

    from reconciliator.config import load_config

    conf = load_config(cfg_file)
    with get_db_connection(conf.storage.database_url) as conn:
        tables = {
            r[0]
            for r in conn.exec_driver_sql(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema();"
            ).fetchall()
        }
    assert {"works", "entity_keys", "corpus_work_clusters", "sources"} <= tables

    # ...and the stale vector index is gone rather than left pointing at deleted rows.
    assert not (tmp_path / "cli_auth.faiss").exists()


def test_corpus_staging_shares_the_authority_database(tmp_path: Path) -> None:
    """One database, so reset cannot clear a corpus the repository is not using.

    Corpus staging used to live in its own file whose name the repository derived
    itself, while reset cleared the configured one. Staged records consequently
    survived a reset and leaked into the next corpus run. There is no staging
    table at all now: a corpus is ordinary WEMI rows carrying its own
    record_origin, with its blocking keys in `entity_keys`.
    """
    from reconciliator.config import StorageConfig
    from reconciliator.repository import WEMIRepository

    storage = StorageConfig(data_dir=tmp_path)
    repo = WEMIRepository(storage.database_url)
    assert repo.corpus_db_path == repo.db_path

    # Corpus and WEMI tables are reachable over the same connection, so a cluster
    # can be joined to the works it resolves to.
    with repo.get_connection() as conn:
        tables = {
            r[0]
            for r in conn.exec_driver_sql(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema();"
            ).fetchall()
        }
    assert {"works", "entity_keys", "corpus_work_clusters", "sources"} <= tables
