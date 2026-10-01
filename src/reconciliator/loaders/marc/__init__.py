"""MARC record parsing, classification, constituent extraction, authority processing, and WEMI decomposition."""

from reconciliator.loaders.marc.authority import (
    decompose_marc_authority,
    extract_agent_qualifiers,
    is_marc_authority_record,
    resolve_marc_relationship_type,
)
from reconciliator.loaders.marc.classification import (
    CARRIER_TYPE_MAPPINGS,
    CONTENT_TYPE_WORK_TYPES,
    GENRE_PATTERNS,
    GENRE_WORK_TYPES,
    RDA_CONTENT_TYPES,
    TACTILE_CONTENT_CODES,
    determine_carrier_type,
    determine_content_type,
    determine_work_type,
)
from reconciliator.loaders.marc.constituents import (
    extract_marc_constituents,
)
from reconciliator.loaders.marc.decomposition import (
    DecomposedWEMIResult,
    build_target_record_from_marc,
    decompose_marc_wemi,
)
from reconciliator.loaders.marc.parsing import (
    extract_marc_analytics,
    extract_marc_source_id,
    iter_marc_record_elements,
    marc_main_entry_name,
    parse_marc_file_fast,
    parse_marc_items,
    parse_marc_record_elem,
    parse_marc_xml_str_fast,
)
from reconciliator.loaders.marc.xml_utils import (
    MARC_NS,
    _get_controlfield,
    _get_datafields,
    _get_leader,
    _get_subfield,
    _get_subfields,
    derive_agent_id,
    derive_content_id,
)

__all__ = [
    # XML Navigation & Identity
    "MARC_NS",
    "_get_controlfield",
    "_get_datafields",
    "_get_leader",
    "_get_subfield",
    "_get_subfields",
    "derive_agent_id",
    "derive_content_id",
    # Classification
    "CARRIER_TYPE_MAPPINGS",
    "CONTENT_TYPE_WORK_TYPES",
    "GENRE_PATTERNS",
    "GENRE_WORK_TYPES",
    "RDA_CONTENT_TYPES",
    "TACTILE_CONTENT_CODES",
    "determine_carrier_type",
    "determine_content_type",
    "determine_work_type",
    # Authority
    "decompose_marc_authority",
    "extract_agent_qualifiers",
    "is_marc_authority_record",
    "resolve_marc_relationship_type",
    # Constituents
    "extract_marc_constituents",
    # Parsing
    "extract_marc_analytics",
    "extract_marc_source_id",
    "iter_marc_record_elements",
    "marc_main_entry_name",
    "parse_marc_file_fast",
    "parse_marc_items",
    "parse_marc_record_elem",
    "parse_marc_xml_str_fast",
    # Decomposition
    "DecomposedWEMIResult",
    "build_target_record_from_marc",
    "decompose_marc_wemi",
]
