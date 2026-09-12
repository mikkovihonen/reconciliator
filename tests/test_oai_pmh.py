"""Unit tests for multithreaded OAI-PMH harvester and date partitioning."""

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import numpy as np

from reconciliator.harvest.oai_pmh import OAIPMHHarvester, partition_date_range


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

    with patch("httpx2.Client.get", return_value=mock_resp):
        records = list(harvester.harvest_partition(from_date="2021-01-01", until_date="2021-01-31"))
        assert len(records) == 1
        assert (
            records[0].find(".//{http://www.loc.gov/MARC21/slim}controlfield[@tag='001']").text
            == "000100201"
        )


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

    with patch("httpx2.Client.get", side_effect=[fail_resp, ok_resp]):
        with patch("time.sleep") as mock_sleep:
            records = list(harvester.harvest_partition(from_date="2021-01-01"))
            assert len(records) == 1
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

    with patch("httpx2.Client.get", side_effect=[fail_resp, ok_resp]):
        with patch("time.sleep") as mock_sleep:
            records = list(harvester.harvest_partition(from_date="2021-01-01"))
            assert len(records) == 1
            assert mock_sleep.called
            assert mock_sleep.call_args[0][0] == 5.0


def test_harvest_partition_retry_exhausted() -> None:
    import httpx2
    import pytest

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

    with patch("httpx2.Client.get", return_value=fail_resp):
        with patch("time.sleep"):
            with pytest.raises(httpx2.HTTPStatusError):
                list(harvester.harvest_partition(from_date="2021-01-01"))


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

    with patch("httpx2.Client.get", return_value=mock_resp):
        earliest = harvester.get_earliest_datestamp()
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

    with patch("httpx2.Client.get", return_value=mock_resp):
        records = list(harvester.harvest_partition(from_date="2001-01-01", until_date="2001-01-31"))
        assert len(records) == 0


def test_stream_oai_records_parallel_date_partitioning() -> None:
    from reconciliator.config import OAIIngestConfig
    from reconciliator.ingestion.oai import stream_oai_records

    sample_xml = b"""<?xml version="1.0" encoding="UTF-8"?>
    <OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/">
      <ListRecords>
        <record>
          <metadata>
            <record xmlns="http://www.loc.gov/MARC21/slim">
              <controlfield tag="001">rec_001</controlfield>
              <datafield tag="245" ind1="1" ind2="0">
                <subfield code="a">Parallel Book</subfield>
              </datafield>
            </record>
          </metadata>
        </record>
      </ListRecords>
    </OAI-PMH>
    """
    config = OAIIngestConfig(
        source_type="oai",
        oai_url="http://example.org/oai",
        workers=2,
        from_date="2021-01-01",
        until_date="2021-03-01",
        slice_days=30,
        batch_size=10,
        request_delay=0.0,
    )

    mock_resp = MagicMock()
    mock_resp.content = sample_xml
    mock_resp.raise_for_status = MagicMock()

    with patch("httpx2.Client.get", return_value=mock_resp):
        records = list(stream_oai_records(source_name="test_oai", config=config))
        # 2 date slices, each returning 1 record -> 2 records yielded
        assert len(records) == 2
        assert records[0]["title"] == "Parallel Book"
        assert records[0]["source"] == "test_oai"


def test_stream_oai_records_autodiscovery_parallel() -> None:
    from reconciliator.config import OAIIngestConfig
    from reconciliator.ingestion.oai import stream_oai_records

    sample_xml = b"""<?xml version="1.0" encoding="UTF-8"?>
    <OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/">
      <ListRecords>
        <record>
          <header>
            <identifier>oai:example.org:auto/123</identifier>
          </header>
          <metadata>
            <record xmlns="http://www.loc.gov/MARC21/slim">
              <controlfield tag="001">rec_auto</controlfield>
              <datafield tag="245" ind1="1" ind2="0">
                <subfield code="a">Auto Book</subfield>
              </datafield>
            </record>
          </metadata>
        </record>
      </ListRecords>
    </OAI-PMH>
    """
    config = OAIIngestConfig(
        source_type="oai",
        oai_url="http://example.org/oai",
        workers=2,
        from_date=None,  # Should auto-discover
        until_date="2000-02-15",
        slice_days=30,
        batch_size=10,
        request_delay=0.0,
    )

    mock_resp = MagicMock()
    mock_resp.content = sample_xml
    mock_resp.raise_for_status = MagicMock()

    with patch(
        "reconciliator.harvest.oai_pmh.OAIPMHHarvester.get_earliest_datestamp",
        return_value="2000-01-01",
    ):
        with patch("httpx2.Client.get", return_value=mock_resp):
            records = list(stream_oai_records(source_name="melinda", config=config))
            assert len(records) >= 1
            assert records[0]["title"] == "Auto Book"
            assert records[0]["source"] == "melinda"


