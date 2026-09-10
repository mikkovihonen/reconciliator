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
