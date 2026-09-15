"""BIBFRAME (2.0, 3.0) and Finnish BFFI (1.0) graph loaders, streaming parsers, and enrichers."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterator, Optional, Union

from reconciliator.loaders.bibframe.base import (
    bibframe_to_raw_record,
    extract_provenance_tag,
    parse_agent_dict,
)
from reconciliator.loaders.bibframe.decomposition import (
    bibframe_entity_keys,
    build_target_record_from_bibframe,
    decompose_bibframe_work,
    extract_marc_key_components,
)
from reconciliator.loaders.bibframe.enricher import (
    enrich_bibframe_graph,
    enrich_bibframe_jsonld,
)
from reconciliator.loaders.bibframe.jsonld_stream import (
    iter_jsonld_bibframe_records,
    parse_jsonld_dict,
)
from reconciliator.loaders.bibframe.profiles import (
    BIBFRAMESchemaProfile,
    detect_profile,
    get_profile,
)
from reconciliator.loaders.bibframe.rdflib_loader import (
    iter_bibframe_records,
    parse_bibframe_graph,
)


def stream_bibframe_records(
    file_path: Union[Path, str],
    profile: Optional[BIBFRAMESchemaProfile] = None,
    source: str = "target",
    format: Optional[str] = None,
) -> Iterator[tuple[dict[str, Any], int, Any]]:
    """
    Unified entrypoint to stream BIBFRAME / BFFI records from any supported format.
    Delegates to fast streaming JSON-LD parser for JSON/JSON-LD, or RDFLib for Turtle, RDF/XML, N-Triples.
    Yields (raw_record, byte_offset, original_context).
    """
    path = Path(file_path)
    suffix = path.suffix.lower()

    if format in ("json-ld", "jsonld", "json", "ndjson") or suffix in (
        ".json",
        ".jsonld",
        ".ndjson",
    ):
        yield from iter_jsonld_bibframe_records(path, profile=profile, source=source)
    else:
        yield from iter_bibframe_records(path, profile=profile, source=source, format=format)


__all__ = [
    "BIBFRAMESchemaProfile",
    "get_profile",
    "detect_profile",
    "bibframe_to_raw_record",
    "extract_provenance_tag",
    "parse_agent_dict",
    "parse_bibframe_graph",
    "iter_bibframe_records",
    "parse_jsonld_dict",
    "iter_jsonld_bibframe_records",
    "stream_bibframe_records",
    "enrich_bibframe_graph",
    "enrich_bibframe_jsonld",
    "decompose_bibframe_work",
    "bibframe_entity_keys",
    "build_target_record_from_bibframe",
    "extract_marc_key_components",
]
