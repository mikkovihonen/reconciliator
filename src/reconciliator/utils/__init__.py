"""Shared general-purpose utilities for the reconciliator package."""

from __future__ import annotations

from reconciliator.utils.curie import (
    clear_curie_cache,
    extract_source_identifier,
    extract_wikidata_qid,
    format_source_uri,
    format_wikidata_uri,
    parse_curie,
    to_curie,
)
from reconciliator.utils.device import (
    configure_execution_threads,
    configure_mixed_precision,
    get_optimal_batch_size,
    resolve_device,
)
from reconciliator.utils.durations import parse_duration_seconds
from reconciliator.utils.entity_keys import to_manifestation_entity_keys
from reconciliator.utils.metrics import compute_jaccard_overlap
from reconciliator.utils.module_ref import (
    ModuleRefError,
    is_path_reference,
    resolve_module_ref,
)
from reconciliator.utils.strings import clean_alphanumeric
from reconciliator.utils.target_extraction import (
    extract_identifiers_from_target,
    extract_target_analytics,
    extract_target_track_titles,
)
from reconciliator.utils.tsv import sanitize_tsv_cell, write_summary_tsv, write_tsv_file
from reconciliator.utils.union_find import UnionFind, cluster_edges
from reconciliator.utils.version import (
    __version__,
    get_default_user_agent,
    get_version,
)

__all__ = [
    "UnionFind",
    "cluster_edges",
    "clean_alphanumeric",
    "compute_jaccard_overlap",
    "extract_identifiers_from_target",
    "extract_target_analytics",
    "extract_target_track_titles",
    "parse_duration_seconds",
    "sanitize_tsv_cell",
    "to_manifestation_entity_keys",
    "write_summary_tsv",
    "write_tsv_file",
    # curie
    "clear_curie_cache",
    "extract_source_identifier",
    "extract_wikidata_qid",
    "format_source_uri",
    "format_wikidata_uri",
    "parse_curie",
    "to_curie",
    # device
    "configure_execution_threads",
    "configure_mixed_precision",
    "get_optimal_batch_size",
    "resolve_device",
    # module_ref
    "ModuleRefError",
    "is_path_reference",
    "resolve_module_ref",
    # version
    "__version__",
    "get_default_user_agent",
    "get_version",
]
