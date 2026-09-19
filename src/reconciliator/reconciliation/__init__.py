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
from reconciliator.reconciliation.matcher import WorkMatcher, _merge_funnel_specs
from reconciliator.reconciliation.results import (
    ConstituentWorkMatch,
    ReconciliationResult,
    WEMIMatchResult,
)

__all__ = [
    "WorkMatcher",
    "_merge_funnel_specs",
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

