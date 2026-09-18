"""EntityKey shaping utilities for WEMI loaders and converters."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from reconciliator.models import EntityKey


def to_manifestation_entity_keys(
    manifestation_uri: str, domain_keys: dict[str, list[str]]
) -> list[EntityKey]:
    """Shape a domain keys dictionary into EntityKey rows bound to a manifestation."""
    from reconciliator.models import EntityKey

    return [
        EntityKey(
            entity_type="manifestation",
            entity_id=0,
            key_type=key_type,
            key_value=value,
            entity_uri=manifestation_uri,
        )
        for key_type, values in domain_keys.items()
        for value in values
    ]
