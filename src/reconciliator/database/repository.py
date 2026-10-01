"""Unified WEMI repository aggregating domain database mixins."""

from __future__ import annotations

import logging
from collections import OrderedDict
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from sqlalchemy.engine import Engine

from reconciliator.config import VectorEngineConfig
from reconciliator.database.engine import init_authority_db
from reconciliator.database.mixins import (
    AuthorityAgentRepositoryMixin,
    ChangesetRepositoryMixin,
    ClusterRepositoryMixin,
    ConsolidationRepositoryMixin,
    EntityLookupRepositoryMixin,
    HarvestStateRepositoryMixin,
    MatchRecordRepositoryMixin,
    PaginationRepositoryMixin,
    SearchRepositoryMixin,
    SourceManagementMixin,
    VectorCacheRepositoryMixin,
    WEMIIngestionRepositoryMixin,
)
from reconciliator.indexer import VectorIndex, create_vector_engine
from reconciliator.models import (
    EntityType,
    ExpressionEntity,
    ManifestationEntity,
    WorkEntity,
)

if TYPE_CHECKING:
    from reconciliator.config.matching import RepositoryFTSConfig

logger = logging.getLogger(__name__)


class WEMIRepository(
    VectorCacheRepositoryMixin,
    MatchRecordRepositoryMixin,
    HarvestStateRepositoryMixin,
    ClusterRepositoryMixin,
    AuthorityAgentRepositoryMixin,
    ChangesetRepositoryMixin,
    SearchRepositoryMixin,
    PaginationRepositoryMixin,
    SourceManagementMixin,
    ConsolidationRepositoryMixin,
    EntityLookupRepositoryMixin,
    WEMIIngestionRepositoryMixin,
):
    """Unified repository managing WEMI tables, corpus staging/clustering, harvest state,
    FTS lexical search, and tripartite vector indices in authority.db and its corpus database.
    """

    def __init__(
        self,
        db_path: Path | str | None = None,
        work_index_path: Optional[Path | str] = None,
        manifestation_index_path: Optional[Path | str] = None,
        dimension: int = 768,
        vector_engine_config: Optional[VectorEngineConfig] = None,
        fts_config: Optional["RepositoryFTSConfig"] = None,
        work_indexer: Optional[VectorIndex] = None,
        manifestation_indexer: Optional[VectorIndex] = None,
        engine: Optional[Engine] = None,
        entity_cache_size: Optional[int] = None,
    ) -> None:
        self.db_path = db_path
        self.dimension = dimension
        self.fts_config = fts_config

        if engine is not None:
            self.engine = engine
        else:
            if db_path is None:
                raise ValueError("Must provide either engine or db_path to WEMIRepository")
            from reconciliator.database.engine import get_engine

            self.engine = get_engine(db_path)

        if entity_cache_size is not None:
            self.entity_cache_size = entity_cache_size
        else:
            from reconciliator.config.defaults import get_bundled_reconcile_defaults

            self.entity_cache_size = int(
                get_bundled_reconcile_defaults().get("entity_cache_size", 50000)
            )

        self._work_cache: OrderedDict[int, WorkEntity] = OrderedDict()
        self._manifestation_cache: OrderedDict[int, ManifestationEntity] = OrderedDict()
        self._expressions_for_work_cache: OrderedDict[int, list[ExpressionEntity]] = OrderedDict()
        self._identifier_lookup_cache: OrderedDict[
            tuple[str, str, bool], list[tuple[EntityType, int]]
        ] = OrderedDict()

        init_authority_db(self.engine)

        # Work indexer
        if work_indexer is not None:
            self.work_indexer = work_indexer
        elif work_index_path is not None:
            self.work_indexer = create_vector_engine(
                Path(work_index_path), dimension=self.dimension, config=vector_engine_config
            )
        else:
            self.work_indexer = None

        # Manifestation indexer
        if manifestation_indexer is not None:
            self.manifestation_indexer = manifestation_indexer
        elif manifestation_index_path is not None:
            self.manifestation_indexer = create_vector_engine(
                Path(manifestation_index_path),
                dimension=self.dimension,
                config=vector_engine_config,
            )
        else:
            self.manifestation_indexer = None

        # Agent indexer
        self.agent_indexer = None

    def get_connection(self) -> Any:
        """Obtain connection with an active transaction."""
        return self.engine.begin()

    def get_read_connection(self) -> Any:
        """Obtain lightweight read connection without transaction wrapping."""
        return self.engine.connect()

    def clear_entity_caches(self) -> None:
        """Clear all in-memory LRU entity and lookup caches."""
        self._work_cache.clear()
        self._manifestation_cache.clear()
        self._expressions_for_work_cache.clear()
        self._identifier_lookup_cache.clear()

    def get_corpus_connection(self) -> Any:
        """Corpus staging shares the authority database."""
        return self.get_connection()

    def close(self) -> None:
        """Dispose SQLAlchemy engine and release connection pool."""
        self.engine.dispose()
