"""Unit tests for CLI dual-routing and configuration handling in WEMI pipelines."""

from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from reconciliator.cli import app
from reconciliator.repository import WEMIRepository


def test_cli_ingest_wikidata_wemi_routing(tmp_path: Path) -> None:
    """Verify that extract_works=true routes to WEMIRepository."""
    runner = CliRunner()
    cfg_file = tmp_path / "config.yaml"
    dump_file = tmp_path / "dump.json.gz"
    dump_file.write_bytes(b"")

    cfg_file.write_text(
        f"""
storage:
  data_dir: "{tmp_path}"

ingest_wikidata:
  dump_path: "{dump_file}"
  extract_works: true
"""
    )

    with patch("reconciliator.cli.ingest_wikidata_pipeline") as mock_pipeline:
        mock_pipeline.return_value = 42
        result = runner.invoke(app, ["ingest-wikidata", "--config", str(cfg_file)])
        assert result.exit_code == 0
        assert mock_pipeline.called
        # The second positional argument should be a WEMIRepository instance
        repo_arg = mock_pipeline.call_args[0][1]
        assert isinstance(repo_arg, WEMIRepository)
        assert repo_arg.db_path == tmp_path / "authority.db"


def test_cli_ingest_oai_corpus_routing(tmp_path: Path) -> None:
    """Verify that target_database='corpus' routes to unified WEMIRepository."""
    runner = CliRunner()
    cfg_file = tmp_path / "config.yaml"

    cfg_file.write_text(
        f"""
storage:
  data_dir: "{tmp_path}"

embedding:
  model_name: "TurkuNLP/sbert-cased-finnish-paraphrase"
  device: "cpu"

ingest_oai:
  test_target:
    enabled: true
    source_type: "oai"
    oai_url: "https://example.org/oai"
    target_database: "corpus"
"""
    )

    with patch("reconciliator.cli.SentenceEmbedder"), patch("reconciliator.cli.ingest_oai_pipeline") as mock_pipeline:
        mock_pipeline.return_value = 10
        result = runner.invoke(
            app, ["ingest-oai", "--config", str(cfg_file), "--source", "test_target"]
        )
        assert result.exit_code == 0
        assert mock_pipeline.called
        store_kwarg = mock_pipeline.call_args.kwargs.get("store")
        assert isinstance(store_kwarg, WEMIRepository)
        assert store_kwarg.db_path == tmp_path / "authority.db"


def test_cli_ingest_oai_authority_routing(tmp_path: Path) -> None:
    """Verify that target_database='authority' routes to WEMIRepository."""
    runner = CliRunner()
    cfg_file = tmp_path / "config.yaml"

    cfg_file.write_text(
        f"""
storage:
  data_dir: "{tmp_path}"

embedding:
  model_name: "TurkuNLP/sbert-cased-finnish-paraphrase"
  device: "cpu"

ingest_oai:
  test_auth:
    enabled: true
    source_type: "oai"
    oai_url: "https://example.org/oai"
    target_database: "authority"
"""
    )

    with patch("reconciliator.cli.SentenceEmbedder"), patch("reconciliator.cli.ingest_oai_pipeline") as mock_pipeline:
        mock_pipeline.return_value = 15
        result = runner.invoke(
            app, ["ingest-oai", "--config", str(cfg_file), "--source", "test_auth"]
        )
        assert result.exit_code == 0
        assert mock_pipeline.called
        store_kwarg = mock_pipeline.call_args.kwargs.get("store")
        assert isinstance(store_kwarg, WEMIRepository)
        assert store_kwarg.db_path == tmp_path / "authority.db"
