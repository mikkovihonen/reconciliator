"""ResourceSync (ANSI/NISO Z39.99) harvester for Linked Data repositories and BIBFRAME / BFFI feeds."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Iterator, Optional

import httpx2
from lxml import etree

from reconciliator.version import get_default_user_agent

logger = logging.getLogger(__name__)

RS_NAMESPACES = {
    "sitemap": "http://www.sitemaps.org/schemas/sitemap/0.9",
    "rs": "http://www.openarchives.org/rs/terms/",
}


@dataclass
class ResourceSyncItem:
    """Metadata for a single synchronized linked data resource."""

    loc: str
    lastmod: Optional[str] = None
    change: Optional[str] = None
    mime_type: Optional[str] = None
    length: Optional[int] = None
    hash_val: Optional[str] = None


class ResourceSyncHarvester:
    """Client for discovering and harvesting resources via ResourceSync (ANSI/NISO Z39.99)."""

    def __init__(
        self,
        endpoint_url: str,
        timeout: float = 60.0,
        user_agent: Optional[str] = None,
        max_retries: int = 5,
        base_delay: float = 1.0,
        request_delay: float = 0.05,
    ) -> None:
        self.endpoint_url = endpoint_url
        self.timeout = timeout
        self.user_agent = user_agent or get_default_user_agent()
        self.max_retries = max_retries
        self.base_delay = base_delay
        self.request_delay = request_delay

    def _get_with_retry(self, client: httpx2.Client, url: str) -> httpx2.Response:
        """Execute HTTP GET with exponential backoff on transient network failures."""
        for attempt in range(self.max_retries):
            try:
                resp = client.get(url)
                resp.raise_for_status()
                return resp
            except (httpx2.HTTPStatusError, httpx2.TransportError) as exc:
                if attempt == self.max_retries - 1:
                    raise
                retry_after = getattr(getattr(exc, "response", None), "headers", {}).get(
                    "Retry-After"
                )
                if isinstance(retry_after, str) and retry_after.isdigit():
                    delay = float(retry_after)
                else:
                    delay = min(self.base_delay * (2**attempt), 60.0)
                logger.warning(
                    "ResourceSync request to %s failed (attempt %d/%d), retrying in %.1fs: %s",
                    url,
                    attempt + 1,
                    self.max_retries,
                    delay,
                    exc,
                )
                time.sleep(delay)
        raise RuntimeError("Retry loop exited unexpectedly")

    def _parse_urlset_or_index(self, client: httpx2.Client, url: str) -> Iterator[ResourceSyncItem]:
        """Parse a ResourceSync urlset or sitemapindex document."""
        resp = self._get_with_retry(client, url)
        root = etree.fromstring(resp.content)

        # Check if sitemapindex (collection of lists)
        sitemaps = root.findall(".//sitemap:sitemap", RS_NAMESPACES)
        if not sitemaps:
            sitemaps = root.findall(".//sitemap", {})

        if sitemaps:
            for sm in sitemaps:
                loc_elem = sm.find("sitemap:loc", RS_NAMESPACES)
                if loc_elem is None:
                    loc_elem = sm.find("loc")
                if loc_elem is not None and loc_elem.text:
                    sub_url = loc_elem.text.strip()
                    yield from self._parse_urlset_or_index(client, sub_url)
            return

        # Regular urlset
        urls = root.findall(".//sitemap:url", RS_NAMESPACES)
        if not urls:
            urls = root.findall(".//url")

        for u in urls:
            loc_elem = u.find("sitemap:loc", RS_NAMESPACES)
            if loc_elem is None:
                loc_elem = u.find("loc")
            if loc_elem is None or not loc_elem.text:
                continue

            loc = loc_elem.text.strip()
            lastmod_elem = u.find("sitemap:lastmod", RS_NAMESPACES)
            if lastmod_elem is None:
                lastmod_elem = u.find("lastmod")
            lastmod = (
                lastmod_elem.text.strip()
                if (lastmod_elem is not None and lastmod_elem.text)
                else None
            )

            rs_md = u.find("rs:md", RS_NAMESPACES)
            mime_type = rs_md.get("type") if rs_md is not None else None
            change = rs_md.get("change") if rs_md is not None else None
            length = (
                int(rs_md.get("length")) if (rs_md is not None and rs_md.get("length")) else None
            )
            hash_val = rs_md.get("hash") if rs_md is not None else None

            yield ResourceSyncItem(
                loc=loc,
                lastmod=lastmod,
                change=change,
                mime_type=mime_type,
                length=length,
                hash_val=hash_val,
            )

    def list_resources(self) -> Iterator[ResourceSyncItem]:
        """Discover and yield all resource descriptors published in the ResourceSync source."""
        with httpx2.Client(
            timeout=self.timeout,
            headers={"User-Agent": self.user_agent},
            follow_redirects=True,
        ) as client:
            yield from self._parse_urlset_or_index(client, self.endpoint_url)

    def harvest_contents(self) -> Iterator[tuple[ResourceSyncItem, bytes]]:
        """Harvest each linked resource payload over HTTP."""
        with httpx2.Client(
            timeout=self.timeout,
            headers={"User-Agent": self.user_agent},
            follow_redirects=True,
        ) as client:
            for item in self._parse_urlset_or_index(client, self.endpoint_url):
                if self.request_delay > 0:
                    time.sleep(self.request_delay)
                resp = self._get_with_retry(client, item.loc)
                yield item, resp.content
