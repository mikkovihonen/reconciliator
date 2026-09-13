"""Bridge vocabulary discovery and parsing.

Bridges foreign metadata vocabularies (e.g. LoC country codes, LoC language codes,
mode of issuance) to canonical YSO / Finto ontologies via skos:exactMatch.
"""

from reconciliator.vocab.bridges import (
    get_bundled_bridge_dir,
    get_bundled_bridge_files,
    parse_bridge_turtle,
)

__all__ = [
    "get_bundled_bridge_dir",
    "get_bundled_bridge_files",
    "parse_bridge_turtle",
]
