"""Integration tests for multi-source OAI-PMH harvesting, ingestion, and CLI commands."""

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import numpy as np
import pytest
import typer
from typer.testing import CliRunner

from reconciliator.cli import app, ingest_oai_cmd
from reconciliator.config import AppConfig, OAIIngestConfig
from reconciliator.embedder import SentenceEmbedder
from reconciliator.finto_folding import FintoFolder
from reconciliator.finto_store import FintoStore
from reconciliator.ingestion.oai import ingest_oai_pipeline
from reconciliator.normalizers.record import RecordNormalizer
from reconciliator.repository import AuthorityStore

SAMPLE_OAI_SOURCE_A = b"""<?xml version="1.0" encoding="UTF-8"?>
<OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/">
  <ListRecords>
    <record>
      <header>
        <identifier>oai:source_a:rec_100</identifier>
      </header>
      <metadata>
        <record xmlns="http://www.loc.gov/MARC21/slim">
          <controlfield tag="001">100</controlfield>
          <controlfield tag="003">SRC-A</controlfield>
          <datafield tag="020" ind1=" " ind2=" ">
            <subfield code="a">9789510000010</subfield>
          </datafield>
          <datafield tag="245" ind1="1" ind2="0">
            <subfield code="a">Source A Book Title</subfield>
          </datafield>
          <datafield tag="100" ind1="1" ind2=" ">
            <subfield code="a">Author Alpha,</subfield>
            <subfield code="0">https://urn.fi/URN:NBN:fi:au:pn:000111111</subfield>
          </datafield>
        </record>
      </metadata>
    </record>
  </ListRecords>
</OAI-PMH>
"""

SAMPLE_OAI_SOURCE_B = b"""<?xml version="1.0" encoding="UTF-8"?>
<OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/">
  <ListRecords>
    <record>
      <header>
        <identifier>oai:source_b:item_200</identifier>
      </header>
      <metadata>
        <record xmlns="http://www.loc.gov/MARC21/slim">
          <controlfield tag="001">200</controlfield>
          <controlfield tag="003">SRC-B</controlfield>
          <datafield tag="020" ind1=" " ind2=" ">
            <subfield code="a">9789510000027</subfield>
          </datafield>
          <datafield tag="245" ind1="1" ind2="0">
            <subfield code="a">Source B Book Title</subfield>
          </datafield>
          <datafield tag="100" ind1="1" ind2=" ">
            <subfield code="a">Author Beta,</subfield>
            <subfield code="0">https://urn.fi/URN:NBN:fi:au:pn:000222222</subfield>
          </datafield>
        </record>
      </metadata>
    </record>
  </ListRecords>
</OAI-PMH>
"""


