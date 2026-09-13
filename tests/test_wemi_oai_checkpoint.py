"""Tests for OAI-PMH checkpointing, resumption tokens, and dual-routing WEMI ingestion."""

import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

from reconciliator.config import OAIIngestConfig
from reconciliator.db import init_authority_db, init_corpus_db
from reconciliator.harvest.oai_pmh import OAIPMHHarvester
from reconciliator.ingestion.oai import ingest_oai_wemi_pipeline
from reconciliator.models import HarvestState
from reconciliator.repository import CorpusRepository, WEMIRepository


def test_harvest_partition_resumption_token_callback():
    """Verify that harvest_partition invokes on_resumption_token when resumptionToken is present and when exhausted."""
    page_1_xml = b"""<?xml version="1.0" encoding="UTF-8"?>
    <OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/">
      <ListRecords>
        <record>
          <header><identifier>oai:test:1</identifier></header>
          <metadata>
            <record xmlns="http://www.loc.gov/MARC21/slim">
              <controlfield tag="001">REC001</controlfield>
              <datafield tag="245" ind1="1" ind2="0"><subfield code="a">Page 1 Title</subfield></datafield>
            </record>
          </metadata>
        </record>
        <resumptionToken cursor="0">tok_page_2</resumptionToken>
      </ListRecords>
    </OAI-PMH>
    """
    page_2_xml = b"""<?xml version="1.0" encoding="UTF-8"?>
    <OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/">
      <ListRecords>
        <record>
          <header><identifier>oai:test:2</identifier></header>
          <metadata>
            <record xmlns="http://www.loc.gov/MARC21/slim">
              <controlfield tag="001">REC002</controlfield>
              <datafield tag="245" ind1="1" ind2="0"><subfield code="a">Page 2 Title</subfield></datafield>
            </record>
          </metadata>
        </record>
        <resumptionToken cursor="1"></resumptionToken>
      </ListRecords>
    </OAI-PMH>
    """

    harvester = OAIPMHHarvester(endpoint_url="http://example.org/oai", request_delay=0.0)

    tokens_captured: list[str | None] = []

    def on_token(t: str | None) -> None:
        tokens_captured.append(t)

    resp1 = MagicMock()
    resp1.content = page_1_xml
    resp1.raise_for_status = MagicMock()

    resp2 = MagicMock()
    resp2.content = page_2_xml
    resp2.raise_for_status = MagicMock()

    with patch("httpx2.Client.get", side_effect=[resp1, resp2]) as mock_get:
        records = list(
            harvester.harvest_partition(
                from_date="2026-01-01",
                until_date="2026-01-31",
                on_resumption_token=on_token,
            )
        )

        assert len(records) == 2
        assert len(tokens_captured) == 2
        assert tokens_captured[0] == "tok_page_2"
        assert tokens_captured[1] is None
        assert mock_get.call_count == 2
        # Check that second request sent resumptionToken
        second_call_params = mock_get.call_args_list[1][1]["params"]
        assert second_call_params.get("resumptionToken") == "tok_page_2"


def test_harvest_state_persistence(tmp_path: Path):
    """Verify saving and retrieving HarvestState in both CorpusRepository and WEMIRepository."""
    corpus_db = tmp_path / "corpus.db"
    auth_db = tmp_path / "auth.db"

    with sqlite3.connect(corpus_db) as conn:
        init_corpus_db(conn)
    with sqlite3.connect(auth_db) as conn:
        init_authority_db(conn)

    c_repo = CorpusRepository(db_path=corpus_db)
    w_repo = WEMIRepository(db_path=auth_db)

    # 1. Test in CorpusRepository
    assert c_repo.get_harvest_state("target_oai") is None
    c_state = HarvestState(
        source_id="target_oai",
        source_type="oai_pmh",
        metadata_prefix="marc21",
        last_harvest_from="2026-09-01",
        last_harvest_until="2026-09-30",
        resumption_token="tok_corp_1",
        status="in_progress",
        records_harvested=50,
    )
    c_repo.save_harvest_state(c_state)

    loaded_c = c_repo.get_harvest_state("target_oai")
    assert loaded_c is not None
    assert loaded_c.source_id == "target_oai"
    assert loaded_c.resumption_token == "tok_corp_1"
    assert loaded_c.status == "in_progress"
    assert loaded_c.records_harvested == 50

    # 2. Test in WEMIRepository
    assert w_repo.get_harvest_state("melinda_oai") is None
    w_state = HarvestState(
        source_id="melinda_oai",
        source_type="oai_pmh",
        metadata_prefix="marc21",
        last_harvest_from="2026-09-01",
        last_harvest_until="2026-09-30",
        resumption_token=None,
        status="completed",
        records_harvested=1200,
    )
    w_repo.save_harvest_state(w_state)

    loaded_w = w_repo.get_harvest_state("melinda_oai")
    assert loaded_w is not None
    assert loaded_w.source_id == "melinda_oai"
    assert loaded_w.resumption_token is None
    assert loaded_w.status == "completed"
    assert loaded_w.records_harvested == 1200


