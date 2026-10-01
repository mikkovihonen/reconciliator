"""Streaming utilities and partition window resolvers for OAI-PMH and local files."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Iterator, Optional

from lxml import etree
from rdflib import Graph

from reconciliator.config import OAIIngestConfig
from reconciliator.harvest.oai_pmh import OAIPMHHarvester
from reconciliator.loaders.bibframe import get_profile, stream_bibframe_records

logger = logging.getLogger(__name__)


async def resolve_harvest_window(
    source_name: str,
    config: OAIIngestConfig,
) -> tuple[Optional[str], Optional[str]]:
    """Resolve the (from, until) window for a partitioned harvest."""
    from_date = config.from_date
    until_date = config.until_date

    if config.workers <= 1 or config.source_type != "oai" or not config.oai_url:
        return from_date, until_date

    if not from_date:
        harvester = OAIPMHHarvester(
            endpoint_url=config.oai_url,
            metadata_prefix=config.metadata_prefix,
            set_spec=config.set_spec,
            request_delay=config.request_delay,
            timeout=config.timeout,
            max_retries=config.max_retries,
            base_delay=config.base_delay,
            http2=config.http2,
            prefetch=config.prefetch,
        )
        discovered = await harvester.get_earliest_datestamp()
        if discovered:
            from_date = discovered
            if not until_date:
                until_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            logger.info(
                "Source '%s': auto-discovered earliestDatestamp '%s'. Partitioning across %d workers up to %s.",
                source_name,
                from_date,
                config.workers,
                until_date,
            )
        else:
            logger.warning(
                "Source '%s': multi-worker harvest requested (%d workers) but from_date was "
                "omitted and earliestDatestamp could not be discovered. Falling back to a "
                "single continuous stream.",
                source_name,
                config.workers,
            )
    elif not until_date:
        until_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    return from_date, until_date


def stream_file_raw_elements(
    source_name: str,
    config: OAIIngestConfig,
) -> Iterator[Any]:
    """Stream raw elements from a local MARCXML or Bibframe file."""
    if not config.input_path:
        raise ValueError(
            f"input_path must be specified for source '{source_name}' when source_type is 'file'"
        )
    if config.format == "bibframe":
        profile = get_profile(config.schema_profile) if config.schema_profile else None
        for _raw_rec, _offset, orig_doc in stream_bibframe_records(
            config.input_path, profile=profile, source=source_name
        ):
            if isinstance(orig_doc, tuple) and orig_doc:
                orig_doc = orig_doc[0]
            if isinstance(orig_doc, Graph):
                yield orig_doc
            else:
                g = Graph()
                import orjson

                g.parse(data=orjson.dumps(orig_doc).decode("utf-8"), format="json-ld")
                yield g
    else:
        path = str(config.input_path)
        f_in: Any
        if path.endswith(".gz"):
            import gzip

            f_in = gzip.open(path, "rb")
        else:
            f_in = open(path, "rb")
        try:
            context = etree.iterparse(f_in, events=("end",))
            for _, elem in context:
                tag = elem.tag
                if tag.endswith("record") and not tag.endswith("collection"):
                    yield elem
                    elem.clear()
        finally:
            f_in.close()
