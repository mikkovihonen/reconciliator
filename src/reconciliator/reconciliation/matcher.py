"""Offline multi-tier WEMI reconciliation: exact identifiers, domain keys, then fallback.

Tier 1 resolves a standard identifier. Tier 2 matches on an author key plus title
stem, filtered by the work type its funnel declares. Tier 3 falls back to dense
vector search, then to PostgreSQL full-text search.
"""

from __future__ import annotations

from typing import Any, Optional

from reconciliator.config import ReconcileConfig
from reconciliator.database import WEMIRepository
from reconciliator.embedding.protocol import VectorEmbedder
from reconciliator.funnels import (
    BUILTIN_SPECS,
    FunnelContext,
    FunnelSpec,
    resolve_specs,
    run_funnel,
)
from reconciliator.indexer.base import VectorIndex
from reconciliator.models import (
    AgentEntity,
    AgentURI,
    BibliographicRecord,
    ContributionEntity,
    ExpressionEntity,
    IdentifierEntity,
    ManifestationEntity,
    MatchRecord,
    ReconciliationMatch,
    WorkEntity,
)
from reconciliator.reconciliation.agents import AgentAligner, AgentFallbackResolver
from reconciliator.reconciliation.results import (
    ConstituentWorkMatch,
    ReconciliationResult,
    WEMIMatchResult,
)
from reconciliator.reconciliation.stages import (
    DEFAULT_MAX_EDITIONS_COMPARED,
    AgentResolutionStage,
    CandidateRankingStage,
    CandidateRetrievalStage,
    IdentifierMatchingStage,
)
from reconciliator.rerank import CandidateEvaluator
from reconciliator.utils import (
    extract_identifiers_from_target,
)
from reconciliator.vocab import (
    VocabularyManager,
)


def _merge_funnel_specs(overrides: Optional[list[FunnelSpec]]) -> list[FunnelSpec]:
    """Combine configured funnels with the built-in ones.

    Config overrides or appends rather than replacing: an entry whose `id` matches
    a built-in takes its place *and its position*, so precedence is not silently
    changed by an override; an entry with a new id is inserted just before the
    default funnel, where it can claim records the built-ins did not.
    """
    if not overrides:
        return list(BUILTIN_SPECS)

    by_id = {s.id: s for s in overrides}
    merged: list[FunnelSpec] = []
    default_spec: Optional[FunnelSpec] = None
    for spec in BUILTIN_SPECS:
        chosen = by_id.pop(spec.id, spec)
        if chosen.default:
            default_spec = chosen
        else:
            merged.append(chosen)

    # Remaining entries are new funnels. Each goes immediately before the funnel
    # its `before` names, so a configured funnel can take precedence over a
    # built-in; without `before` it lands just ahead of the fall-through.
    for spec in by_id.values():
        if spec.before:
            positions = [i for i, s in enumerate(merged) if s.id == spec.before]
            if not positions:
                raise ValueError(
                    f"funnel {spec.id!r}: before={spec.before!r} names no known funnel"
                )
            merged.insert(positions[0], spec)
        else:
            merged.append(spec)

    if default_spec is not None:
        merged.append(default_spec)
    return merged