def test_stream_oai_records_custom_queue_size() -> None:
    import queue

    from reconciliator.config import OAIIngestConfig
    from reconciliator.ingestion.oai import stream_oai_records

    sample_xml = b"""<?xml version="1.0" encoding="UTF-8"?>
    <OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/">
      <ListRecords>
        <record>
          <metadata>
            <record xmlns="http://www.loc.gov/MARC21/slim">
              <controlfield tag="001">rec_001</controlfield>
              <datafield tag="245" ind1="1" ind2="0">
                <subfield code="a">Queue Test Book</subfield>
              </datafield>
            </record>
          </metadata>
        </record>
      </ListRecords>
    </OAI-PMH>
    """
    config = OAIIngestConfig(
        source_type="oai",
        oai_url="http://example.org/oai",
        workers=2,
        from_date="2021-01-01",
        until_date="2021-03-01",
        slice_days=30,
        batch_size=10,
        request_delay=0.0,
        queue_size=777,
    )

    mock_resp = MagicMock()
    mock_resp.content = sample_xml
    mock_resp.raise_for_status = MagicMock()

    created_queues: list[queue.Queue] = []
    real_queue = queue.Queue

    def queue_spy(*args: Any, **kwargs: Any) -> queue.Queue:
        q = real_queue(*args, **kwargs)
        created_queues.append(q)
        return q

    with patch("queue.Queue", side_effect=queue_spy):
        with patch("httpx2.Client.get", return_value=mock_resp):
            records = list(stream_oai_records(source_name="test_oai", config=config))
            assert len(records) >= 1
            assert any(q.maxsize == 777 for q in created_queues)


def test_ingest_oai_pipeline_custom_batch_queue_size() -> None:
    import queue
    from pathlib import Path

    import numpy as np

    from reconciliator.config import OAIIngestConfig
    from reconciliator.ingestion.oai import ingest_oai_pipeline

    config = OAIIngestConfig(
        source_type="file",
        input_path=Path("dummy.xml"),
        batch_size=2,
        normalization_workers=2,
        batch_queue_size=9,
    )
    store = MagicMock()
    normalizer = MagicMock()
    embedder = MagicMock()
    embedder.embed_records.return_value = np.zeros((2, 4), dtype=np.float32)

    created_queues: list[queue.Queue] = []
    real_queue = queue.Queue

    def queue_spy(*args: Any, **kwargs: Any) -> queue.Queue:
        q = real_queue(*args, **kwargs)
        created_queues.append(q)
        return q

    with patch(
        "reconciliator.ingestion.oai.stream_oai_records",
        return_value=[{"title": "Book 1"}, {"title": "Book 2"}],
    ):
        with patch("queue.Queue", side_effect=queue_spy):
            total = ingest_oai_pipeline("test", config, store, normalizer, embedder)
            assert total == 2
            assert any(q.maxsize == 9 for q in created_queues)


