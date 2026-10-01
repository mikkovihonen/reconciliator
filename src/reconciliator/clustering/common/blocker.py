"""Generic multi-key inverted index candidate blocker for entity co-reference resolution."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterable
from typing import Generic, Optional, TypeVar

TRecord = TypeVar("TRecord")
TId = TypeVar("TId")


class InvertedIndexBlocker(Generic[TRecord, TId]):
    """Generic multi-key inverted index candidate blocker.

    Indexes records into partitioned buckets based on extracted blocking keys,
    filtering oversized/undersized buckets and generating deduplicated symmetric candidate pairs.
    """

    def __init__(
        self,
        key_extractor: Callable[[TRecord], Iterable[str]],
        id_extractor: Callable[[TRecord], TId],
        min_bucket_size: int = 2,
        max_bucket_size: int = 200,
    ) -> None:
        self.key_extractor = key_extractor
        self.id_extractor = id_extractor
        self.min_bucket_size = min_bucket_size
        self.max_bucket_size = max_bucket_size

    def build_index(self, records: Iterable[TRecord]) -> dict[str, list[TId]]:
        """Index records into bucket lists keyed by extracted blocking strings."""
        buckets: dict[str, list[TId]] = defaultdict(list)
        for r in records:
            rid = self.id_extractor(r)
            for k in self.key_extractor(r):
                if k and k.strip():
                    buckets[k.strip()].append(rid)
        return buckets

    def generate_candidate_pairs_from_index(
        self,
        buckets: dict[str, list[TId]],
        max_bucket_size: Optional[int] = None,
    ) -> set[tuple[TId, TId]]:
        """Generate deduplicated candidate pairs from pre-built inverted index buckets."""
        max_size = max_bucket_size if max_bucket_size is not None else self.max_bucket_size
        candidates: set[tuple[TId, TId]] = set()

        for _, members in buckets.items():
            n = len(members)
            if self.min_bucket_size <= n <= max_size:
                for i in range(n):
                    m_i = members[i]
                    for j in range(i + 1, n):
                        m_j = members[j]
                        pair = (m_i, m_j) if m_i < m_j else (m_j, m_i)
                        candidates.add(pair)

        return candidates

    def generate_candidate_pairs(
        self,
        records: Iterable[TRecord],
        max_bucket_size: Optional[int] = None,
    ) -> set[tuple[TId, TId]]:
        """Generate deduplicated candidate pairs directly from record collection."""
        buckets = self.build_index(records)
        return self.generate_candidate_pairs_from_index(
            buckets, max_bucket_size=max_bucket_size
        )