def test_ingest_oai_wemi_pipeline_dual_routing(tmp_path: Path):
    """Verify dual-routing dispatch in ingest_oai_wemi_pipeline."""
    sample_oai_xml = b"""<?xml version="1.0" encoding="UTF-8"?>
    <OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/">
      <ListRecords>
        <record>
          <header><identifier>oai:test:101</identifier></header>
          <metadata>
            <record xmlns="http://www.loc.gov/MARC21/slim">
              <controlfield tag="001">FI-MEL-101</controlfield>
              <controlfield tag="008">210101s2020    fi |||||||||||||||||fin||</controlfield>
              <datafield tag="020" ind1="#" ind2="#"><subfield code="a">9789510000012</subfield></datafield>
              <datafield tag="100" ind1="1" ind2="#"><subfield code="a">Virtanen, Jorma,</subfield><subfield code="4">aut</subfield></datafield>
              <datafield tag="245" ind1="1" ind2="0"><subfield code="a">Testikirja :</subfield><subfield code="b">alaotsikko</subfield></datafield>
            </record>
          </metadata>
        </record>
      </ListRecords>
    </OAI-PMH>
    """

    corpus_db = tmp_path / "target_corpus.db"
    auth_db = tmp_path / "authority.db"

    with sqlite3.connect(corpus_db) as conn:
        init_corpus_db(conn)
    with sqlite3.connect(auth_db) as conn:
        init_authority_db(conn)

    c_repo = CorpusRepository(db_path=corpus_db)
    w_repo = WEMIRepository(db_path=auth_db)

    # Route A: Target Routing -> stages TargetRecord in corpus_db
    target_config = OAIIngestConfig(
        oai_url="http://example.org/oai_target",
        metadata_prefix="marc21",
        target_database=str(corpus_db),
        batch_size=1,
    )

    mock_resp = MagicMock()
    mock_resp.content = sample_oai_xml
    mock_resp.raise_for_status = MagicMock()

    with patch("httpx2.Client.get", return_value=mock_resp):
        total_a = ingest_oai_wemi_pipeline(
            source_name="target_src",
            config=target_config,
            corpus_repo=c_repo,
        )
        assert total_a == 1

    # Verify TargetRecord staged in corpus.db
    staged = c_repo.get_target_record("FI-MEL-101")
    assert staged is not None
    assert staged.record_id == "FI-MEL-101"
    assert staged.work_type == "text"
    assert staged.standard_key == "9789510000012"
    assert "Testikirja" in staged.serialized_work

    # Verify checkpoint in corpus.db
    c_ckpt = c_repo.get_harvest_state("target_src")
    assert c_ckpt is not None
    assert c_ckpt.status == "completed"

    # Route B: Authority Routing -> decomposes into WEMI in auth.db
    auth_config = OAIIngestConfig(
        oai_url="http://example.org/oai_auth",
        metadata_prefix="marc21",
        target_database=None,
        batch_size=1,
    )

    with patch("httpx2.Client.get", return_value=mock_resp):
        total_b = ingest_oai_wemi_pipeline(
            source_name="melinda_src",
            config=auth_config,
            wemi_repo=w_repo,
        )
        assert total_b == 1

    # Verify WEMI entities created in authority.db
    w = w_repo.get_work_by_source("melinda_src", "FI-MEL-101")
    assert w is not None
    assert w.title == "Testikirja :"
    assert w.work_type == "text"
    assert w.original_language == "fin"

    m = w_repo.get_manifestation_by_source("melinda_src", "FI-MEL-101")
    assert m is not None
    assert m.title == "Testikirja :"

    # Verify checkpoint in authority.db
    w_ckpt = w_repo.get_harvest_state("melinda_src")
    assert w_ckpt is not None
    assert w_ckpt.status == "completed"
