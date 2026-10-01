"""Two-pass streaming ingestion pipeline for gzipped Wikidata dump latest-all.json.gz.

Pass 1: Pre-scan all human/organization/place entities into PostgreSQL agents, identifiers, and agent_attributes tables.
Pass 2: Stream bibliographic entities, hydrating authors from PostgreSQL for true O(1) memory.
"""

from __future__ import annotations

from reconciliator.ingestion.wikidata.authors import (
    _extract_author_data,
    _prescan_authors,
    _worker_parse_authors_chunk,
)
from reconciliator.ingestion.wikidata.cache import (
    AuthorLRUCache,
    extract_referenced_qids,
    prefetch_batch_qids,
)
from reconciliator.ingestion.wikidata.claims import (
    _AUTHOR_PROPERTIES,
    _BOOK_TYPES,
    _CREATIVE_WORK_MAPPING,
    _CREATIVE_WORK_TYPES,
    _DEFAULT_WIKIDATA_CONFIG,
    _HUMAN_AND_ENTITY_TYPES,
    WIKIDATA_LANG_MAP,
    WIKIDATA_PREF_LANGS,
    extract_claim_qualifier_string,
    extract_wikidata_yso_aliases,
    get_claim_qids,
    get_claim_snak_value,
    is_qid,
)
from reconciliator.ingestion.wikidata.decomposition import (
    _lookup_entity_name,
    decompose_wikidata_entity,
    parse_wikidata_entity,
)
from reconciliator.ingestion.wikidata.pipeline import (
    ingest_wikidata_pipeline,
    ingest_wikidata_wemi_pipeline,
)
from reconciliator.ingestion.wikidata.streaming import (
    AUTHOR_INDICATORS,
    AUTHOR_INDICATORS_RE,
    BIB_INDICATORS,
    BIB_INDICATORS_RE,
    _chunk_stream_lines,
    _has_author_indicator,
    _has_bib_indicator,
    _stream_bibliographic_entities,
    _worker_parse_raw_bib_chunk,
    find_pigz_binary,
    open_dump_stream,
    stream_wikidata_dump,
)

__all__ = [
    "AUTHOR_INDICATORS",
    "AUTHOR_INDICATORS_RE",
    "AuthorLRUCache",
    "BIB_INDICATORS",
    "BIB_INDICATORS_RE",
    "WIKIDATA_LANG_MAP",
    "WIKIDATA_PREF_LANGS",
    "_AUTHOR_PROPERTIES",
    "_BOOK_TYPES",
    "_CREATIVE_WORK_MAPPING",
    "_CREATIVE_WORK_TYPES",
    "_DEFAULT_WIKIDATA_CONFIG",
    "_HUMAN_AND_ENTITY_TYPES",
    "_chunk_stream_lines",
    "_extract_author_data",
    "_has_author_indicator",
    "_has_bib_indicator",
    "_lookup_entity_name",
    "_prescan_authors",
    "_stream_bibliographic_entities",
    "_worker_parse_authors_chunk",
    "_worker_parse_raw_bib_chunk",
    "decompose_wikidata_entity",
    "extract_claim_qualifier_string",
    "extract_referenced_qids",
    "extract_wikidata_yso_aliases",
    "find_pigz_binary",
    "get_claim_qids",
    "get_claim_snak_value",
    "ingest_wikidata_pipeline",
    "ingest_wikidata_wemi_pipeline",
    "is_qid",
    "open_dump_stream",
    "parse_wikidata_entity",
    "prefetch_batch_qids",
    "stream_wikidata_dump",
]
