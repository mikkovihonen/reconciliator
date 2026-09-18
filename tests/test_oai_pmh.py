"""Unit tests for multithreaded OAI-PMH harvester and date partitioning."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, AsyncIterator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select

from reconciliator.db import works_table
from reconciliator.harvest.oai_pmh import OAIPMHHarvester, partition_date_range

MARC_NS = "http://www.loc.gov/MARC21/slim"


def make_marc_record(source_id: str, title: str, org_code: str | None = None) -> Any:
    """Build a minimal MARCXML <record> element for driving the ingest pipeline.

    The WEMI pipeline consumes raw lxml elements from stream_file_raw_elements,
    so tests feed it elements rather than pre-normalized dicts.
    """
    from lxml import etree

    record = etree.Element(f"{{{MARC_NS}}}record")
    cf001 = etree.SubElement(record, f"{{{MARC_NS}}}controlfield", tag="001")
    cf001.text = source_id
    if org_code:
        cf003 = etree.SubElement(record, f"{{{MARC_NS}}}controlfield", tag="003")
        cf003.text = org_code
    df245 = etree.SubElement(record, f"{{{MARC_NS}}}datafield", tag="245", ind1="1", ind2="0")
    sf_a = etree.SubElement(df245, f"{{{MARC_NS}}}subfield", code="a")
    sf_a.text = title
    return record


async def _collect(gen: AsyncIterator[Any]) -> list[Any]:
    items = []
    async for item in gen:
        items.append(item)
    return items


def test_partition_date_range() -> None:
    slices = partition_date_range("2021-01-01", "2021-03-15", slice_days=30)
    assert len(slices) >= 3
    assert slices[0][0] == "2021-01-01"
    assert slices[-1][1] == "2021-03-15"


def test_harvest_partition_mocked() -> None:
    sample_oai_xml = b"""<?xml version="1.0" encoding="UTF-8"?>
    <OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/">
      <ListRecords>
        <record>
          <header><identifier>oai:melinda:123</identifier></header>
          <metadata>
            <record xmlns="http://www.loc.gov/MARC21/slim">
              <controlfield tag="001">000100201</controlfield>
              <datafield tag="245" ind1="1" ind2="0">
                <subfield code="a">Sinuhe</subfield>
              </datafield>
            </record>
          </metadata>
        </record>
      </ListRecords>
    </OAI-PMH>
    """

    harvester = OAIPMHHarvester(endpoint_url="http://example.org/oai")

    mock_resp = MagicMock()
    mock_resp.content = sample_oai_xml
    mock_resp.raise_for_status = MagicMock()

    with patch("httpx2.AsyncClient.get", new_callable=AsyncMock, return_value=mock_resp):
        pages = asyncio.run(
            _collect(harvester.harvest_partition(from_date="2021-01-01", until_date="2021-01-31"))
        )
        assert len(pages) == 1
        page_bytes, token = pages[0]
        assert b"000100201" in page_bytes
        assert token is None


def test_oai_pmh_user_agent() -> None:
    """Test that OAIPMHHarvester uses default canonical User-Agent from version.py."""
    from reconciliator.version import get_default_user_agent

    expected_ua = get_default_user_agent()
    harvester = OAIPMHHarvester(endpoint_url="http://example.org/oai")
    assert harvester.user_agent == expected_ua
    assert harvester.user_agent.startswith("reconciliator/")

    custom_harvester = OAIPMHHarvester(
        endpoint_url="http://example.org/oai", user_agent="custom-oai/1.0"
    )
    assert custom_harvester.user_agent == "custom-oai/1.0"


def test_harvest_partition_retry_on_transient_error() -> None:
    import httpx2

    sample_xml = b"""<?xml version="1.0" encoding="UTF-8"?>
    <OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/">
      <ListRecords>
        <record>
          <metadata>
            <record xmlns="http://www.loc.gov/MARC21/slim">
              <controlfield tag="001">999</controlfield>
            </record>
          </metadata>
        </record>
      </ListRecords>
    </OAI-PMH>
    """
    harvester = OAIPMHHarvester(
        endpoint_url="http://example.org/oai",
        max_retries=3,
        base_delay=0.01,
        request_delay=0.0,
    )

    fail_resp = MagicMock()
    fail_resp.headers = {}
    req = MagicMock()
    fail_err = httpx2.HTTPStatusError("503 Service Unavailable", request=req, response=fail_resp)
    fail_resp.raise_for_status.side_effect = fail_err

    ok_resp = MagicMock()
    ok_resp.content = sample_xml
    ok_resp.raise_for_status = MagicMock()

    with patch("httpx2.AsyncClient.get", side_effect=[fail_resp, ok_resp]):
        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            pages = asyncio.run(_collect(harvester.harvest_partition(from_date="2021-01-01")))
            assert len(pages) == 1
            assert mock_sleep.called
            assert mock_sleep.call_args[0][0] == 0.01  # base_delay * (2 ** 0)


def test_harvest_partition_retry_after_header() -> None:
    import httpx2

    sample_xml = b"""<?xml version="1.0" encoding="UTF-8"?>
    <OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/">
      <ListRecords>
        <record>
          <metadata>
            <record xmlns="http://www.loc.gov/MARC21/slim">
              <controlfield tag="001">999</controlfield>
            </record>
          </metadata>
        </record>
      </ListRecords>
    </OAI-PMH>
    """
    harvester = OAIPMHHarvester(
        endpoint_url="http://example.org/oai",
        max_retries=3,
        base_delay=1.0,
        request_delay=0.0,
    )

    fail_resp = MagicMock()
    fail_resp.headers = {"Retry-After": "5"}
    req = MagicMock()
    fail_err = httpx2.HTTPStatusError("429 Too Many Requests", request=req, response=fail_resp)
    fail_resp.raise_for_status.side_effect = fail_err

    ok_resp = MagicMock()
    ok_resp.content = sample_xml
    ok_resp.raise_for_status = MagicMock()

    with patch("httpx2.AsyncClient.get", side_effect=[fail_resp, ok_resp]):
        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            pages = asyncio.run(_collect(harvester.harvest_partition(from_date="2021-01-01")))
            assert len(pages) == 1
            assert mock_sleep.called
            assert mock_sleep.call_args[0][0] == 5.0


def test_harvest_partition_retry_exhausted() -> None:
    import httpx2

    harvester = OAIPMHHarvester(
        endpoint_url="http://example.org/oai",
        max_retries=2,
        base_delay=0.01,
        request_delay=0.0,
    )

    fail_resp = MagicMock()
    req = MagicMock()
    fail_err = httpx2.HTTPStatusError("500 Server Error", request=req, response=fail_resp)
    fail_resp.raise_for_status.side_effect = fail_err

    with patch("httpx2.AsyncClient.get", new_callable=AsyncMock, return_value=fail_resp):
        with patch("asyncio.sleep", new_callable=AsyncMock):
            with pytest.raises(httpx2.HTTPStatusError):
                asyncio.run(_collect(harvester.harvest_partition(from_date="2021-01-01")))


def test_get_earliest_datestamp() -> None:
    identify_xml = b"""<?xml version="1.0" encoding="UTF-8"?>
    <OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/">
      <responseDate>2026-09-11T08:00:00Z</responseDate>
      <request verb="Identify">http://example.org/oai</request>
      <Identify>
        <repositoryName>Melinda</repositoryName>
        <baseURL>http://example.org/oai</baseURL>
        <protocolVersion>2.0</protocolVersion>
        <earliestDatestamp>2005-06-15T00:00:00Z</earliestDatestamp>
        <deletedRecord>no</deletedRecord>
        <granularity>YYYY-MM-DDThh:mm:ssZ</granularity>
      </Identify>
    </OAI-PMH>
    """
    harvester = OAIPMHHarvester(endpoint_url="http://example.org/oai")
    mock_resp = MagicMock()
    mock_resp.content = identify_xml
    mock_resp.raise_for_status = MagicMock()

    with patch("httpx2.AsyncClient.get", new_callable=AsyncMock, return_value=mock_resp):
        earliest = asyncio.run(harvester.get_earliest_datestamp())
        assert earliest == "2005-06-15"


def test_harvest_partition_no_records_match() -> None:
    no_records_xml = b"""<?xml version="1.0" encoding="UTF-8"?>
    <OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/">
      <responseDate>2026-09-11T08:00:00Z</responseDate>
      <request verb="ListRecords" from="2001-01-01" until="2001-01-31">http://example.org/oai</request>
      <error code="noRecordsMatch">No matching records found for date partition</error>
    </OAI-PMH>
    """
    harvester = OAIPMHHarvester(endpoint_url="http://example.org/oai")
    mock_resp = MagicMock()
    mock_resp.content = no_records_xml
    mock_resp.raise_for_status = MagicMock()

    with patch("httpx2.AsyncClient.get", new_callable=AsyncMock, return_value=mock_resp):
        pages = asyncio.run(
            _collect(harvester.harvest_partition(from_date="2001-01-01", until_date="2001-01-31"))
        )
        assert len(pages) == 0


def test_ingest_oai_pipeline_custom_batch_queue_size() -> None:
    from reconciliator.config import OAIIngestConfig
    from reconciliator.ingestion.oai import ingest_oai_pipeline

    config = OAIIngestConfig(
        source_type="file",
        input_path=Path("dummy.xml"),
        batch_size=2,
        batch_queue_size=9,
    )
    store = MagicMock()
    store.get_indexed_source_ids.return_value = set()

    created_queues: list[asyncio.Queue[Any]] = []
    real_queue = asyncio.Queue

    def queue_spy(*args: Any, **kwargs: Any) -> asyncio.Queue[Any]:
        q = real_queue(*args, **kwargs)
        created_queues.append(q)
        return q

    with patch(
        "reconciliator.ingestion.oai.stream_file_raw_elements",
        return_value=[
            make_marc_record("rec_1", "Book 1"),
            make_marc_record("rec_2", "Book 2"),
        ],
    ):
        with patch("asyncio.Queue", side_effect=queue_spy):
            total = asyncio.run(ingest_oai_pipeline("test", config, store))
            assert total == 2
            assert any(q.maxsize == 9 for q in created_queues)


def test_wemi_repository_get_indexed_source_ids(tmp_path: Path) -> None:
    from reconciliator.config import VectorEngineConfig
    from reconciliator.models import ManifestationEntity
    from reconciliator.repository import WEMIRepository

    db_path = tmp_path / "test_auth.db"
    index_path = tmp_path / "test_auth.index"
    dim = 8

    vec_cfg = VectorEngineConfig(backend="usearch", device="cpu", mode="embedded")
    store = WEMIRepository(
        db_path=db_path, work_index_path=index_path, dimension=dim, vector_engine_config=vec_cfg
    )
    assert store.get_indexed_source_ids("melinda") == set()

    # Insert two manifestations
    store.insert_manifestation(
        ManifestationEntity(
            manifestation_uri="http://example.org/manifest/rec_101",
            source="melinda",
            source_id="rec_101",
            title="Title 1",
            title_normalized="title 1",
            carrier_type="volume",
        )
    )
    store.insert_manifestation(
        ManifestationEntity(
            manifestation_uri="http://example.org/manifest/rec_102",
            source="melinda",
            source_id="rec_102",
            title="Title 2",
            title_normalized="title 2",
            carrier_type="volume",
        )
    )
    indexed_ids = store.get_indexed_source_ids("melinda")
    assert indexed_ids == {"rec_101", "rec_102"}
    assert store.get_indexed_source_ids("other_source") == set()


def test_ingest_oai_pipeline_skip_existing(tmp_path: Path) -> None:
    from reconciliator.config import OAIIngestConfig, VectorEngineConfig
    from reconciliator.ingestion.oai import ingest_oai_pipeline
    from reconciliator.repository import WEMIRepository

    config = OAIIngestConfig(
        source_type="file",
        input_path=Path("dummy.xml"),
        batch_size=2,
    )
    vec_cfg = VectorEngineConfig(backend="usearch", device="cpu", mode="embedded")
    store = WEMIRepository(
        db_path=tmp_path / "authority.db",
        work_index_path=tmp_path / "work.index",
        dimension=8,
        vector_engine_config=vec_cfg,
    )

    raw_stream = [
        make_marc_record("rec_1", "Old Book 1"),
        make_marc_record("rec_2", "New Book 2"),
        make_marc_record("rec_3", "Old Book 3"),
        make_marc_record("rec_4", "New Book 4"),
    ]

    with patch("reconciliator.ingestion.oai.stream_file_raw_elements", return_value=raw_stream):
        with patch.object(
            WEMIRepository, "get_indexed_source_ids", return_value={"rec_1", "rec_3"}
        ):
            total = asyncio.run(ingest_oai_pipeline("melinda", config, store, skip_existing=True))

    assert total == 2
    # Only the previously unseen records reached storage.
    with store.get_connection() as conn:
        stored = sorted(conn.execute(select(works_table.c.source_id)).scalars())
    assert stored == ["rec_2", "rec_4"]


def test_wemi_repository_get_indexed_source_ids_variants(tmp_path: Path) -> None:
    from reconciliator.config import VectorEngineConfig
    from reconciliator.models import ManifestationEntity
    from reconciliator.repository import WEMIRepository

    vec_cfg = VectorEngineConfig(backend="usearch", device="cpu", mode="embedded")
    store = WEMIRepository(
        db_path=tmp_path / "variants.db",
        work_index_path=tmp_path / "variants.index",
        dimension=8,
        vector_engine_config=vec_cfg,
    )
    store.insert_manifestation(
        ManifestationEntity(
            manifestation_uri="http://example.org/manifest/rec_variant",
            source="melinda",
            source_id="(FI-MELINDA)000298022",
            title="Sample Book",
            title_normalized="sample book",
            carrier_type="volume",
        )
    )

    ids = store.get_indexed_source_ids("melinda")
    assert "(FI-MELINDA)000298022" in ids
    # Note: WEMIRepository stores source_ids as-is without prefix stripping


def test_ingest_oai_pipeline_skip_existing_matches_canonical_ids(tmp_path: Path) -> None:
    """Skip-existing compares the (003)001 form that storage actually holds."""
    from reconciliator.config import OAIIngestConfig, VectorEngineConfig
    from reconciliator.ingestion.oai import ingest_oai_pipeline
    from reconciliator.repository import WEMIRepository

    config = OAIIngestConfig(
        source_type="file",
        input_path=Path("dummy.xml"),
        batch_size=2,
    )
    vec_cfg = VectorEngineConfig(backend="usearch", device="cpu", mode="embedded")
    store = WEMIRepository(
        db_path=tmp_path / "authority.db",
        work_index_path=tmp_path / "work.index",
        dimension=8,
        vector_engine_config=vec_cfg,
    )

    # A 001 paired with a 003 is stored as "(003)001", which is the form the
    # skip set holds; a bare 001 stays bare.
    raw_stream = [
        make_marc_record("000298022", "Old Book 1", org_code="FI-MELINDA"),
        make_marc_record("999999", "New Book 2", org_code="FI-MELINDA"),
        make_marc_record("888888", "New Book 3"),
    ]

    with patch("reconciliator.ingestion.oai.stream_file_raw_elements", return_value=raw_stream):
        with patch.object(
            WEMIRepository,
            "get_indexed_source_ids",
            return_value={"(FI-MELINDA)000298022"},
        ):
            total = asyncio.run(ingest_oai_pipeline("melinda", config, store, skip_existing=True))

    assert total == 2
    with store.get_connection() as conn:
        stored = sorted(conn.execute(select(works_table.c.source_id)).scalars())
    assert stored == ["(FI-MELINDA)999999", "888888"]


def test_ingest_oai_pipeline_propagates_producer_error() -> None:
    from reconciliator.config import OAIIngestConfig
    from reconciliator.ingestion.oai import ingest_oai_pipeline

    config = OAIIngestConfig(
        source_type="file",
        input_path=Path("dummy.xml"),
        batch_size=2,
    )
    store = MagicMock()
    store.get_indexed_source_ids.return_value = set()
    tracker = MagicMock()

    def failing_stream(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("Harvester stream failed abruptly")
        yield {}

    with patch("reconciliator.ingestion.oai.stream_file_raw_elements", side_effect=failing_stream):
        with pytest.raises(RuntimeError, match="Harvester stream failed abruptly"):
            asyncio.run(
                ingest_oai_pipeline(
                    "melinda",
                    config,
                    store,
                    tracker=tracker,
                )
            )
        tracker.fail_task.assert_called_once()


def test_ingest_oai_pipeline_decoupled_harvest_queue() -> None:
    """Harvesting continues while the writer is busy, and batches stay batch_size."""
    import time

    from reconciliator.config import OAIIngestConfig
    from reconciliator.ingestion.oai import ingest_oai_pipeline

    config = OAIIngestConfig(
        source_type="file",
        input_path=Path("dummy.xml"),
        batch_size=2,
        queue_size=20,
        batch_queue_size=2,
    )
    store = MagicMock()
    store.get_indexed_source_ids.return_value = set()

    write_sizes: list[int] = []

    def slow_write(**kwargs: Any) -> None:
        time.sleep(0.02)
        write_sizes.append(len(kwargs.get("works") or []))

    store.insert_wemi_batch.side_effect = slow_write

    raw_stream = [make_marc_record(f"rec_{i}", f"Book {i}") for i in range(10)]

    with patch("reconciliator.ingestion.oai.stream_file_raw_elements", return_value=raw_stream):
        total = asyncio.run(ingest_oai_pipeline("melinda", config, store))

    assert total == 10
    assert store.insert_wemi_batch.call_count == 5
    assert write_sizes == [2, 2, 2, 2, 2]


def test_ingest_oai_pipeline_preserves_skipped_in_telemetry() -> None:
    from reconciliator.config import OAIIngestConfig
    from reconciliator.ingestion.oai import ingest_oai_pipeline

    config = OAIIngestConfig(
        source_type="file",
        input_path=Path("dummy.xml"),
        batch_size=2,
    )
    store = MagicMock()
    store.get_indexed_source_ids.return_value = {"rec_old_1", "rec_old_2"}
    tracker = MagicMock()

    raw_stream = [
        make_marc_record("rec_old_1", "Old 1"),
        make_marc_record("rec_new_1", "New 1"),
        make_marc_record("rec_old_2", "Old 2"),
        make_marc_record("rec_new_2", "New 2"),
    ]

    with patch("reconciliator.ingestion.oai.stream_file_raw_elements", return_value=raw_stream):
        total = asyncio.run(
            ingest_oai_pipeline(
                "melinda",
                config,
                store,
                tracker=tracker,
                skip_existing=True,
            )
        )

    assert total == 2
    # Skipped records stay visible in the final telemetry, not just the log.
    tracker.complete_task.assert_called_once()
    meta = tracker.complete_task.call_args.kwargs.get("metadata", {})
    assert meta.get("skipped") == 2
    assert meta.get("indexed") == 2


def test_ingest_oai_partitioned_telemetry_with_tracker() -> None:
    from reconciliator.config import OAIIngestConfig
    from reconciliator.ingestion.oai import ingest_oai_pipeline

    config = OAIIngestConfig(
        source_type="oai",
        oai_url="https://example.com/oai",
        from_date="2021-01-01",
        until_date="2021-01-10",
        slice_days=2,
    )
    store = MagicMock()
    store.get_harvest_state.return_value = None
    store.get_indexed_source_ids.return_value = set()
    tracker = MagicMock()
    tracker.heartbeat_interval = 0.05

    async def mock_harvest_partition(*args: Any, **kwargs: Any) -> Any:
        if False:
            yield b"", None

    with patch("reconciliator.ingestion.oai.OAIPMHHarvester") as mock_harv_cls:
        mock_harv_cls.return_value.harvest_partition = mock_harvest_partition
        total = asyncio.run(
            ingest_oai_pipeline(
                "melinda",
                config,
                store,
                tracker=tracker,
            )
        )

    assert total == 0
    tracker.start_task.assert_called_once()
    assert tracker.update_progress.call_count >= 1
    last_update_call = tracker.update_progress.call_args_list[-1]
    assert last_update_call.kwargs.get("percentage") == 100.0
    meta = last_update_call.kwargs.get("metadata", {})
    assert meta.get("slices_total") == 3
    assert meta.get("slices_done") == 3
    tracker.complete_task.assert_called_once()

