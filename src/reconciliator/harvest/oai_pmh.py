"""Multithreaded OAI-PMH harvester with temporal partitioning and resumptionToken handling."""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta
from typing import Iterator, Optional

import httpx2
from lxml import etree

from reconciliator.progress import ProgressTracker
from reconciliator.version import get_default_user_agent

logger = logging.getLogger(__name__)

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
        user_agent: Optional[str] = None,
        max_retries: int = 5,
        base_delay: float = 1.0,
        request_delay: float = 0.5,
    ) -> None:
        self.endpoint_url = endpoint_url
        self.metadata_prefix = metadata_prefix
        self.set_spec = set_spec
        self.timeout = timeout
        self.user_agent = user_agent if user_agent is not None else get_default_user_agent()
        self.max_retries = max_retries
        self.base_delay = base_delay
        self.request_delay = request_delay

    def _request_with_retry(
        self,
        client: httpx2.Client,
        params: dict[str, str],
    ) -> httpx2.Response:
        """Execute an HTTP GET with exponential backoff on transient failures."""
        for attempt in range(self.max_retries):
            try:
                resp = client.get(self.endpoint_url, params=params)
                resp.raise_for_status()
                return resp
            except (httpx2.HTTPStatusError, httpx2.TransportError) as exc:
                if attempt == self.max_retries - 1:
                    raise

                # Respect Retry-After header if present
                retry_after = getattr(
                    getattr(exc, "response", None),
                    "headers",
                    {},
                ).get("Retry-After")
                if isinstance(retry_after, str) and retry_after.isdigit():
                    delay = float(retry_after)
                else:
                    delay = min(self.base_delay * (2**attempt), 60.0)

                logger.warning(
                    "OAI-PMH request failed (attempt %d/%d), retrying in %.1fs: %s",
                    attempt + 1,
                    self.max_retries,
                    delay,
                    exc,
                )
                time.sleep(delay)

        # Unreachable, but satisfies type checker
        raise RuntimeError("Retry loop exited without returning or raising")  # pragma: no cover

    def harvest_partition(
        self,
        from_date: Optional[str] = None,
        until_date: Optional[str] = None,
        tracker: Optional[ProgressTracker] = None,
    ) -> Iterator[etree._Element]:
        """Harvest records for a given date slice, following resumptionTokens."""
        client = httpx2.Client(
            timeout=self.timeout,
            headers={"User-Agent": self.user_agent},
            follow_redirects=True,
        )
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

                resp = self._request_with_retry(client, req_params)

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

                # Rate limiting between pagination requests
                if self.request_delay > 0:
                    time.sleep(self.request_delay)
        finally:
            client.close()
