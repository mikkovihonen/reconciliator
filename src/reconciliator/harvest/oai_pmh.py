"""Asynchronous OAI-PMH harvester with temporal partitioning and resumptionToken pipelining."""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta
from typing import AsyncIterator, Optional

import httpx2
from lxml import etree

from reconciliator.utils.version import get_default_user_agent

logger = logging.getLogger(__name__)

OAI_NS = {
    "oai": "http://www.openarchives.org/OAI/2.0/",
    "marc": "http://www.loc.gov/MARC21/slim",
}

_RESUMPTION_TOKEN_RE = re.compile(
    rb"<(?:\w+:)?resumptionToken[^>]*>([^<]+)</(?:\w+:)?resumptionToken>",
    re.IGNORECASE,
)
_ERROR_RE = re.compile(
    rb"<(?:\w+:)?error\s+[^>]*code=[\"']([^\"']+)[\"'][^>]*>(.*?)</(?:\w+:)?error>",
    re.IGNORECASE | re.DOTALL,
)


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


def extract_page_resumption_token(page_bytes: bytes) -> Optional[str]:
    """Fast extraction of resumptionToken from raw OAI-PMH XML response bytes."""
    match = _RESUMPTION_TOKEN_RE.search(page_bytes)
    if match:
        token = match.group(1).strip()
        if token:
            return token.decode("utf-8", errors="replace")
    return None


def extract_page_error(page_bytes: bytes) -> Optional[tuple[str, str]]:
    """Fast extraction of OAI-PMH error code and message from response bytes."""
    match = _ERROR_RE.search(page_bytes)
    if match:
        code = match.group(1).decode("utf-8", errors="replace").strip()
        msg = match.group(2).decode("utf-8", errors="replace").strip()
        return code, msg
    return None


class OAIPMHHarvester:
    """Asynchronous client for harvesting MARCXML records from OAI-PMH endpoints."""

    def __init__(
        self,
        endpoint_url: str,
        metadata_prefix: str = "marc21",
        set_spec: Optional[str] = None,
        timeout: float = 60.0,
        user_agent: Optional[str] = None,
        max_retries: int = 5,
        base_delay: float = 1.0,
        request_delay: float = 0.05,
        http2: bool = True,
        prefetch: bool = True,
    ) -> None:
        self.endpoint_url = endpoint_url
        self.metadata_prefix = metadata_prefix
        self.set_spec = set_spec
        self.timeout = timeout
        self.user_agent = user_agent if user_agent is not None else get_default_user_agent()
        self.max_retries = max_retries
        self.base_delay = base_delay
        self.request_delay = request_delay
        self.http2 = http2
        self.prefetch = prefetch

    async def _request_with_retry(
        self,
        client: httpx2.AsyncClient,
        params: dict[str, str],
    ) -> httpx2.Response:
        """Execute an asynchronous HTTP GET with exponential backoff on transient failures."""
        for attempt in range(self.max_retries):
            try:
                resp = await client.get(self.endpoint_url, params=params)
                resp.raise_for_status()
                return resp
            except (httpx2.HTTPStatusError, httpx2.TransportError) as exc:
                if attempt == self.max_retries - 1:
                    raise

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
                await asyncio.sleep(delay)

        raise RuntimeError("Retry loop exited without returning or raising")

    async def get_earliest_datestamp(self) -> Optional[str]:
        """Discover the repository earliestDatestamp via OAI-PMH Identify verb."""
        try:
            async with httpx2.AsyncClient(
                timeout=min(self.timeout, 10.0),
                headers={"User-Agent": self.user_agent},
                follow_redirects=True,
                http2=self.http2,
            ) as client:
                resp = await self._request_with_retry(client, {"verb": "Identify"})
                root = etree.fromstring(resp.content)
                elem = root.find(".//oai:earliestDatestamp", OAI_NS)
                if elem is None:
                    elem = root.find(".//earliestDatestamp")
                if elem is None:
                    elem = root.find(".//oai:earliestTimestamp", OAI_NS)
                if elem is None:
                    elem = root.find(".//earliestTimestamp")
                if elem is not None and elem.text and elem.text.strip():
                    raw = elem.text.strip()
                    return str(raw[:10])
        except Exception as exc:
            logger.debug("Failed to query OAI-PMH Identify for earliestDatestamp: %s", exc)
        return None

    async def harvest_partition(
        self,
        from_date: Optional[str] = None,
        until_date: Optional[str] = None,
        resumption_token: Optional[str] = None,
        semaphore: Optional[asyncio.Semaphore] = None,
    ) -> AsyncIterator[tuple[bytes, Optional[str]]]:
        """Asynchronously harvest raw XML pages for a date slice, yielding (page_bytes, next_token).

        If prefetch is enabled, the next page is fetched in the background while the
        current page is yielded and consumed.
        """
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

        current_token = resumption_token

        async with httpx2.AsyncClient(
            timeout=self.timeout,
            headers={"User-Agent": self.user_agent},
            follow_redirects=True,
            http2=self.http2,
        ) as client:

            async def _fetch(tok: Optional[str]) -> bytes:
                req_params = {"verb": "ListRecords", "resumptionToken": tok} if tok else params
                if semaphore:
                    async with semaphore:
                        resp = await self._request_with_retry(client, req_params)
                else:
                    resp = await self._request_with_retry(client, req_params)
                return resp.content

            prefetch_task: Optional[asyncio.Task[bytes]] = None

            while True:
                if prefetch_task is not None:
                    page_content = await prefetch_task
                    prefetch_task = None
                else:
                    page_content = await _fetch(current_token)

                error = extract_page_error(page_content)
                if error:
                    code, msg = error
                    if code == "noRecordsMatch":
                        logger.debug("No records match partition %s - %s", from_date, until_date)
                    else:
                        logger.warning(
                            "OAI-PMH response error for partition %s - %s: %s (%s)",
                            from_date,
                            until_date,
                            msg,
                            code,
                        )
                    break

                next_token = extract_page_resumption_token(page_content)

                # Prefetch next page concurrently if token is available
                if next_token and self.prefetch:
                    prefetch_task = asyncio.create_task(_fetch(next_token))

                yield page_content, next_token

                current_token = next_token
                if not current_token:
                    break

                if self.request_delay > 0:
                    await asyncio.sleep(self.request_delay)
