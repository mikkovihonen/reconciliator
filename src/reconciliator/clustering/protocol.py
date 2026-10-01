"""Protocol definitions for WEMI corpus clustering engines."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable, Iterator, Sequence
from typing import Any, Optional, Protocol, runtime_checkable

from reconciliator.models import MatchRecord, MultiplexClusterResult


@runtime_checkable
class CorpusClustererProtocol(Protocol):
    """Protocol for WEMI corpus clustering algorithms."""

    def cluster_corpus(self, origins: Optional[list[str]] = None) -> MultiplexClusterResult:
        """Cluster entire staged corpus from repository and persist clusters.

        Args:
            origins: Optional list of source origins to filter records.

        Returns:
            MultiplexClusterResult with cluster counts and elapsed time.
        """
        ...

    def cluster_records(
        self,
        records: Sequence[MatchRecord],
        persist: bool = False,
        incremental: bool = False,
    ) -> MultiplexClusterResult:
        """Cluster an explicit sequence of MatchRecords into WEMI entities.

        Args:
            records: Sequence of MatchRecord items to cluster.
            persist: Whether to persist clusters and linkages to database.
            incremental: Whether to warm-start from existing cluster centroids.

        Returns:
            MultiplexClusterResult with cluster counts and elapsed time.
        """
        ...


class BaseCorpusClusterer(ABC, CorpusClustererProtocol):
    """Abstract base class for corpus clusterers."""

    @abstractmethod
    def cluster_corpus(self, origins: Optional[list[str]] = None) -> MultiplexClusterResult:
        """Cluster entire staged corpus from repository and persist clusters."""
        ...

    @abstractmethod
    def cluster_records(
        self,
        records: Sequence[MatchRecord],
        persist: bool = False,
        incremental: bool = False,
    ) -> MultiplexClusterResult:
        """Cluster an explicit sequence of MatchRecords into WEMI entities."""
        ...

    @property
    def template_engine(self) -> Any:
        """Lazily initialize WEMI template serialization engine."""
        if not hasattr(self, "_template_engine") or self._template_engine is None:
            try:
                from reconciliator.serialization import WEMISerializationEngine

                self._template_engine = WEMISerializationEngine()
            except Exception:
                self._template_engine = None
        return self._template_engine

    def format_work_text(self, record: MatchRecord) -> str:
        """Format high-level work representation focusing on intellectual creation."""
        engine = self.template_engine
        if engine:
            try:
                rendered = engine.render_match_record_work(record)
                if rendered:
                    return rendered
            except Exception:
                pass

        parts = []
        if record.author_key:
            parts.append(f"Author: {record.author_key}")
        if record.uniform_title:
            parts.append(f"Uniform Title: {record.uniform_title}")
        elif record.title:
            parts.append(f"Title: {record.title}")
        if record.work_type:
            parts.append(f"Type: {record.work_type}")
        return " | ".join(parts) if parts else record.record_id

    _format_work_text = format_work_text

    def format_expression_text(self, record: MatchRecord) -> str:
        """Format expression-level representation emphasizing language and realization attributes."""
        engine = self.template_engine
        if engine:
            try:
                rendered = engine.render_match_record_expression(record)
                if rendered:
                    return rendered
            except Exception:
                pass

        parts = []
        if record.title:
            parts.append(f"Title: {record.title}")
        if record.language:
            parts.append(f"Language: {record.language}")
        if record.work_type:
            parts.append(f"Format: {record.work_type}")
        if "translators_signature" in record.keys:
            parts.append(f"Translator: {record.keys['translators_signature'][0]}")
        if record.volume_designation:
            parts.append(f"Edition: {record.volume_designation}")
        return " | ".join(parts) if parts else record.record_id

    _format_expression_text = format_expression_text

    def cluster_records_stream(
        self,
        records_stream: Iterable[MatchRecord],
        chunk_size: int = 10000,
        persist: bool = False,
    ) -> Iterator[MultiplexClusterResult]:
        """Stream and cluster records in out-of-core chunks, minimizing peak memory usage."""
        chunk: list[MatchRecord] = []
        is_first = True
        for rec in records_stream:
            chunk.append(rec)
            if len(chunk) >= chunk_size:
                yield self.cluster_records(chunk, persist=persist, incremental=not is_first)
                chunk = []
                is_first = False
                try:
                    from reconciliator.clustering.bigclust.engine import empty_accelerator_cache

                    empty_accelerator_cache()
                except Exception:
                    pass
        if chunk:
            yield self.cluster_records(chunk, persist=persist, incremental=not is_first)
            try:
                from reconciliator.clustering.bigclust.engine import empty_accelerator_cache

                empty_accelerator_cache()
            except Exception:
                pass

