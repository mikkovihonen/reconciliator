"""Generic canonical representative election for entity and work clusters."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any, Generic, Optional, TypeVar

TRecord = TypeVar("TRecord")


class CanonicalRepresentativeSelector(Generic[TRecord]):
    """Elects the canonical representative record for a cluster based on precedence and completeness."""

    def __init__(
        self,
        source_precedence: Sequence[str] = (),
        source_extractor: Optional[Callable[[TRecord], str]] = None,
        tie_breaker: Optional[Callable[[TRecord], tuple[Any, ...]]] = None,
    ) -> None:
        self.source_precedence = [s.lower() for s in source_precedence]
        self.source_extractor = source_extractor
        self.tie_breaker = tie_breaker

    def compute_rank(self, record: TRecord) -> tuple[Any, ...]:
        """Compute sorting rank tuple for a candidate record (lowest rank wins)."""
        rank_parts: list[Any] = []

        if self.source_extractor and self.source_precedence:
            src = str(self.source_extractor(record) or "").lower()
            try:
                src_rank = self.source_precedence.index(src)
            except ValueError:
                src_rank = len(self.source_precedence) + 1
            rank_parts.append(src_rank)

        if self.tie_breaker is not None:
            rank_parts.extend(self.tie_breaker(record))

        return tuple(rank_parts)

    def select_canonical(self, records: Sequence[TRecord]) -> TRecord:
        """Select the highest-ranking canonical record from a sequence of cluster members."""
        if not records:
            raise ValueError("Cannot select canonical representative from an empty sequence.")
        return sorted(records, key=self.compute_rank)[0]