def test_multi_source_ingestion_pipeline(tmp_path: Path) -> None:
    db_path = tmp_path / "authority.db"
    index_path = tmp_path / "authority.faiss"
    dim = 64

    store = AuthorityStore(db_path, index_path, dimension=dim)
    finto_store = FintoStore(tmp_path / "finto.db")
    finto_folder = FintoFolder(finto_store)

    mock_model = MagicMock()
    mock_model.get_embedding_dimension.return_value = dim
    mock_model.get_sentence_embedding_dimension.return_value = dim
    mock_model.encode.side_effect = lambda texts, **kwargs: np.ones(
        (len(texts), dim), dtype=np.float32
    )

    template_path = (
        Path(__file__).parent.parent / "src" / "reconciliator" / "templates" / "default_serialization.j2"
    )

    with patch("reconciliator.embedder.SentenceTransformer", return_value=mock_model):
        from reconciliator.config import EmbeddingConfig

        emb_cfg = EmbeddingConfig(template_path=template_path, device="cpu", batch_size=10)
        embedder = SentenceEmbedder(emb_cfg)
        normalizer = RecordNormalizer(
            finto_folder=finto_folder, template_engine=embedder.template_engine
        )

        cfg_a = OAIIngestConfig(
            source_type="oai",
            oai_url="https://source-a.example.org/oai",
            workers=1,
            batch_size=10,
        )
        cfg_b = OAIIngestConfig(
            source_type="oai",
            oai_url="https://source-b.example.org/oai",
            workers=1,
            batch_size=10,
        )

        def mock_get(url: str, params: dict[str, str], **kwargs: Any) -> MagicMock:
            resp = MagicMock()
            resp.raise_for_status = MagicMock()
            if "source-a" in url:
                resp.content = SAMPLE_OAI_SOURCE_A
            else:
                resp.content = SAMPLE_OAI_SOURCE_B
            return resp

        with patch("httpx2.Client.get", side_effect=mock_get):
            # Ingest source A
            ingested_a = ingest_oai_pipeline(
                source_name="catalog_a",
                config=cfg_a,
                store=store,
                normalizer=normalizer,
                embedder=embedder,
            )
            assert ingested_a == 1

            # Ingest source B
            ingested_b = ingest_oai_pipeline(
                source_name="catalog_b",
                config=cfg_b,
                store=store,
                normalizer=normalizer,
                embedder=embedder,
            )
            assert ingested_b == 1

    # Verify both records exist with their respective source names
    with store.get_connection() as conn:
        rows = conn.execute("SELECT source, external_id, title FROM authority_records ORDER BY id;").fetchall()
        assert len(rows) == 2
        assert rows[0]["source"] == "catalog_a"
        assert rows[0]["title"] == "Source A Book Title"
        assert rows[1]["source"] == "catalog_b"
        assert rows[1]["title"] == "Source B Book Title"

    assert store.indexer.total_vectors == 2


def test_cli_ingest_oai_options(tmp_path: Path) -> None:
    runner = CliRunner()
    cfg_file = tmp_path / "test_cfg.yaml"
    cfg_file.write_text(
        f"""
storage:
  authority_db: "{tmp_path}/authority.db"
  finto_db: "{tmp_path}/finto.db"
  progress_db: "{tmp_path}/progress.db"
  index_path: "{tmp_path}/authority.faiss"

embedding:
  model_name: "TurkuNLP/sbert-cased-finnish-paraphrase"
  device: "cpu"

ingest_oai:
  source_one:
    enabled: true
    source_type: "oai"
    oai_url: "https://source1.example.org/oai"
    workers: 1
    batch_size: 10
  source_two:
    enabled: false
    source_type: "oai"
    oai_url: "https://source2.example.org/oai"
    workers: 1
    batch_size: 10
"""
    )

    mock_model = MagicMock()
    mock_model.get_embedding_dimension.return_value = 64
    mock_model.get_sentence_embedding_dimension.return_value = 64
    mock_model.encode.side_effect = lambda texts, **kwargs: np.ones((len(texts), 64), dtype=np.float32)

    mock_resp = MagicMock()
    mock_resp.content = SAMPLE_OAI_SOURCE_A
    mock_resp.raise_for_status = MagicMock()

    with patch("reconciliator.embedder.SentenceTransformer", return_value=mock_model):
        with patch("httpx2.Client.get", return_value=mock_resp):
            # Test specifying unknown source raises error
            res = runner.invoke(app, ["ingest-oai", "--config", str(cfg_file), "--source", "non_existent"])
            assert res.exit_code != 0
            assert "Source 'non_existent' not found" in res.output

            # Test specifying specific source
            res = runner.invoke(app, ["ingest-oai", "--config", str(cfg_file), "--source", "source_one"])
            assert res.exit_code == 0
            assert "Successfully ingested 1 records from source 'source_one'" in res.output

            # Test --all (should ingest only enabled sources)
            res = runner.invoke(app, ["ingest-oai", "--config", str(cfg_file), "--all"])
            assert res.exit_code == 0
            assert "source 'source_one'" in res.output
            assert "source 'source_two'" not in res.output