def test_authority_store_get_indexed_source_ids(tmp_path: Path) -> None:
    from reconciliator.models import BibliographicRecord
    from reconciliator.repository import AuthorityStore

    db_path = tmp_path / "test_auth.db"
    index_path = tmp_path / "test_auth.index"
    dim = 8

    store = AuthorityStore(db_path=db_path, index_path=index_path, dimension=dim)
    assert store.get_indexed_source_ids("melinda") == set()

    # Insert two records
    rec1 = BibliographicRecord(source="melinda", source_id="rec_101", title="Title 1")
    rec2 = BibliographicRecord(source="melinda", source_id="rec_102", title="Title 2")
    vecs = np.zeros((2, dim), dtype=np.float32)

    store.insert_records_batch([rec1, rec2], vectors=vecs)
    indexed_ids = store.get_indexed_source_ids("melinda")
    assert indexed_ids == {"rec_101", "rec_102"}
    assert store.get_indexed_source_ids("other_source") == set()


def test_ingest_oai_pipeline_skip_existing() -> None:
    from reconciliator.config import OAIIngestConfig
    from reconciliator.ingestion.oai import ingest_oai_pipeline
    from reconciliator.models import BibliographicRecord

    config = OAIIngestConfig(
        source_type="file",
        input_path=Path("dummy.xml"),
        batch_size=2,
        normalization_workers=1,
    )
    store = MagicMock()
    store.get_indexed_source_ids.return_value = {"rec_1", "rec_3"}

    normalizer = MagicMock()
    normalizer.normalize.side_effect = lambda r: BibliographicRecord(
        source="melinda", source_id=r["source_id"], title=r["title"]
    )

    embedder = MagicMock()
    embedder.embed_records.return_value = np.zeros((2, 4), dtype=np.float32)

    raw_stream = [
        {"source_id": "rec_1", "title": "Old Book 1"},
        {"source_id": "rec_2", "title": "New Book 2"},
        {"source_id": "rec_3", "title": "Old Book 3"},
        {"source_id": "rec_4", "title": "New Book 4"},
    ]

    with patch("reconciliator.ingestion.oai.stream_oai_records", return_value=raw_stream):
        total = ingest_oai_pipeline(
            "melinda",
            config,
            store,
            normalizer,
            embedder,
            skip_existing=True,
        )
        assert total == 2
        # Verify normalizer was only called for rec_2 and rec_4
        normalized_ids = [call.args[0]["source_id"] for call in normalizer.normalize.call_args_list]
        assert normalized_ids == ["rec_2", "rec_4"]


def test_authority_store_get_indexed_source_ids_variants(tmp_path: Path) -> None:
    from reconciliator.models import BibliographicRecord
    from reconciliator.repository import AuthorityStore

    store = AuthorityStore(tmp_path / "variants.db", tmp_path / "variants.index", dimension=8)
    rec = BibliographicRecord(
        source="melinda",
        source_id="(FI-MELINDA)000298022",
        title="Sample Book",
    )
    vecs = np.zeros((1, 8), dtype=np.float32)
    store.insert_records_batch([rec], vectors=vecs)

    ids = store.get_indexed_source_ids("melinda")
    assert "(FI-MELINDA)000298022" in ids
    assert "000298022" in ids
    assert "298022" in ids
    assert "oai:melinda.kansalliskirjasto.fi/bib/000298022" in ids
    assert "oai:melinda.kansalliskirjasto.fi/bib/298022" in ids


def test_ingest_oai_pipeline_skip_existing_multithreaded() -> None:
    from reconciliator.config import OAIIngestConfig
    from reconciliator.ingestion.oai import ingest_oai_pipeline
    from reconciliator.models import BibliographicRecord

    config = OAIIngestConfig(
        source_type="file",
        input_path=Path("dummy.xml"),
        batch_size=2,
        normalization_workers=2,
    )
    store = MagicMock()
    # Stored canonical ID
    store.get_indexed_source_ids.return_value = {"(FI-MELINDA)000298022"}

    normalizer = MagicMock()
    normalizer.normalize_id.side_effect = lambda raw_id: f"(FI-MELINDA){str(raw_id).zfill(9)}"
    normalizer.normalize.side_effect = lambda r: BibliographicRecord(
        source="melinda", source_id=r["source_id"], title=r["title"]
    )

    embedder = MagicMock()
    embedder.embed_records.return_value = np.zeros((2, 4), dtype=np.float32)

    # Raw stream contains unnormalized ID "298022" which normalizes to "(FI-MELINDA)000298022"
    raw_stream = [
        {"source_id": "298022", "title": "Old Book 1"},
        {"source_id": "999999", "title": "New Book 2"},
        {"source_id": "(FI-MELINDA)000298022", "title": "Old Book 3"},
        {"source_id": "888888", "title": "New Book 4"},
    ]

    with patch("reconciliator.ingestion.oai.stream_oai_records", return_value=raw_stream):
        total = ingest_oai_pipeline(
            "melinda",
            config,
            store,
            normalizer,
            embedder,
            skip_existing=True,
        )
        assert total == 2
        normalized_ids = [call.args[0]["source_id"] for call in normalizer.normalize.call_args_list]
        assert normalized_ids == ["999999", "888888"]


