"""Offline multi-tier WEMI reconciliation: exact identifiers, domain keys, then fallback.

Tier 1 resolves a standard identifier. Tier 2 matches on an author key plus title
stem, filtered by the work type its funnel declares. Tier 3 falls back to dense
vector search, then to SQLite FTS5.

The engine names a specific backend nowhere: the dense tier searches whatever
vector index the repository was constructed with, and the CLI currently
constructs none, so tier 3 reaches only FTS in practice. Building and wiring the
work and manifestation indexes is Phase 14.10; until then the dense branch is
unreachable outside tests, and this docstring says so rather than advertising it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Optional

from reconciliator.agent_transfer import AgentAligner, AgentFallbackResolver
from reconciliator.config import ReconcileConfig, ReconcileFTSFallbackConfig
from reconciliator.embedder import SentenceEmbedder
from reconciliator.funnels import (
    BUILTIN_SPECS,
    FunnelContext,
    FunnelSpec,
    resolve_specs,
    run_funnel,
)
from reconciliator.indexer.base import VectorIndex
from reconciliator.models import (
    Agent,
    AgentEntity,
    AgentURI,
    BibliographicRecord,
    CorpusLinkage,
    ExpressionEntity,
    ManifestationEntity,
    MatchRecord,
    ReconciliationMatch,
    WorkEntity,
    WorkRelationship,
)
from reconciliator.normalizers.blocking import compute_author_key
from reconciliator.database import WEMIRepository
from reconciliator.rerank import CandidateEvaluator
from reconciliator.utils import (
    extract_identifiers_from_target,
)
from reconciliator.vocab import (
    VocabularyManager,
    format_wikidata_uri,
    normalize_subject_vocabulary,
)


@dataclass
class ReconciliationResult:
    """Consolidated reconciliation result for a single target record."""

    target_record: BibliographicRecord
    matches: list[ReconciliationMatch] = field(default_factory=list)
    accepted_candidates: list[BibliographicRecord] = field(default_factory=list)
    discovered_agent_uris: dict[int, list[AgentURI]] = field(default_factory=dict)
    selected_agent_uris: dict[int, list[str]] = field(default_factory=dict)
    suppressed_agent_uris: dict[int, list[str]] = field(default_factory=dict)
    corpus_linkage: Optional[CorpusLinkage] = None


@dataclass
class ConstituentWorkMatch:
    """Represents a matched constituent work within an aggregate or collection."""

    title: str
    matched_work: Optional[WorkEntity] = None
    work_uri: Optional[str] = None
    expression_uri: Optional[str] = None
    relationship_type: Optional[str] = None
    confidence_score: float = 1.0


@dataclass
class WEMIMatchResult:
    """Consolidated match result for a target record resolved by WorkMatcher."""

    target_record_id: str
    matched_work: Optional[WorkEntity] = None
    matched_expression: Optional[ExpressionEntity] = None
    matched_manifestation: Optional[ManifestationEntity] = None
    work_curie: Optional[str] = None
    expression_curie: Optional[str] = None
    manifestation_curie: Optional[str] = None
    work_uri: Optional[str] = None
    expression_uri: Optional[str] = None
    manifestation_uri: Optional[str] = None
    match_tier: str = "unmatched"
    funnel_used: str = "unknown"
    confidence_score: float = 0.0
    constituent_matches: list[ConstituentWorkMatch] = field(default_factory=list)
    relationships: list[WorkRelationship] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.work_curie and self.work_uri:
            self.work_curie = self.work_uri
        elif not self.work_uri and self.work_curie:
            self.work_uri = self.work_curie

        if not self.expression_curie and self.expression_uri:
            self.expression_curie = self.expression_uri
        elif not self.expression_uri and self.expression_curie:
            self.expression_uri = self.expression_curie

        if not self.manifestation_curie and self.manifestation_uri:
            self.manifestation_curie = self.manifestation_uri
        elif not self.manifestation_uri and self.manifestation_curie:
            self.manifestation_uri = self.manifestation_curie


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


# How many of a work's manifestations are compared before giving up. A work with
# hundreds of editions would otherwise turn one candidate into hundreds of
# evaluations; the discriminators only need to find one compatible edition.
MAX_EDITIONS_COMPARED = 25


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
        embedder: Optional[SentenceEmbedder] = None,
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
        self.agent_aligner = AgentAligner()
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
        # Vocabulary filtering and the standalone agent fallback are only meaningful
        # once a ReconcileConfig is present; match_target_record() guards on these.
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
            )

    # -----------------------------------------------------------------------
    # Helper Methods
    # -----------------------------------------------------------------------

    def _retrieval_top_k(self, default: int = 50) -> int:
        """How many candidates the dense tiers retrieve, from `rerank.top_k`."""
        rerank = getattr(self.config, "rerank", None)
        value = getattr(rerank, "top_k", None)
        return int(value) if value else default

    def _align_expression(
        self,
        exps: list[ExpressionEntity],
        target_language: Optional[str] = None,
        content_type: Optional[str] = None,
        translator_signature: Optional[str] = None,
    ) -> Optional[ExpressionEntity]:
        """Align expressions by language, optional content_type, and translator matching via AgentAligner."""
        if not exps:
            return None
        filtered = exps
        if content_type:
            ct_filtered = [e for e in filtered if e.content_type == content_type]
            if ct_filtered:
                filtered = ct_filtered
        if target_language:
            t_lang = target_language.strip().lower()
            lang_filtered = [
                e for e in filtered if e.language and e.language.strip().lower() == t_lang
            ]
            if lang_filtered:
                filtered = lang_filtered

        # Translator alignment via AgentAligner if translator_signature is present
        if translator_signature and len(filtered) > 1:
            norm_sig = translator_signature.strip().lower()
            best_match: Optional[ExpressionEntity] = None
            best_score = 0.0
            for e in filtered:
                candidate_names: list[str] = []
                if e.title:
                    candidate_names.append(e.title)
                if e.id and hasattr(self.repo, "get_contributions_for_entity"):
                    contribs = self.repo.get_contributions_for_entity("expression", e.id)
                    for c in contribs:
                        if hasattr(self.repo, "get_agent"):
                            agent = self.repo.get_agent(c.agent_id)
                            if agent:
                                if agent.name:
                                    candidate_names.append(agent.name)
                                if agent.surname:
                                    candidate_names.append(agent.surname)
                for c_name in candidate_names:
                    score = self.agent_aligner.align_names(norm_sig, c_name)
                    if score > best_score:
                        best_score = score
                        best_match = e
            if best_match and best_score >= 0.70:
                return best_match

        return filtered[0]

    def _try_fast_path_identifiers(
        self,
        target: MatchRecord,
        ids: dict[str, list[str]],
        funnel_name: str,
        preferred_schemes: Optional[list[str]] = None,
    ) -> Optional[WEMIMatchResult]:
        """Attempt Tier 1 standard identifier resolution."""
        schemes_order: list[str] = []
        if preferred_schemes:
            schemes_order.extend([s.lower() for s in preferred_schemes])
        for s in ids.keys():
            s_low = s.lower()
            if s_low not in schemes_order:
                schemes_order.append(s_low)

        for scheme in schemes_order:
            values = ids.get(scheme, [])
            for val in values:
                matches = self.repo.lookup_by_identifier(
                    scheme, val, authoritative_only=self.authoritative_only
                )
                if not matches:
                    continue

                entity_type, entity_id = matches[0]
                if entity_type == "manifestation":
                    manif = self.repo.get_manifestation(entity_id)
                    if manif:
                        work = self.repo.get_work(manif.work_id) if manif.work_id else None
                        exps = self.repo.get_expressions_for_manifestation(manif.id)
                        if not exps and work:
                            exps = self.repo.get_expressions_for_work(work.id)
                        exp = self._align_expression(
                            exps,
                            target.language,
                            translator_signature=target.get_first_key("translators_signature"),
                        )
                        if not work and exp and exp.work_id:
                            work = self.repo.get_work(exp.work_id)
                        return WEMIMatchResult(
                            target_record_id=target.record_id,
                            matched_work=work,
                            matched_expression=exp,
                            matched_manifestation=manif,
                            work_uri=work.work_uri if work else None,
                            expression_uri=exp.expression_uri if exp else None,
                            manifestation_uri=manif.manifestation_uri,
                            match_tier="tier1_exact_identifier",
                            funnel_used=funnel_name,
                            confidence_score=1.0,
                        )

                elif entity_type == "expression":
                    exp = self.repo.get_expression(entity_id)
                    if exp:
                        work = self.repo.get_work(exp.work_id)
                        manifs = self.repo.get_manifestations_for_expression(exp.id)
                        manif = manifs[0] if manifs else None
                        return WEMIMatchResult(
                            target_record_id=target.record_id,
                            matched_work=work,
                            matched_expression=exp,
                            matched_manifestation=manif,
                            work_uri=work.work_uri if work else None,
                            expression_uri=exp.expression_uri,
                            manifestation_uri=manif.manifestation_uri if manif else None,
                            match_tier="tier1_exact_identifier",
                            funnel_used=funnel_name,
                            confidence_score=1.0,
                        )

                elif entity_type == "work":
                    work = self.repo.get_work(entity_id)
                    if work:
                        exps = self.repo.get_expressions_for_work(work.id)
                        exp = self._align_expression(
                            exps,
                            target.language,
                            translator_signature=target.get_first_key("translators_signature"),
                        )
                        return WEMIMatchResult(
                            target_record_id=target.record_id,
                            matched_work=work,
                            matched_expression=exp,
                            work_uri=work.work_uri,
                            expression_uri=exp.expression_uri if exp else None,
                            match_tier="tier1_exact_identifier",
                            funnel_used=funnel_name,
                            confidence_score=1.0,
                        )

        return None

    def _vector_or_fts_fallback(
        self,
        target: MatchRecord,
        work_type: Optional[str] = None,
        funnel_name: str = "fallback",
    ) -> Optional[WEMIMatchResult]:
        """Perform semantic vector search fallback or FTS5 lexical search."""
        # 1. Dense vector search
        if (
            self.embedder is not None
            and self.repo.work_indexer is not None
            and self.repo.work_indexer.total_vectors > 0
        ):
            query_text = target.serialized_work or f"{target.title} {target.author_key or ''}"
            query_vec = self.embedder.embed_texts([query_text])
            # Retrieval depth comes from rerank.top_k. It was hardcoded to 5
            # while the config declared 50, so configuring it did nothing - and
            # depth is exactly what the authoritative_only over-fetch has to
            # reason about, since a filter applied after retrieval can only
            # return fewer candidates than were fetched.
            results = self.repo.search_work_vectors(
                query_vec,
                top_k=self._retrieval_top_k(),
                authoritative_only=self.authoritative_only,
            )
            thresh = (
                self.config.threshold
                if hasattr(self.config, "threshold") and self.config.threshold is not None
                else 0.70
            )
            # Rank the retrieved candidates instead of accepting the first one
            # over a bare dense threshold. This is what `rerank.top_k` exists to
            # feed: retrieving 50 candidates is pointless if the first
            # acceptable one wins, which is why nobody noticed the depth was
            # hardcoded to 5.
            typed = [
                (cand_work, score)
                for cand_work, score in results
                if not work_type or cand_work.work_type == work_type
            ]
            for cand_work, verdict in self._rank_candidates(
                target, typed, acceptance_threshold=thresh
            ):
                if not verdict.accepted:
                    continue
                exps = self.repo.get_expressions_for_work(cand_work.id)
                exp = self._align_expression(
                    exps,
                    target.language,
                    translator_signature=target.get_first_key("translators_signature"),
                )
                return WEMIMatchResult(
                    target_record_id=target.record_id,
                    matched_work=cand_work,
                    matched_expression=exp,
                    work_uri=cand_work.work_uri,
                    expression_uri=exp.expression_uri if exp else None,
                    match_tier="tier2_vector_fallback",
                    funnel_used=funnel_name,
                    confidence_score=float(verdict.match_score),
                )

        # 2. FTS5 Lexical Search (Tier 3 Fallback)
        # Architectural distinction:
        # This fallback performs database-level candidate retrieval against SQLite FTS5 virtual tables
        # using SQLite's native C-level BM25 ranking function. It is completely independent of the
        # in-memory sparse reranking (rank-bm25 / CandidateEvaluator) performed during scoring.
        fts_cfg = (
            self.config.fts_fallback
            if (self.config and hasattr(self.config, "fts_fallback"))
            else ReconcileFTSFallbackConfig()
        )
        if fts_cfg.enabled:
            fts_query = target.title_stem or target.title
            if fts_query:
                try:
                    fts_results = self.repo.search_works_fts(
                        fts_query, limit=fts_cfg.limit, authoritative_only=self.authoritative_only
                    )
                    for cand_work, _rank in fts_results:
                        if work_type and cand_work.work_type != work_type:
                            continue
                        exps = self.repo.get_expressions_for_work(cand_work.id)
                        exp = self._align_expression(
                            exps,
                            target.language,
                            translator_signature=target.get_first_key("translators_signature"),
                        )
                        return WEMIMatchResult(
                            target_record_id=target.record_id,
                            matched_work=cand_work,
                            matched_expression=exp,
                            work_uri=cand_work.work_uri,
                            expression_uri=exp.expression_uri if exp else None,
                            match_tier="tier3_fts_fallback",
                            funnel_used=funnel_name,
                            confidence_score=fts_cfg.confidence_score,
                        )
                except Exception:
                    pass

        return None

    # -----------------------------------------------------------------------
    # Domain Funnels (A through PP)
    # -----------------------------------------------------------------------

    # -----------------------------------------------------------------------
    # Master Dispatcher
    # -----------------------------------------------------------------------

    def match_target(
        self,
        target: MatchRecord,
        identifiers: Optional[dict[str, list[str]]] = None,
    ) -> WEMIMatchResult:
        """Resolve a staged MatchRecord and map the result to its canonical work.

        Canonicalisation happens here rather than at each call site so that every
        consumer - the corpus clusterer's authority hoisting and the reconcile
        loop alike - resolves a work the same way. Without it, records matching
        different sources that hold the same work would disagree about its URI.
        """
        result = self._dispatch_match(target, identifiers)
        return self._canonicalize_match(result)

    def _dispatch_match(
        self,
        target: MatchRecord,
        identifiers: Optional[dict[str, list[str]]] = None,
    ) -> WEMIMatchResult:
        """Route a record to the first funnel whose predicate claims it.

        Replaces a 44-condition if-chain in which precedence was implied by
        statement order. Specs are ordered, first match wins, and the one marked
        `default` claims whatever nothing else did.
        """
        extracted = extract_identifiers_from_target(target)
        if identifiers:
            for scheme, values in identifiers.items():
                bucket = extracted.setdefault(scheme.lower().strip(), [])
                for value in values:
                    if value not in bucket:
                        bucket.append(value)

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
        return run_funnel(spec, target, ctx, handler)

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
        """Re-point a matched work at the canonical work of its equivalence group.

        Each source mints its own work URI, so the same work exists once per
        source. Consolidation elects one representative; this maps a match onto
        it, leaving the match tier and score untouched. The matched expression is
        deliberately not re-pointed: an expression of a consolidated work is a
        genuine expression of the canonical work.
        """
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

    def _agent_uris_from_identifiers(self, agent: AgentEntity) -> tuple[list[str], list[str]]:
        """Collect an authority agent's $0 and $1 URIs from the identifiers table and its own columns."""
        u0: list[str] = []
        u1: list[str] = []

        def _add(uri: Optional[str], subfield: Optional[str] = None) -> None:
            if not uri:
                return
            uri = uri.strip()
            # Synthetic placeholders minted at ingest are not linkable authority URIs.
            if not uri or uri.startswith(("urn:agent:", "urn:reconciliator:")):
                return
            is_entity = "wikidata.org" in uri.lower() or (subfield == "1")
            bucket = u1 if is_entity else u0
            if uri not in u0 and uri not in u1:
                bucket.append(uri)

        idents = agent.identifiers
        if not idents and agent.id:
            idents = self.repo.get_identifiers_for_entity("agent", agent.id)
        for ident in idents:
            src = (ident.source or "").lower()
            if src == "wikidata":
                _add(format_wikidata_uri(ident.source_id), "1")
            else:
                _add(ident.curie or f"{src}:{ident.source_id}", "0")

        _add(agent.curie)
        if agent.source.lower() == "wikidata" and agent.source_id:
            _add(format_wikidata_uri(agent.source_id), "1")

        return u0, u1

    def _target_view(self, record: MatchRecord) -> BibliographicRecord:
        """A BibliographicRecord view of a MatchRecord, for candidate evaluation.

        `CandidateEvaluator` compares two BibliographicRecords. The funnels carry
        a MatchRecord, whose author identity is the normalised `author_key`
        rather than a list of agents - so the key is presented as a single
        pseudo-agent surname, and candidate surnames are normalised the same way
        in `_rank_candidates` before the comparison. Without that both sides
        would be compared with different normalisation and "O'Brien" would never
        match "obrien".
        """
        return BibliographicRecord(
            source_id=record.record_id,
            source=record.format or "target",
            title=record.title or "",
            uniform_title=record.uniform_title,
            publication_year=record.year,
            volume_designation=record.volume_designation,
            language=record.language,
            agents=[Agent(name=record.author_key or "", surname=record.author_key)]
            if record.author_key
            else [],
        )

    def _hard_checks_pass(self, verdict: ReconciliationMatch) -> bool:
        """Whether a candidate survives the vetoes, ignoring the fuzzy threshold.

        The evaluator's `accepted` flag answers two questions at once: did the
        candidate survive the volume, surname and year discriminators, and did
        its fused score clear the acceptance threshold. Those belong to
        different tiers. A tier-2 candidate already matched the author key and
        the title stem *exactly*, so demanding its BM25 title similarity also
        clear a fuzzy bar rejects correct matches - with the default weights and
        a 0.85 threshold it demands a rerank score of 0.775 from an exact-key
        match. The score orders tier-2 candidates; only the vetoes reject them.
        """
        rerank_cfg = getattr(self.config, "rerank", None)
        max_year = getattr(rerank_cfg, "max_year_difference", None)
        year_ok = (
            verdict.year_diff is None or max_year is None or abs(verdict.year_diff) <= max_year
        )
        return bool(verdict.volume_match and verdict.surname_match and year_ok)

    def _rank_candidates(
        self,
        record: MatchRecord,
        candidates: list[tuple[WorkEntity, float]],
        acceptance_threshold: Optional[float] = None,
        require_threshold: bool = True,
    ) -> list[tuple[WorkEntity, ReconciliationMatch]]:
        """Score candidates against the record and return them best-first.

        This is the reranking stage the configuration has always described and
        nothing performed. Previously a tier took `candidates[0]`, or the first
        candidate above a bare dense threshold, so the matcher could not prefer a
        better candidate over a worse one and the volume, surname and year
        discriminators never ran.

        Returns every candidate with its verdict, ordered by fused score, so a
        caller can take the best *accepted* one and report what it rejected.
        With no evaluator configured the ordering is the input order and every
        candidate is accepted, which is the previous behaviour.
        """
        threshold = (
            acceptance_threshold
            if acceptance_threshold is not None
            else getattr(self.config, "threshold", None) or 0.85
        )
        if self.evaluator is None:
            return [
                (
                    work,
                    ReconciliationMatch(
                        candidate_record_id=work.source_id,
                        source=work.source,
                        match_tier="tier2_domain_key",
                        match_score=score,
                        dense_score=score,
                        accepted=True,
                    ),
                )
                for work, score in candidates
            ]

        target = self._target_view(record)
        scored: list[tuple[WorkEntity, ReconciliationMatch]] = []
        for work, dense_score in candidates:
            # The volume, year and publisher the discriminators compare live on
            # the *manifestation*, not the work, so a work has to be hydrated
            # with one to be judged on them. A work with several manifestations
            # is several editions: the record is compatible with the work if it
            # is compatible with any one of them, so each is evaluated and the
            # best verdict stands. Hydrating with the work alone left volume and
            # year as None on the candidate side, which both checks read as
            # "do not penalise" - so they silently never fired.
            manifestations = self.repo.get_manifestations_for_work(work.id) if work.id else []
            best: Optional[ReconciliationMatch] = None
            for manifestation in (manifestations or [None])[:MAX_EDITIONS_COMPARED]:
                candidate = self._hydrate_candidate_record(work, None, manifestation)
                if candidate is None:
                    continue
                # Normalise candidate surnames the way author_key is normalised,
                # so the evaluator's surname check compares like with like.
                candidate = candidate.model_copy(
                    update={
                        "agents": [
                            agent.model_copy(
                                update={"surname": compute_author_key(agent.name, agent.surname)}
                            )
                            for agent in candidate.agents
                        ]
                    }
                )
                verdict = self.evaluator.evaluate_candidate(
                    target, candidate, dense_score, acceptance_threshold=threshold
                )
                if not require_threshold and not verdict.accepted:
                    # Re-admit a candidate the fuzzy threshold rejected but the
                    # vetoes did not. See _hard_checks_pass.
                    verdict = verdict.model_copy(
                        update={"accepted": self._hard_checks_pass(verdict)}
                    )
                if best is None or (verdict.accepted, verdict.match_score) > (
                    best.accepted,
                    best.match_score,
                ):
                    best = verdict
            if best is not None:
                scored.append((work, best))

        scored.sort(key=lambda pair: (pair[1].accepted, pair[1].match_score), reverse=True)
        return scored

    def _hydrate_candidate_record(
        self,
        work: Optional[WorkEntity],
        expression: Optional[ExpressionEntity] = None,
        manifestation: Optional[ManifestationEntity] = None,
    ) -> Optional[BibliographicRecord]:
        """Reconstruct a BibliographicRecord from matched WEMI entities for agent alignment.

        AgentAligner compares target agents against candidate records' agents, so a
        matched work has to be rehydrated (contributions -> agents -> identifiers)
        before its authority URIs can be transferred.
        """
        from reconciliator.models import RecordIdentifiers

        if work is None and manifestation is None:
            return None

        # Gather contributions across every matched tier, de-duplicating by agent.
        agent_ids: list[int] = []
        tiers: list[tuple[str, Optional[int]]] = [
            ("work", work.id if work else None),
            ("expression", expression.id if expression else None),
            ("manifestation", manifestation.id if manifestation else None),
        ]
        for entity_type, entity_id in tiers:
            if not entity_id:
                continue
            try:
                contribs = self.repo.get_contributions_for_entity(entity_type, entity_id)
            except Exception:
                continue
            for c in contribs:
                if c.agent_id and c.agent_id not in agent_ids:
                    agent_ids.append(c.agent_id)

        agents: list[Agent] = []
        for aid in agent_ids:
            entity = self.repo.get_agent(aid)
            if entity is None:
                continue
            u0, u1 = self._agent_uris_from_identifiers(entity)
            agents.append(
                Agent(
                    name=entity.name,
                    surname=entity.surname,
                    dates=entity.dates,
                    fuller_name=entity.fuller_name,
                    birth_year=entity.birth_year,
                    death_year=entity.death_year,
                    is_personal=entity.is_personal,
                    is_fictional=entity.is_fictional,
                    uri_subfield_0=u0,
                    uri_subfield_1=u1,
                    variant_names=list(entity.variant_names),
                    qualifiers=list(entity.qualifiers),
                )

            )


        title = (work.title if work else None) or (manifestation.title if manifestation else "")
        source = (work.source if work else None) or (manifestation.source if manifestation else "")
        source_id = (work.source_id if work else None) or (
            manifestation.source_id if manifestation else ""
        )

        identifiers = RecordIdentifiers()
        if manifestation and manifestation.id:
            try:
                for ident in self.repo.get_identifiers_for_entity(
                    "manifestation", manifestation.id
                ):
                    bucket = getattr(identifiers, (ident.scheme or "").lower(), None)
                    if isinstance(bucket, list) and ident.value not in bucket:
                        bucket.append(ident.value)
            except Exception:
                pass

        return BibliographicRecord(
            source_id=source_id,
            source=source,
            title=title,
            subtitle=manifestation.subtitle if manifestation else None,
            uniform_title=work.original_title if work else None,
            publication_year=manifestation.publication_year if manifestation else None,
            publisher=manifestation.publisher if manifestation else None,
            publication_place=manifestation.publication_place if manifestation else None,
            language=expression.language if expression else None,
            original_language=work.original_language if work else None,
            series_title=manifestation.series_title if manifestation else None,
            series_numbering=manifestation.series_numbering if manifestation else None,
            volume_designation=manifestation.volume_designation if manifestation else None,
            identifiers=identifiers,
            agents=agents,
        )

    def _resolve_agent_fallback(
        self,
        target_record: BibliographicRecord,
        discovered_uris: dict[int, list[AgentURI]],
        matches: list[ReconciliationMatch],
    ) -> None:
        """Tier 3: resolve target agents left unlinked by coupled work matching.

        Queries the standalone agent vector index, hydrates candidate profiles from
        SQLite, and lets AgentFallbackResolver arbitrate with chronology and homonym
        guards before any URI is accepted.
        """
        cfg = self.config
        if (
            cfg is None
            or not cfg.fallback_agent_reconciliation
            or self.agent_indexer is None
            or self.embedder is None
            or self.agent_fallback_resolver is None
        ):
            return

        from reconciliator.template import WEMISerializationEngine
        from reconciliator.vocab import detect_vocabulary

        wemi_engine = WEMISerializationEngine()
        for idx, target_agent in enumerate(target_record.agents):
            # Already linked via coupled matching, or a fictional character that
            # must never be bound to a real authority record.
            if discovered_uris.get(idx) or target_agent.is_fictional:
                continue

            agent_text = wemi_engine.render_agent(target_agent)
            if not agent_text or not agent_text.strip():
                continue

            q_vec = self.embedder.embed_texts([agent_text])
            cand_scores, cand_ids = self.agent_indexer.search(q_vec, top_k=self._retrieval_top_k())
            if len(cand_ids) == 0 or len(cand_ids[0]) == 0:
                continue

            agent_id_list = [int(i) for i in cand_ids[0]]
            agent_score_list = [float(s) for s in cand_scores[0]]

            cand_profiles = self.repo.get_agents_by_ids(agent_id_list)
            evaluated_cands: list[dict[str, Any]] = []
            for cid, cscore in zip(agent_id_list, agent_score_list):
                if cid in cand_profiles:
                    profile = dict(cand_profiles[cid])
                    profile["score"] = cscore
                    evaluated_cands.append(profile)
            if not evaluated_cands:
                continue

            target_coauthors = [a.name for a in target_record.agents if a is not target_agent]
            target_classifications = [(c.code, c.scheme) for c in target_record.classifications]
            target_subjects: dict[str, int] = {}
            if target_record.subjects:
                for cat in target_record.subjects.all_subjects():
                    if cat.vocabulary and cat.pref_label:
                        norm_v = normalize_subject_vocabulary(
                            cat.vocabulary,
                            uri=cat.uri,
                            supported_vocabularies=cfg.supported_subject_vocabularies,
                        )
                        if norm_v:
                            target_subjects[f"{norm_v}:{cat.pref_label.strip().lower()}"] = 1

            best_cand, _reason = self.agent_fallback_resolver.evaluate_candidates(
                target_agent=target_agent,
                target_pub_year=target_record.publication_year,
                candidates=evaluated_cands,
                authority_store=self.repo,
                target_coauthors=target_coauthors,
                target_publisher=target_record.publisher,
                target_classifications=target_classifications,
                target_subjects=target_subjects,
            )
            if not best_cand:
                continue

            best_score = float(best_cand.get("score", cfg.agent_fallback_threshold))
            existing_uris = {u.uri for u in discovered_uris.setdefault(idx, [])}
            cand_source = str(best_cand.get("source") or "agent_fallback")
            cand_row_id = str(best_cand.get("id"))

            def _accept(uri: Optional[str], subfield: str, default_vocab: str) -> None:
                if not uri or uri in existing_uris:
                    return
                vocab = detect_vocabulary(uri) or default_vocab
                discovered_uris[idx].append(
                    AgentURI(
                        uri=uri,
                        vocabulary=vocab,
                        subfield_code=subfield,
                        source_record_id=cand_row_id,
                        source=cand_source,
                        alignment_score=best_score * 100.0,
                    )
                )
                existing_uris.add(uri)

            if best_cand.get("id"):
                try:
                    agent_idents = self.repo.get_identifiers_for_entity(
                        "agent", int(best_cand["id"])
                    )
                    for ident in agent_idents:
                        is_wd = ident.scheme.lower() == "wikidata"
                        _accept(
                            ident.curie,
                            "1" if is_wd else "0",
                            "WIKIDATA" if is_wd else ident.scheme.upper(),
                        )
                except Exception:
                    pass

            c_src = best_cand.get("source")
            c_sid = best_cand.get("source_id")
            if c_src and c_sid and not str(c_src).startswith(("urn", "unknown")):
                c_src_l = str(c_src).lower()
                from reconciliator.curie import _get_sources_registry, format_source_uri

                formatted_uri = format_source_uri(c_src_l, str(c_sid))
                src_info = _get_sources_registry().get(c_src_l, {})
                subf = str(src_info.get("subfield", "1" if c_src_l == "wikidata" else "0"))
                _accept(formatted_uri, subf, str(c_src).upper())

            if best_cand.get("agent_uri"):
                a_uri = str(best_cand["agent_uri"])
                if not a_uri.startswith(("urn:agent:", "urn:reconciliator:")):
                    is_wd = "wikidata.org" in a_uri.lower()
                    _accept(a_uri, "1" if is_wd else "0", "WIKIDATA" if is_wd else "OTHER")

            for raw_key, subfield, default_vocab in (
                ("uri_subfield_0", "0", "OTHER"),
                ("uri_subfield_1", "1", "WIKIDATA"),
            ):
                raw = best_cand.get(raw_key)
                if not raw:
                    continue
                try:
                    items = json.loads(raw) if str(raw).startswith("[") else [raw]
                except Exception:
                    items = [raw]
                for item in items:
                    if item:
                        _accept(str(item), subfield, default_vocab)

            if best_cand.get("wikidata_qid"):
                from reconciliator.curie import format_source_uri

                _accept(
                    format_source_uri("wikidata", str(best_cand["wikidata_qid"])), "1", "WIKIDATA"
                )

            cand_rec_id = str(
                best_cand.get("curie")
                or (
                    f"{best_cand['source']}:{best_cand['source_id']}"
                    if best_cand.get("source") and best_cand.get("source_id")
                    else None
                )
                or best_cand.get("agent_uri")
                or best_cand.get("id")
            )
            matches.append(
                ReconciliationMatch(
                    candidate_record_id=cand_rec_id,
                    source=cand_source,
                    match_tier="tier3_agent_fallback",
                    match_score=best_score,
                    dense_score=best_score,
                    rerank_score=None,
                    volume_match=True,
                    surname_match=True,
                    year_diff=None,
                    accepted=True,
                )
            )

    def resolve_agents(
        self,
        result: WEMIMatchResult,
        target_record: BibliographicRecord,
    ) -> ReconciliationResult:
        """Resolve authority URIs for a record's agents, given its work match.

        Split out of the former `match_target_record`, which built its own
        degraded MatchRecord and matched a second time. That inner record
        hardcoded `work_type="text"`, carried no domain keys, and labelled a JSON
        payload as marcxml, so it could only ever reach the monograph funnel and
        could never resolve an identifier - yet its result was what agent
        alignment anchored on, while the correct match was computed separately and
        used only for reporting.

        Matching a record and resolving its agents are two questions, so they are
        two calls. The caller matches once and passes the result here.
        """
        # Build ReconciliationResult from WEMIMatchResult
        matches: list[ReconciliationMatch] = []
        accepted_candidates: list[BibliographicRecord] = []
        selected_uris: dict[int, list[str]] = {}
        suppressed_uris: dict[int, list[str]] = {}

        is_matched = bool(result.matched_work or result.matched_manifestation)
        if is_matched and result.match_tier != "unmatched":
            matched = result.matched_work
            matches.append(
                ReconciliationMatch(
                    candidate_record_id=str(
                        (matched.work_uri if matched else None)
                        or result.work_uri
                        or result.manifestation_uri
                        or result.target_record_id
                    ),
                    source="authority",
                    match_tier=result.match_tier or "tier2_domain_key",
                    match_score=result.confidence_score or 0.8,
                    dense_score=None,
                    rerank_score=None,
                    volume_match=False,
                    surname_match=True,
                    year_diff=None,
                    accepted=True,
                )
            )
            # Rehydrate the matched WEMI entities so their agents (and thus their
            # authority URIs) are available to AgentAligner.
            candidate = self._hydrate_candidate_record(
                result.matched_work,
                result.matched_expression,
                result.matched_manifestation,
            )
            if candidate is not None:
                accepted_candidates.append(candidate)

        # Tier 1/2: coupled agent alignment against the accepted candidates.
        discovered_uris: dict[int, list[AgentURI]] = self.agent_aligner.align_target_agents(
            target_record.agents, accepted_candidates
        )

        # Tier 3: standalone agent fallback for target agents still unlinked.
        self._resolve_agent_fallback(target_record, discovered_uris, matches)

        # Vocabulary filter and link-policy priority selection. Without this the
        # enricher receives no URIs to inject and every report row stays empty.
        for idx, uri_items in discovered_uris.items():
            raw_uri_strs = [item.uri for item in uri_items]
            if self.vocab_manager is not None:
                sel, sup = self.vocab_manager.select_agent_uris(raw_uri_strs)
            else:
                sel, sup = raw_uri_strs, []
            selected_uris[idx] = sel
            suppressed_uris[idx] = sup

        # Query corpus_linkages for linkage context
        corpus_linkage: Optional[CorpusLinkage] = None
        try:
            corpus_linkage = self.repo.get_corpus_linkage(target_record.source_id)
        except Exception:
            # Corpus linkage may not exist if record hasn't been linked yet
            pass

        return ReconciliationResult(
            target_record=target_record,
            matches=matches,
            accepted_candidates=accepted_candidates,
            discovered_agent_uris=discovered_uris,
            selected_agent_uris=selected_uris,
            suppressed_agent_uris=suppressed_uris,
            corpus_linkage=corpus_linkage,
        )
