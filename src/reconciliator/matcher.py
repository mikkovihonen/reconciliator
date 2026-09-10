"""Offline multi-tier reconciliation engine combining exact lookup, dense FAISS, and sparse reranking."""

from __future__ import annotations

from dataclasses import dataclass, field

from reconciliator.agent_transfer import AgentAligner
from reconciliator.config import ReconcileConfig
from reconciliator.embedder import SentenceEmbedder
from reconciliator.models import AgentURI, BibliographicRecord, ReconciliationMatch
from reconciliator.repository import AuthorityStore
from reconciliator.sparse import SparseReranker
from reconciliator.vocabularies import VocabularyManager


@dataclass
class ReconciliationResult:
    """Consolidated reconciliation result for a single target record."""

    target_record: BibliographicRecord
    matches: list[ReconciliationMatch] = field(default_factory=list)
    accepted_candidates: list[BibliographicRecord] = field(default_factory=list)
    discovered_agent_uris: dict[int, list[AgentURI]] = field(default_factory=dict)
    selected_agent_uris: dict[int, list[str]] = field(default_factory=dict)
    suppressed_agent_uris: dict[int, list[str]] = field(default_factory=dict)


class ReconciliationEngine:
    """Orchestrates multi-tier offline reconciliation against local authority storage."""

    def __init__(
        self,
        authority_store: AuthorityStore,
        embedder: SentenceEmbedder,
        config: ReconcileConfig,
    ) -> None:
        self.authority_store = authority_store
        self.embedder = embedder
        self.config = config

        self.sparse_reranker = SparseReranker(config.sparse_rerank)
        self.vocab_manager = VocabularyManager(
            allowed_vocabularies=config.vocabularies,
            agent_priority=config.agent_priority,
            subject_priority=config.subject_priority,
            link_policy=config.link_policy,
        )
        self.agent_aligner = AgentAligner()

    def reconcile_record(self, target_record: BibliographicRecord) -> ReconciliationResult:
        """Run complete multi-tier matching funnel on target record."""
        matches: list[ReconciliationMatch] = []
        accepted_candidates: list[BibliographicRecord] = []
        seen_cand_ids: set[str] = set()

        # Step 2.2: Tier 1 Exact ID Lookup
        exact_candidates = self.authority_store.find_exact_matches(target_record.identifiers)
        for cand in exact_candidates:
            if cand.source_id not in seen_cand_ids:
                seen_cand_ids.add(cand.source_id)
                match = ReconciliationMatch(
                    candidate_record_id=cand.source_id,
                    source=cand.source,
                    match_tier="tier1_exact_id",
                    match_score=1.0,
                    dense_score=None,
                    sparse_score=None,
                    volume_match=True,
                    surname_match=True,
                    year_diff=None,
                    accepted=True,
                )
                matches.append(match)
                accepted_candidates.append(cand)

        # Step 2.3: Tier 2 FAISS Dense Retrieval & Step 2.4: Sparse Reranking
        dense_candidates = []
        if self.authority_store.indexer.total_vectors > 0:
            query_vector = self.embedder.embed_records([target_record])
            dense_candidates = self.authority_store.search_vector_candidates(
                query_vector, top_k=self.config.sparse_rerank.top_k
            )

        for cand_record, dense_score in dense_candidates:
            if cand_record.source_id in seen_cand_ids:
                continue
            seen_cand_ids.add(cand_record.source_id)

            match = self.sparse_reranker.evaluate_candidate(
                target_record=target_record,
                candidate_record=cand_record,
                dense_score=dense_score,
                acceptance_threshold=self.config.threshold,
            )
            matches.append(match)
            if match.accepted:
                accepted_candidates.append(cand_record)

        # Step 2.5: Tier 4 Agent Alignment & Multi-Candidate URI Aggregation
        discovered_uris = self.agent_aligner.align_target_agents(
            target_record.agents, accepted_candidates
        )

        # Step 2.6: Vocabulary Filter & Priority Selection
        selected_uris: dict[int, list[str]] = {}
        suppressed_uris: dict[int, list[str]] = {}

        for idx, uri_items in discovered_uris.items():
            raw_uri_strs = [item.uri for item in uri_items]
            sel, sup = self.vocab_manager.select_agent_uris(raw_uri_strs)
            selected_uris[idx] = sel
            suppressed_uris[idx] = sup

        return ReconciliationResult(
            target_record=target_record,
            matches=matches,
            accepted_candidates=accepted_candidates,
            discovered_agent_uris=discovered_uris,
            selected_agent_uris=selected_uris,
            suppressed_agent_uris=suppressed_uris,
        )
