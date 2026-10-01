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
  database_url: "sqlite:///{tmp_path}/test.db"

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
  database_url: "sqlite:///{tmp_path}/test.db"

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


def test_cli_cluster_corpus_bigclust_routing(tmp_path: Path) -> None:
    """Verify that cluster-corpus with --method bigclust invokes BigClustMultiplexClusterer."""
    from unittest.mock import MagicMock

    from reconciliator.models import MultiplexClusterResult

    runner = CliRunner()
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        f"""
storage:
  data_dir: "{tmp_path}"
"""
    )

    mock_res = MultiplexClusterResult(
        work_clusters_count=3,
        expression_clusters_count=5,
        manifestation_clusters_count=8,
        records_clustered=8,
        elapsed_seconds=0.12,
    )

    with patch("reconciliator.commands.wemi._wemi_repo") as mock_repo_fn, \
         patch("reconciliator.embedding.create_vector_embedder") as mock_embedder_fn, \
         patch("reconciliator.clustering.bigclust.BigClustMultiplexClusterer") as mock_bigclust_cls:
        mock_embedder = MagicMock()
        mock_embedder_fn.return_value = mock_embedder
        mock_repo = MagicMock()
        mock_repo.get_all_match_records.return_value = [MagicMock()]
        mock_repo.non_authoritative_sources.return_value = ["test_corpus"]
        mock_repo_fn.return_value = mock_repo

        mock_instance = MagicMock()
        mock_instance.cluster_records.return_value = mock_res
        mock_bigclust_cls.return_value = mock_instance

        result = runner.invoke(
            app,
            [
                "cluster-corpus",
                "--config",
                str(cfg_file),
                "--method",
                "bigclust",
                "--max-work-clusters",
                "25",
                "--max-expression-clusters",
                "5",
                "--dry-run",
            ],
        )
        assert result.exit_code == 0, result.output
        assert "Evaluated 8 staged record(s) into 3 Work cluster(s)" in result.output
        mock_bigclust_cls.assert_called_once()
        assert mock_bigclust_cls.call_args.kwargs["max_work_clusters"] == 25
        assert mock_bigclust_cls.call_args.kwargs["max_expression_clusters_per_work"] == 5
        mock_instance.cluster_records.assert_called_once_with(
            mock_repo.get_all_match_records.return_value, persist=False
        )