class WorkMatcher:
    """Multi-tier WEMI resolution and authority linking.

    Domain behaviour lives in the funnel specs rather than here: see
    `reconciliator.funnels`, which declares 42 built-in funnels and the predicates
    that route a record to one. This class owns the three tiers they share, the
    agent resolution that follows a match, and canonicalisation of the result.
    """

    def __init__(
        self,
        wemi_repo: WEMIRepository,
        embedder: Optional[VectorEmbedder] = None,
        config: Optional[ReconcileConfig] = None,
        authoritative_only: bool = True,
        agent_indexer: Optional[VectorIndex] = None,
    ) -> None:
        self.repo = wemi_repo
        self.embedder = embedder
        self.config = config
        self.authoritative_only = authoritative_only
        self.agent_indexer = agent_indexer
        self._equivalence_cache: Optional[dict[int, int]] = None

        aligner_thresh = (
            getattr(self.config, "agent_aligner_threshold", 85.0) if self.config else 85.0
        )
        self.agent_aligner = AgentAligner(similarity_threshold=aligner_thresh)

        # Funnels are resolved once, here, so an unknown handler name or a bad
        # handler path fails at construction naming the funnel, rather than
        # degrading silently to the generic template mid-corpus.
        self._funnels = resolve_specs(
            _merge_funnel_specs(getattr(config, "funnels", None)),
            getattr(config, "config_dir", None),
        )
        self.evaluator = (
            CandidateEvaluator(self.config.rerank)
            if (self.config and hasattr(self.config, "rerank"))
            else None
        )

        self.vocab_manager: Optional[VocabularyManager] = None
        self.agent_fallback_resolver: Optional[AgentFallbackResolver] = None
        if config is not None:
            self.vocab_manager = VocabularyManager(
                allowed_vocabularies=config.vocabularies,
                agent_priority=config.agent_priority,
                subject_priority=config.subject_priority,
                link_policy=config.link_policy,
                vocabulary_patterns=config.vocabulary_patterns,
                entity_vocabularies=config.entity_vocabularies,
                supported_subject_vocabularies=config.supported_subject_vocabularies,
            )
            self.agent_fallback_resolver = AgentFallbackResolver(
                similarity_threshold=config.agent_fallback_threshold,
                verify_chronology=config.verify_chronology,
                min_author_age=config.chronology_min_author_age,
                max_posthumous_years=config.chronology_max_posthumous_years,
                strict_homonym_guard=config.strict_homonym_guard,
                life_date_tolerance_years=getattr(config, "life_date_tolerance_years", 1),
                homonym_score_margin=getattr(config, "homonym_score_margin", 0.03),
                surname_match_threshold=getattr(config, "surname_similarity_threshold", 85.0),
                qualifier_similarity_threshold=getattr(
                    config, "qualifier_similarity_threshold", 80.0
                ),
                distribution_match_threshold=getattr(config, "distribution_match_threshold", 80.0),
            )

        # Specialized matching and resolution stages
        self.identifier_stage = IdentifierMatchingStage(
            repo=self.repo,
            agent_aligner=self.agent_aligner,
            config=self.config,
            authoritative_only=self.authoritative_only,
        )
        self.ranking_stage = CandidateRankingStage(
            repo=self.repo,
            evaluator=self.evaluator,
            config=self.config,
        )
        self.retrieval_stage = CandidateRetrievalStage(
            repo=self.repo,
            embedder=self.embedder,
            config=self.config,
            authoritative_only=self.authoritative_only,
            rank_candidates_fn=self.ranking_stage.rank_candidates,
            align_expression_fn=self.identifier_stage.align_expression,
        )
        self.agent_stage = AgentResolutionStage(
            repo=self.repo,
            agent_aligner=self.agent_aligner,
            agent_fallback_resolver=self.agent_fallback_resolver,
            vocab_manager=self.vocab_manager,
            agent_indexer=self.agent_indexer,
            embedder=self.embedder,
            config=self.config,
            hydrate_candidate_record_fn=self.ranking_stage.hydrate_candidate_record,
            retrieval_top_k_fn=self.retrieval_stage.retrieval_top_k,
        )
        self._work_manifestations_cache = self.ranking_stage._work_manifestations_cache
        self._hydration_cache = self.ranking_stage._hydration_cache

    # -----------------------------------------------------------------------
    # Stage Delegations
    # -----------------------------------------------------------------------

    def _retrieval_top_k(self, default: int = 50) -> int:
        """How many candidates the dense tiers retrieve, from `rerank.top_k`."""
        return self.retrieval_stage.retrieval_top_k(default)

    def _align_expression(
        self,
        exps: list[ExpressionEntity],
        target_language: Optional[str] = None,
        content_type: Optional[str] = None,
        translator_signature: Optional[str] = None,
    ) -> Optional[ExpressionEntity]:
        """Align expressions by language, optional content_type, and translator matching via AgentAligner."""
        return self.identifier_stage.align_expression(
            exps,
            target_language=target_language,
            content_type=content_type,
            translator_signature=translator_signature,
        )

    def _try_fast_path_identifiers(
        self,
        target: MatchRecord,
        ids: dict[str, list[str]],
        funnel_name: str,
        preferred_schemes: Optional[list[str]] = None,
    ) -> Optional[WEMIMatchResult]:
        return self.identifier_stage.try_identifiers(
            target, ids, funnel_name=funnel_name, preferred_schemes=preferred_schemes
        )

    def _vector_or_fts_fallback(
        self,
        target: MatchRecord,
        work_type: Optional[str] = None,
        funnel_name: str = "fallback",
    ) -> Optional[WEMIMatchResult]:
        """Perform semantic vector search fallback or FTS lexical search."""
        return self.retrieval_stage.vector_or_fts_fallback(
            target, work_type=work_type, funnel_name=funnel_name
        )

    def _target_view(self, record: MatchRecord) -> BibliographicRecord:
        """A BibliographicRecord view of a MatchRecord, for candidate evaluation."""
        return self.ranking_stage.target_view(record)

    def _hard_checks_pass(self, verdict: ReconciliationMatch) -> bool:
        """Whether a candidate survives the vetoes, ignoring the fuzzy threshold."""
        return self.ranking_stage.hard_checks_pass(verdict)

    def _rank_candidates(
        self,
        record: MatchRecord,
        candidates: list[tuple[WorkEntity, float]],
        acceptance_threshold: Optional[float] = None,
        require_threshold: bool = True,
        target_encoding: Optional[Any] = None,
    ) -> list[tuple[WorkEntity, ReconciliationMatch]]:
        """Score candidates against the record and return them best-first."""
        return self.ranking_stage.rank_candidates(
            record,
            candidates,
            acceptance_threshold=acceptance_threshold,
            require_threshold=require_threshold,
            target_encoding=target_encoding,
        )

    def _hydrate_candidate_record(
        self,
        work: Optional[WorkEntity],
        expression: Optional[ExpressionEntity] = None,
        manifestation: Optional[ManifestationEntity] = None,
        *,
        contributions_map: Optional[dict[tuple[str, int], list[ContributionEntity]]] = None,
        agents_map: Optional[dict[int, AgentEntity]] = None,
        agent_rels_map: Optional[dict[int, list[dict[str, Any]]]] = None,
        manifestation_identifiers_map: Optional[dict[int, list[IdentifierEntity]]] = None,
    ) -> Optional[BibliographicRecord]:
        """Reconstruct a BibliographicRecord from matched WEMI entities for agent alignment."""
        return self.ranking_stage.hydrate_candidate_record(
            work,
            expression=expression,
            manifestation=manifestation,
            contributions_map=contributions_map,
            agents_map=agents_map,
            agent_rels_map=agent_rels_map,
            manifestation_identifiers_map=manifestation_identifiers_map,
        )

    def _agent_uris_from_identifiers(self, agent: AgentEntity) -> tuple[list[str], list[str]]:
        """Collect an authority agent's $0 and $1 URIs from the identifiers table and its own columns."""
        return self.agent_stage.agent_uris_from_identifiers(agent)

    def _resolve_agent_fallback(
        self,
        target_record: BibliographicRecord,
        discovered_uris: dict[int, list[AgentURI]],
        matches: list[ReconciliationMatch],
    ) -> None:
        """Tier 3: resolve target agents left unlinked by coupled work matching."""
        self.agent_stage.resolve_agent_fallback(target_record, discovered_uris, matches)

    def resolve_agents(
        self,
        result: WEMIMatchResult,
        target_record: BibliographicRecord,
    ) -> ReconciliationResult:
        """Resolve authority URIs for a record's agents, given its work match."""
        return self.agent_stage.resolve_agents(result, target_record)

    # -----------------------------------------------------------------------
    # Master Dispatcher
    # -----------------------------------------------------------------------

    def match_target(
        self,
        target: MatchRecord,
        identifiers: Optional[dict[str, list[str]]] = None,
    ) -> WEMIMatchResult:
        """Resolve a staged MatchRecord and map the result to its canonical work."""
        batch_ids = [identifiers] if identifiers else None
        return self.match_targets_batch([target], batch_identifiers=batch_ids)[0]

    def match_targets_batch(
        self,
        targets: list[MatchRecord],
        batch_identifiers: Optional[list[Optional[dict[str, list[str]]]]] = None,
    ) -> list[WEMIMatchResult]:
        """Resolve a batch of MatchRecord instances through the 3 tiers with bulk vectorization.

        1. Filter by config accepts().
        2. Pre-warm identifier cache via bulk lookup_by_identifiers_batch.
        3. Dispatch targets to matching funnels with pre-warmed cache hits.
        4. Canonicalize all results.
        """
        if not targets:
            return []

        results: list[Optional[WEMIMatchResult]] = [None] * len(targets)
        batch_extracted: list[dict[str, list[str]]] = []

        # 1. Config acceptance check & identifier extraction
        for i, target in enumerate(targets):
            if self.config is not None and not self.config.accepts(
                work_type=target.work_type,
                language=target.language,
                year=target.year,
            ):
                results[i] = WEMIMatchResult(
                    target_record_id=target.record_id,
                    match_tier="skipped_filtered_work_type",
                    confidence_score=0.0,
                )
                batch_extracted.append({})
                continue

            extracted = extract_identifiers_from_target(target)
            extra_ids = (
                batch_identifiers[i] if batch_identifiers and i < len(batch_identifiers) else None
            )
            if extra_ids:
                for scheme, values in extra_ids.items():
                    bucket = extracted.setdefault(scheme.lower().strip(), [])
                    for val in values:
                        if val not in bucket:
                            bucket.append(val)
            batch_extracted.append(extracted)

        # 2. Bulk identifier lookup to pre-warm cache in a single SQL query
        id_pairs_to_lookup: list[tuple[str, str]] = []
        for i, target in enumerate(targets):
            if results[i] is not None:
                continue
            for scheme, vals in batch_extracted[i].items():
                for v in vals:
                    id_pairs_to_lookup.append((scheme, v))

        if id_pairs_to_lookup and hasattr(self.repo, "lookup_by_identifiers_batch"):
            self.repo.lookup_by_identifiers_batch(
                id_pairs_to_lookup, authoritative_only=self.authoritative_only
            )

        # 3. Route targets through their respective funnels
        for i, target in enumerate(targets):
            if results[i] is not None:
                continue
            extracted = batch_extracted[i]
            spec, handler = self._select_funnel(target, extracted)
            ctx = FunnelContext(
                spec=spec,
                repo=self.repo,
                config=self.config,
                identifiers=extracted,
                align_expression=self._align_expression,
                try_identifiers=self._try_fast_path_identifiers,
                vector_or_fts=self._vector_or_fts_fallback,
                rank_candidates=self._rank_candidates,
                result_cls=WEMIMatchResult,
                constituent_cls=ConstituentWorkMatch,
            )
            results[i] = run_funnel(spec, target, ctx, handler)

        # 4. Canonicalize all results
        canonicalized: list[WEMIMatchResult] = []
        for i, r in enumerate(results):
            if r is None:
                r = WEMIMatchResult(
                    target_record_id=targets[i].record_id,
                    match_tier="unmatched",
                    confidence_score=0.0,
                )
            canonicalized.append(self._canonicalize_match(r))

        return canonicalized

    def _select_funnel(
        self, target: MatchRecord, identifiers: dict[str, list[str]]
    ) -> tuple[FunnelSpec, Any]:
        """First claiming spec, else the default one."""
        fallback: Optional[tuple[FunnelSpec, Any]] = None
        for spec, handler in self._funnels:
            if spec.default:
                fallback = (spec, handler)
                continue
            if spec.claims(target, identifiers):
                return spec, handler
        if fallback is None:
            raise RuntimeError("no funnel claimed the record and no default is configured")
        return fallback

    def _canonicalize_match(self, result: WEMIMatchResult) -> WEMIMatchResult:
        """Re-point a matched work at the canonical work of its equivalence group."""
        if result.matched_work is None or result.matched_work.id is None:
            return result

        equivalences = self._work_equivalences()
        canonical_id = equivalences.get(result.matched_work.id)
        if canonical_id is None or canonical_id == result.matched_work.id:
            return result

        canonical = self.repo.get_work(canonical_id)
        if canonical is None:
            return result

        result.matched_work = canonical
        result.work_uri = canonical.work_uri
        return result

    def _work_equivalences(self) -> dict[int, int]:
        """Cached work-id -> canonical-work-id map; empty when nothing is consolidated."""
        cached = getattr(self, "_equivalence_cache", None)
        if cached is None:
            try:
                cached = self.repo.get_work_equivalence_map()
            except Exception:
                cached = {}
            self._equivalence_cache = cached
        return cached


__all__ = [
    "WorkMatcher",
    "_merge_funnel_specs",
    "DEFAULT_MAX_EDITIONS_COMPARED",
]
