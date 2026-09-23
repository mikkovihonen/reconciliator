"""Tests for OAI-PMH checkpointing, resumption tokens, and dual-routing WEMI ingestion."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, AsyncIterator
from unittest.mock import AsyncMock, MagicMock, patch

from reconciliator.config import OAIIngestConfig
from reconciliator.database import WEMIRepository
from reconciliator.harvest.oai_pmh import OAIPMHHarvester
from reconciliator.ingestion.oai import ingest_oai_pipeline
from reconciliator.models import HarvestState


async def _collect(gen: AsyncIterator[Any]) -> list[Any]:
    items = []
    async for item in gen:
        items.append(item)
    return items


def test_harvest_partition_resumption_token_callback():
    """Verify that harvest_partition extracts and yields resumptionToken on page boundaries and exhausts properly."""
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

    resp1 = MagicMock()
    resp1.content = page_1_xml
    resp1.raise_for_status = MagicMock()

    resp2 = MagicMock()
    resp2.content = page_2_xml
    resp2.raise_for_status = MagicMock()

    with patch("httpx2.AsyncClient.get", side_effect=[resp1, resp2]) as mock_get:
        pages = asyncio.run(
            _collect(
                harvester.harvest_partition(
                    from_date="2026-01-01",
                    until_date="2026-01-31",
                )
            )
        )

        assert len(pages) == 2
        page1_bytes, tok1 = pages[0]
        page2_bytes, tok2 = pages[1]
        assert tok1 == "tok_page_2"
        assert tok2 is None
        assert mock_get.call_count == 2
        # Check that second request sent resumptionToken
        second_call_params = mock_get.call_args_list[1][1]["params"]
        assert second_call_params.get("resumptionToken") == "tok_page_2"


def test_harvest_state_persistence(tmp_path: Path):
    """Verify saving and retrieving HarvestState in unified WEMIRepository."""
    auth_db = tmp_path / "auth.db"
    repo = WEMIRepository(db_path=auth_db)

    # 1. Test corpus-style harvest state (target OAI harvest)
    assert repo.get_harvest_state("target_oai") is None
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
    repo.save_harvest_state(c_state)

    loaded_c = repo.get_harvest_state("target_oai")
    assert loaded_c is not None
    assert loaded_c.source_id == "target_oai"
    assert loaded_c.resumption_token == "tok_corp_1"
    assert loaded_c.status == "in_progress"
    assert loaded_c.records_harvested == 50

    # 2. Test authority-style harvest state (Melinda OAI harvest)
    assert repo.get_harvest_state("melinda_oai") is None
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
    repo.save_harvest_state(w_state)

    loaded_w = repo.get_harvest_state("melinda_oai")
    assert loaded_w is not None
    assert loaded_w.source_id == "melinda_oai"
    assert loaded_w.resumption_token is None
    assert loaded_w.status == "completed"
    assert loaded_w.records_harvested == 1200


def test_ingest_oai_pipeline_single_routing(tmp_path: Path):
    """Every source decomposes into WEMI; only `authoritative` differs.

    There used to be two routes: a source with `target_database` set was staged
    as MatchRecords in a separate corpus table and never decomposed, while every
    other source became WEMI rows. A corpus is now ingested exactly like an
    authority, and `authoritative: false` is the only distinction.
    """
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

    auth_db = tmp_path / "authority.db"
    repo = WEMIRepository(db_path=auth_db)

    mock_resp = MagicMock()
    mock_resp.content = sample_oai_xml
    mock_resp.raise_for_status = MagicMock()

    # Route A: a corpus source - WEMI rows, registered non-authoritative
    target_config = OAIIngestConfig(
        oai_url="http://example.org/oai_target",
        metadata_prefix="marc21",
        authoritative=False,
        batch_size=1,
    )

    with patch("httpx2.AsyncClient.get", new_callable=AsyncMock, return_value=mock_resp):
        total_a = asyncio.run(
            ingest_oai_pipeline(
                source_name="target_src",
                config=target_config,
                store=repo,
            )
        )
        assert total_a == 1

    # The corpus record is WEMI, not staging, and projects back out as a record
    # carrying its blocking keys.
    staged = next(
        r for r in repo.get_all_match_records(origins=["target_src"]) if r.record_id == "FI-MEL-101"
    )
    assert staged.work_type == "text"
    assert staged.get_first_key("standard") == "9789510000012"
    assert "Testikirja" in staged.serialized_work
    assert repo.get_sources()["target_src"] is False

    # Verify checkpoint (unified harvest_state)
    c_ckpt = repo.get_harvest_state("target_src")
    assert c_ckpt is not None
    assert c_ckpt.status == "completed"

    # Route B: an authority source - the same decomposition, marked authoritative
    auth_config = OAIIngestConfig(
        oai_url="http://example.org/oai_auth",
        metadata_prefix="marc21",
        authoritative=True,
        batch_size=1,
    )

    with patch("httpx2.AsyncClient.get", new_callable=AsyncMock, return_value=mock_resp):
        total_b = asyncio.run(
            ingest_oai_pipeline(
                source_name="melinda_src",
                config=auth_config,
                store=repo,
            )
        )
        assert total_b == 1

    # Verify WEMI entities created in authority.db
    w = repo.get_work_by_source("melinda_src", "FI-MEL-101")
    assert w is not None
    # 245 reads "Testikirja :" because a $b follows; the " :" is ISBD
    # presentation punctuation and the loader strips it. It used to survive
    # here: the toggle reached the authority decomposer's headings and not the
    # bibliographic 245, so one source's title carried the colon and another's
    # did not. `RecordLoader.finalise` now applies it to every loader's output.
    assert w.title == "Testikirja"
    assert w.work_type == "text"

    assert w.original_language == "fin"

    m = repo.get_manifestation_by_source("melinda_src", "FI-MEL-101")
    assert m is not None
    assert m.title == "Testikirja"

    # Verify checkpoint (unified harvest_state)
    w_ckpt = repo.get_harvest_state("melinda_src")
    assert w_ckpt is not None
    assert w_ckpt.status == "completed"

    # Source registered as authoritative
    assert repo.get_sources()["melinda_src"] is True
