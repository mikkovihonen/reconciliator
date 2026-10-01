"""Modular domain repository mixins for WEMIRepository."""

from reconciliator.database.mixins.agent import AuthorityAgentRepositoryMixin
from reconciliator.database.mixins.changeset import ChangesetRepositoryMixin
from reconciliator.database.mixins.cluster import ClusterRepositoryMixin
from reconciliator.database.mixins.common import row_to_dict
from reconciliator.database.mixins.consolidation import ConsolidationRepositoryMixin
from reconciliator.database.mixins.harvest_state import HarvestStateRepositoryMixin
from reconciliator.database.mixins.lookup import EntityLookupRepositoryMixin
from reconciliator.database.mixins.match_records import MatchRecordRepositoryMixin
from reconciliator.database.mixins.pagination import PaginationRepositoryMixin
from reconciliator.database.mixins.search import SearchRepositoryMixin
from reconciliator.database.mixins.source import SourceManagementMixin
from reconciliator.database.mixins.vector_cache import VectorCacheRepositoryMixin

__all__ = [
    "AuthorityAgentRepositoryMixin",
    "ChangesetRepositoryMixin",
    "ClusterRepositoryMixin",
    "ConsolidationRepositoryMixin",
    "EntityLookupRepositoryMixin",
    "HarvestStateRepositoryMixin",
    "MatchRecordRepositoryMixin",
    "PaginationRepositoryMixin",
    "SearchRepositoryMixin",
    "SourceManagementMixin",
    "VectorCacheRepositoryMixin",
    "row_to_dict",
]
