"""Unit tests for multithreaded OAI-PMH harvester and date partitioning."""

from unittest.mock import MagicMock, patch

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
