"""Unit tests for CLI dual-routing and configuration handling in WEMI pipelines."""

from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from reconciliator.commands import app
from reconciliator.database import WEMIRepository


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

ingestion:
  wikidata:
    dump_path: "{dump_file}"
    extract_works: true
"""
    )

    with patch("reconciliator.commands.ingest.ingest_wikidata_pipeline") as mock_pipeline:
        mock_pipeline.return_value = 42
        result = runner.invoke(app, ["ingest-wikidata", "--config", str(cfg_file)])
        assert result.exit_code == 0
        assert mock_pipeline.called
        # The second positional argument should be a WEMIRepository instance
        repo_arg = mock_pipeline.call_args[0][1]
        from urllib.parse import unquote

        from reconciliator.config import load_config

        conf = load_config(cfg_file)
        assert unquote(repo_arg.db_path) == unquote(conf.storage.database_url)


def test_every_source_routes_to_the_single_authority_database(tmp_path: Path) -> None:
    """There is one database, and `authoritative` is the only per-source switch.

    This replaces two tests that set `target_database` to "corpus" and
    "authority" and then asserted the same db_path in both - which they would
    have done with the field removed, because nothing had read it since the
    staging tables went. The field is gone; what actually distinguishes a corpus
    from an authority is `authoritative`, and both land in the same store.
    """
    runner = CliRunner()
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        f"""
storage:
  data_dir: "{tmp_path}"

embedding:
  model_name: "TurkuNLP/sbert-cased-finnish-paraphrase"
  device: "cpu"

ingestion:
  oai:
    an_authority:
      enabled: true
      source_type: "oai"
      oai_url: "https://example.org/oai"
      authoritative: true
    a_corpus:
      enabled: true
      source_type: "oai"
      oai_url: "https://example.org/oai"
      authoritative: false
"""
    )

    for source, expected_authoritative in (("an_authority", True), ("a_corpus", False)):
        with patch("reconciliator.commands.ingest.ingest_oai_pipeline") as mock_pipeline:
            mock_pipeline.return_value = 1
            result = runner.invoke(
                app, ["ingest-oai", "--config", str(cfg_file), "--source", source]
            )
            assert result.exit_code == 0, result.output
            store = mock_pipeline.call_args.kwargs.get("store")
            assert isinstance(store, WEMIRepository)
            from urllib.parse import unquote

            from reconciliator.config import load_config

            conf = load_config(cfg_file)
            assert unquote(store.db_path) == unquote(conf.storage.database_url)
            passed_config = mock_pipeline.call_args.kwargs.get("config")
            assert passed_config.authoritative is expected_authoritative