def test_stream_oai_records_no_dates_fallback_on_discovery_failure() -> None:
    from reconciliator.config import OAIIngestConfig
    from reconciliator.ingestion.oai import stream_oai_records

    config = OAIIngestConfig(
        source_type="oai",
        oai_url="http://example.org/oai",
        workers=4,
        from_date=None,
        until_date=None,
    )

    with patch(
        "reconciliator.harvest.oai_pmh.OAIPMHHarvester.get_earliest_datestamp",
        return_value=None,
    ):
        with patch(
            "reconciliator.harvest.oai_pmh.OAIPMHHarvester.harvest_partition"
        ) as mock_harvest:
            mock_harvest.return_value = []
            records = list(stream_oai_records(source_name="melinda", config=config))
            assert records == []
            mock_harvest.assert_called_once_with(
                None,
                None,
                tracker=None,
                on_record_harvested=None,
            )


def test_stream_oai_records_auto_discovery_multi_worker() -> None:
    from reconciliator.config import OAIIngestConfig
    from reconciliator.ingestion.oai import stream_oai_records

    config = OAIIngestConfig(
        source_type="oai",
        oai_url="http://example.org/oai",
        workers=3,
        from_date=None,
        until_date="2000-03-01",
        slice_days=30,
    )

    with patch(
        "reconciliator.harvest.oai_pmh.OAIPMHHarvester.get_earliest_datestamp",
        return_value="2000-01-01",
    ):
        with patch(
            "reconciliator.harvest.oai_pmh.OAIPMHHarvester.harvest_partition"
        ) as mock_harvest:
            mock_harvest.return_value = []
            records = list(stream_oai_records(source_name="melinda", config=config))
            assert records == []
            assert mock_harvest.call_count == 2


def test_stream_oai_records_single_worker_no_identify() -> None:
    from reconciliator.config import OAIIngestConfig
    from reconciliator.ingestion.oai import stream_oai_records

    config = OAIIngestConfig(
        source_type="oai",
        oai_url="http://example.org/oai",
        workers=1,
        from_date=None,
        until_date=None,
    )

    with patch("reconciliator.harvest.oai_pmh.OAIPMHHarvester.get_earliest_datestamp") as mock_id:
        with patch(
            "reconciliator.harvest.oai_pmh.OAIPMHHarvester.harvest_partition"
        ) as mock_harvest:
            mock_harvest.return_value = []
            records = list(stream_oai_records(source_name="melinda", config=config))
            assert records == []
            mock_id.assert_not_called()
            mock_harvest.assert_called_once_with(None, None, tracker=None, on_record_harvested=None)


def test_stream_oai_records_from_date_defaults_until_to_today() -> None:
    from reconciliator.config import OAIIngestConfig
    from reconciliator.ingestion.oai import stream_oai_records

    config = OAIIngestConfig(
        source_type="oai",
        oai_url="http://example.org/oai",
        workers=2,
        from_date="2026-08-01",
        until_date=None,
        slice_days=10,
    )

    with patch("reconciliator.harvest.oai_pmh.OAIPMHHarvester.harvest_partition") as mock_harvest:
        mock_harvest.return_value = []
        records = list(stream_oai_records(source_name="melinda", config=config))
        assert records == []
        assert mock_harvest.call_count >= 1


