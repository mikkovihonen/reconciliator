"""Reconciliation subsystem for WEMI multi-tier matching and authority linking."""

from reconciliator.reconciliation.agents import (
    AgentAligner,
    AgentFallbackResolver,
    check_homonym_ambiguity,
    verify_affiliations,
    verify_agent_identifiers,
    verify_languages,
    verify_life_dates,
    verify_publication_chronology,
    verify_qualifier,
    verify_qualifiers,
    verify_subjects,
)
from reconciliator.reconciliation.consolidation import (
    ConsolidationResult,
    WorkConsolidator,
    WorkSignature,
)
from reconciliator.reconciliation.matcher import (
    DEFAULT_MAX_EDITIONS_COMPARED,
    WorkMatcher,
    _merge_funnel_specs,
)
from reconciliator.reconciliation.results import (
    ConstituentWorkMatch,
    ReconciliationResult,
    WEMIMatchResult,
)
from reconciliator.reconciliation.runner import (
    ReconciliationPipelineRunner,
)
from reconciliator.reconciliation.stages import (
    AgentResolutionStage,
    CandidateRankingStage,
    CandidateRetrievalStage,
    IdentifierMatchingStage,
    extract_agent_uris_from_identifiers,
)

__all__ = [
    "ReconciliationPipelineRunner",
    "WorkMatcher",
    "_merge_funnel_specs",
    "DEFAULT_MAX_EDITIONS_COMPARED",
    "IdentifierMatchingStage",
    "CandidateRetrievalStage",
    "CandidateRankingStage",
    "AgentResolutionStage",
    "extract_agent_uris_from_identifiers",
    "ReconciliationResult",
    "ConstituentWorkMatch",
    "WEMIMatchResult",
    "AgentAligner",
    "AgentFallbackResolver",
    "check_homonym_ambiguity",
    "verify_affiliations",
    "verify_agent_identifiers",
    "verify_languages",
    "verify_life_dates",
    "verify_publication_chronology",
    "verify_qualifier",
    "verify_qualifiers",
    "verify_subjects",
    # consolidation
    "ConsolidationResult",
    "WorkConsolidator",
    "WorkSignature",
]
