"""In-memory caching and batch hydration utilities for Wikidata entities."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from reconciliator.database import WEMIRepository

_HYDRATION_PROPERTIES: tuple[str, ...] = (
    "P50",
    "P86",
    "P57",
    "P170",
    "P287",
    "P178",
    "P84",
    "P655",
    "P110",
    "P98",
    "P175",
    "P345",
    "P123",
    "P291",
    "P179",
    "P921",
    "P136",
    "P840",
    "P870",
)


class AuthorLRUCache:
    """Bounded in-memory LRU cache for hydrated Wikidata author/agent entity metadata."""

    def __init__(self, maxsize: int = 500000) -> None:
        self.maxsize = max(1, maxsize)
        self._cache: dict[str, Optional[dict[str, Any]]] = {}

    def __len__(self) -> int:
        return len(self._cache)

    def get(self, qid: str) -> Optional[dict[str, Any]]:
        if qid in self._cache:
            val = self._cache.pop(qid)
            self._cache[qid] = val
            return val
        return None

    def __contains__(self, qid: str) -> bool:
        return qid in self._cache

    def set(self, qid: str, data: Optional[dict[str, Any]]) -> None:
        if qid in self._cache:
            self._cache.pop(qid)
        elif len(self._cache) >= self.maxsize:
            next_key = next(iter(self._cache))
            del self._cache[next_key]
        self._cache[qid] = data

    def update(self, mapping: dict[str, Optional[dict[str, Any]]]) -> None:
        for k, v in mapping.items():
            self.set(k, v)

    def clear(self) -> None:
        self._cache.clear()


def extract_referenced_qids(entity: dict[str, Any]) -> set[str]:
    """Collect all entity QIDs requiring author/name hydration."""
    qids: set[str] = set()
    claims = entity.get("claims", {})
    for prop in _HYDRATION_PROPERTIES:
        for stmt in claims.get(prop, []):
            mainsnak = stmt.get("mainsnak", {})
            datavalue = mainsnak.get("datavalue", {})
            val = datavalue.get("value")
            if isinstance(val, dict) and "id" in val:
                qid = val["id"]
                if isinstance(qid, str) and qid.startswith("Q"):
                    qids.add(qid)
    return qids


def prefetch_batch_qids(
    entities: list[dict[str, Any]],
    store: Optional[WEMIRepository],
    cache: AuthorLRUCache,
) -> None:
    """Pre-fetch all referenced QIDs for a batch in single database round-trips."""
    if not store:
        return
    needed_qids: set[str] = set()
    for ent in entities:
        needed_qids.update(extract_referenced_qids(ent))

    missing = [qid for qid in needed_qids if qid not in cache]
    if not missing:
        return

    chunk_size = 5000
    for i in range(0, len(missing), chunk_size):
        chunk = missing[i : i + chunk_size]
        results = store.get_wikidata_authors_batch(chunk)
        cache.update(results)
        for qid in chunk:
            if qid not in cache:
                cache.set(qid, None)