def test_ingest_oai_pipeline_propagates_producer_error() -> None:
    import pytest

    from reconciliator.config import OAIIngestConfig
    from reconciliator.ingestion.oai import ingest_oai_pipeline

    config = OAIIngestConfig(
        source_type="file",
        input_path=Path("dummy.xml"),
        batch_size=2,
        normalization_workers=2,
    )
    store = MagicMock()
    normalizer = MagicMock()
    embedder = MagicMock()
    tracker = MagicMock()

    def failing_stream(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("Harvester stream failed abruptly")
        yield {}

    with patch("reconciliator.ingestion.oai.stream_oai_records", side_effect=failing_stream):
        with pytest.raises(RuntimeError, match="Harvester stream failed abruptly"):
            ingest_oai_pipeline(
                "melinda",
                config,
                store,
                normalizer,
                embedder,
                tracker=tracker,
            )
        tracker.fail_task.assert_called_once()


def test_ingest_oai_pipeline_decoupled_harvest_queue() -> None:
    import time

    from reconciliator.config import OAIIngestConfig
    from reconciliator.ingestion.oai import ingest_oai_pipeline
    from reconciliator.models import BibliographicRecord

    config = OAIIngestConfig(
        source_type="file",
        input_path=Path("dummy.xml"),
        batch_size=2,
        normalization_workers=2,
        queue_size=20,
        batch_queue_size=2,
    )
    store = MagicMock()
    normalizer = MagicMock()
    normalizer.normalize.side_effect = lambda r: BibliographicRecord(
        source="melinda", source_id=r["source_id"], title=r["title"]
    )
    normalizer.normalize_id.side_effect = lambda rid: str(rid)

    embedder = MagicMock()

    def slow_embed(batch: list[Any]) -> np.ndarray:
        time.sleep(0.02)
        return np.zeros((len(batch), 4), dtype=np.float32)

    embedder.embed_records.side_effect = slow_embed

    raw_stream = [{"source_id": f"rec_{i}", "title": f"Book {i}"} for i in range(10)]

    with patch("reconciliator.ingestion.oai.stream_oai_records", return_value=raw_stream):
        total = ingest_oai_pipeline(
            "melinda",
            config,
            store,
            normalizer,
            embedder,
        )
        assert total == 10
        assert store.insert_records_batch.call_count == 5


def test_ingest_oai_pipeline_preserves_skipped_in_telemetry() -> None:
    from reconciliator.config import OAIIngestConfig
    from reconciliator.ingestion.oai import ingest_oai_pipeline
    from reconciliator.models import BibliographicRecord

    config = OAIIngestConfig(
        source_type="oai",
        oai_url="https://example.org/oai",
        batch_size=2,
        normalization_workers=1,
    )
    store = MagicMock()
    store.get_indexed_source_ids.return_value = {"rec_old_1", "rec_old_2"}

    normalizer = MagicMock()
    normalizer.normalize.side_effect = lambda r: BibliographicRecord(
        source="melinda", source_id=r["source_id"], title=r["title"]
    )
    normalizer.normalize_id.side_effect = lambda rid: str(rid)

    embedder = MagicMock()
    embedder.embed_records.return_value = np.zeros((2, 4), dtype=np.float32)

    tracker = MagicMock()

    raw_stream = [
        {"source_id": "rec_old_1", "title": "Old 1"},
        {"source_id": "rec_new_1", "title": "New 1"},
        {"source_id": "rec_old_2", "title": "Old 2"},
        {"source_id": "rec_new_2", "title": "New 2"},
    ]

    with patch("reconciliator.ingestion.oai.stream_oai_records", return_value=raw_stream):
        total = ingest_oai_pipeline(
            "melinda",
            config,
            store,
            normalizer,
            embedder,
            tracker=tracker,
            skip_existing=True,
        )
        assert total == 2
        # Verify complete_task was called with metadata containing skipped == 2
        tracker.complete_task.assert_called_once()
        meta = tracker.complete_task.call_args.kwargs.get("metadata", {})
        assert meta.get("skipped") == 2
        assert meta.get("indexed") == 2
