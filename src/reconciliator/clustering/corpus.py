"""Intra-corpus co-reference resolution and 3-tier WEMI multiplex clusterer.

Provides the backward-compatible CorpusClusterer facade, routing between
discrete graph-based clustering (MultiplexGraphClusterer) and continuous
mathematical programming on Metal GPU (BigClustMultiplexClusterer).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Optional

from reconciliator.authority.lifecycle import ClusterIdentityManager
from reconciliator.clustering.factory import create_corpus_clusterer
from reconciliator.clustering.graph import MultiplexGraphClusterer, _lcs_length
from reconciliator.clustering.protocol import BaseCorpusClusterer, CorpusClustererProtocol
from reconciliator.clustering.wemi_builder import (
    extract_expression_partition_keys,
    extract_manifestation_key,
)
from reconciliator.config import WEMIClusterConfig
from reconciliator.database import WEMIRepository
from reconciliator.models import MatchRecord, MultiplexClusterResult
from reconciliator.reconciliation import WorkMatcher

__all__ = ["CorpusClusterer", "_lcs_length"]


class CorpusClusterer(BaseCorpusClusterer):
    """Facade and router for intra-corpus WEMI multiplex clustering."""

    def __init__(
        self,
        corpus_repo: WEMIRepository,
        wemi_repo: Optional[WEMIRepository] = None,
        matcher: Optional[WorkMatcher] = None,
        config: Optional[WEMIClusterConfig] = None,
        corpus_origins: Optional[list[str]] = None,
        identity_manager: Optional[ClusterIdentityManager] = None,
    ) -> None:
        self.corpus_repo = corpus_repo
        self.wemi_repo = wemi_repo if wemi_repo is not None else corpus_repo
        self.matcher = matcher
        self.config = config or WEMIClusterConfig()
        self.corpus_origins = corpus_origins
        self.identity_manager = identity_manager or ClusterIdentityManager(self.corpus_repo)

        # Active strategy resolved via factory
        self._strategy: CorpusClustererProtocol = create_corpus_clusterer(
            config=self.config,
            corpus_repo=self.corpus_repo,
            wemi_repo=self.wemi_repo,
            matcher=self.matcher,
            corpus_origins=self.corpus_origins,
            identity_manager=self.identity_manager,
        )

        # Fallback graph clusterer for helper methods (blocking keys, etc.)
        self._graph_helper: Optional[MultiplexGraphClusterer] = (
            self._strategy
            if isinstance(self._strategy, MultiplexGraphClusterer)
            else None
        )

    def _ensure_graph_helper(self) -> MultiplexGraphClusterer:
        if self._graph_helper is None:
            self._graph_helper = MultiplexGraphClusterer(
                corpus_repo=self.corpus_repo,
                wemi_repo=self.wemi_repo,
                matcher=self.matcher,
                config=self.config,
                corpus_origins=self.corpus_origins,
                identity_manager=self.identity_manager,
            )
        return self._graph_helper

    def generate_blocking_keys(self, target: MatchRecord) -> list[str]:
        """Generate deterministic blocking keys across all key families and identifiers."""
        return self._ensure_graph_helper().generate_blocking_keys(target)

    def compute_pairwise_score(
        self,
        r1: MatchRecord,
        r2: MatchRecord,
        ids1: dict[str, list[str]],
        ids2: dict[str, list[str]],
    ) -> tuple[float, str]:
        """Compute composite pairwise similarity score and evidence between two records."""
        return self._ensure_graph_helper().compute_pairwise_score(r1, r2, ids1, ids2)

    def extract_expression_key(self, target: MatchRecord) -> tuple[str, str, str]:
        """Extract (content_type, language, sub_key) partition signature for expression sub-clustering."""
        return extract_expression_partition_keys(target, music_cfg=self.config.music_score)

    def extract_manifestation_key(self, target: MatchRecord) -> str:
        """Extract carrier publication signature for manifestation duplicate clustering."""
        return extract_manifestation_key(target)

    def cluster_records(
        self,
        records: Sequence[MatchRecord],
        persist: bool = False,
        incremental: bool = False,
    ) -> MultiplexClusterResult:
        """Cluster explicit MatchRecords without re-querying the database."""
        return self._strategy.cluster_records(records, persist=persist, incremental=incremental)

    def cluster_corpus(self, origins: Optional[list[str]] = None) -> MultiplexClusterResult:
        """Execute full intra-corpus co-reference clustering pipeline across staged records."""
        actual_origins = origins or self.corpus_origins
        return self._strategy.cluster_corpus(origins=actual_origins)
