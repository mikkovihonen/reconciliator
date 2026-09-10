"""Multithreaded OAI-PMH harvester with temporal partitioning and resumptionToken handling."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Iterator, Optional

from lxml import etree

try:
    import httpx2 as httpx
except ImportError:
    import httpx  # type: ignore[no-redef]

from reconciliator.progress import ProgressTracker

OAI_NS = {
    "oai": "http://www.openarchives.org/OAI/2.0/",
    "marc": "http://www.loc.gov/MARC21/slim",
}


def partition_date_range(
    from_date_str: Optional[str],
    until_date_str: Optional[str],
    slice_days: int = 30,
) -> list[tuple[Optional[str], Optional[str]]]:
    """Divide a date range into parallel temporal slices."""
    if not from_date_str or not until_date_str:
        return [(from_date_str, until_date_str)]

    fmt = "%Y-%m-%d"
    start = datetime.strptime(from_date_str, fmt)
    end = datetime.strptime(until_date_str, fmt)
    if start >= end:
        return [(from_date_str, until_date_str)]

    slices: list[tuple[Optional[str], Optional[str]]] = []
    curr = start
    while curr < end:
        next_dt = min(curr + timedelta(days=slice_days), end)
        slices.append((curr.strftime(fmt), next_dt.strftime(fmt)))
        curr = next_dt + timedelta(days=1)

    return slices


class OAIPMHHarvester:
    """Client for harvesting MARCXML records from OAI-PMH endpoints."""

    def __init__(
        self,
        endpoint_url: str = "https://oai-pmh.api.melinda.kansalliskirjasto.fi/bib",
        metadata_prefix: str = "marc21",
        set_spec: Optional[str] = None,
        timeout: float = 60.0,
    ) -> None:
        self.endpoint_url = endpoint_url
        self.metadata_prefix = metadata_prefix
        self.set_spec = set_spec
        self.timeout = timeout

    def harvest_partition(
        self,
        from_date: Optional[str] = None,
        until_date: Optional[str] = None,
        tracker: Optional[ProgressTracker] = None,
    ) -> Iterator[etree._Element]:
        """Harvest records for a given date slice, following resumptionTokens."""
        client = httpx.Client(timeout=self.timeout)
        params: dict[str, str] = {
            "verb": "ListRecords",
            "metadataPrefix": self.metadata_prefix,
        }
        if self.set_spec:
            params["set"] = self.set_spec
        if from_date:
            params["from"] = from_date
        if until_date:
            params["until"] = until_date

        resumption_token: Optional[str] = None

        try:
            while True:
                if resumption_token:
                    req_params = {
                        "verb": "ListRecords",
                        "resumptionToken": resumption_token,
                    }
                else:
                    req_params = params

                resp = client.get(self.endpoint_url, params=req_params)
                resp.raise_for_status()

                root = etree.fromstring(resp.content)
                records = root.findall(".//oai:record", OAI_NS)
                if not records:
                    records = root.findall(".//record")

                for rec in records:
                    metadata_elem = rec.find("oai:metadata", OAI_NS)
                    if metadata_elem is not None and len(metadata_elem) > 0:
                        yield metadata_elem[0]
                    else:
                        marc_rec = rec.find(".//marc:record", OAI_NS)
                        if marc_rec is not None:
                            yield marc_rec
                        else:
                            yield rec

                    if tracker:
                        tracker.update_progress(items_increment=1)

                # Check resumptionToken
                token_elem = root.find(".//oai:resumptionToken", OAI_NS)
                if token_elem is None:
                    token_elem = root.find(".//resumptionToken")

                if token_elem is not None and token_elem.text and token_elem.text.strip():
                    resumption_token = token_elem.text.strip()
                else:
                    break
        finally:
            client.close()
