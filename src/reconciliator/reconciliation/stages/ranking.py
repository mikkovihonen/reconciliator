"""Candidate ranking, candidate hydration, and multi-edition evaluation stage."""

from __future__ import annotations

from typing import Any, Optional

from reconciliator.config import ReconcileConfig, get_work_authority_sources
from reconciliator.database import WEMIRepository
from reconciliator.models import (
    Agent,
    AgentEntity,
    BibliographicRecord,
    ContributionEntity,
    ExpressionEntity,
    IdentifierEntity,
    ManifestationEntity,
    MatchRecord,
    ReconciliationMatch,
    RecordIdentifiers,
    WorkEntity,
)
from reconciliator.normalizers.blocking import compute_author_key
from reconciliator.reconciliation.stages.agents import extract_agent_uris_from_identifiers
from reconciliator.rerank import CandidateEvaluator

DEFAULT_MAX_EDITIONS_COMPARED = 25


class CandidateRankingStage:
    """Manages multi-edition candidate hydration, discriminator vetoes, and composite reranking."""

    def __init__(
        self,
        repo: WEMIRepository,
        evaluator: Optional[CandidateEvaluator] = None,
        config: Optional[ReconcileConfig] = None,
    ) -> None:
        self.repo = repo
        self.evaluator = evaluator
        self.config = config
        self._work_manifestations_cache: dict[int, list[ManifestationEntity]] = {}
        self._hydration_cache: dict[
            tuple[Optional[int], Optional[int], Optional[int]], BibliographicRecord
        ] = {}

    def target_view(self, record: MatchRecord) -> BibliographicRecord:
        """A BibliographicRecord view of a MatchRecord, for candidate evaluation."""
        work_sources = {s.lower() for s in get_work_authority_sources(self.config)}
        target_work_ids = {
            s: list(v) for s, v in record.identifiers.items() if s.lower() in work_sources
        }
        return BibliographicRecord(
            source_id=record.record_id,
            source=record.format or "target",
            title=record.title or "",
            uniform_title=record.uniform_title,
            publication_year=record.year,
            volume_designation=record.volume_designation,
            language=record.language,
            work_identifiers=target_work_ids,
            agents=[Agent(name=record.author_key or "", surname=record.author_key)]
            if record.author_key
            else [],
        )

    def hard_checks_pass(self, verdict: ReconciliationMatch) -> bool:
        """Whether a candidate survives the vetoes, ignoring the fuzzy threshold."""
        rerank_cfg = getattr(self.config, "rerank", None)
        max_year = getattr(rerank_cfg, "max_year_difference", None)
        year_ok = (
            verdict.year_diff is None or max_year is None or abs(verdict.year_diff) <= max_year
        )
        return bool(verdict.volume_match and verdict.surname_match and year_ok)

    def hydrate_candidate_record(
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
        if work is None and manifestation is None:
            return None

        cache_key = (
            work.id if work else None,
            expression.id if expression else None,
            manifestation.id if manifestation else None,
        )
        if cache_key in self._hydration_cache:
            return self._hydration_cache[cache_key]

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
                if contributions_map is not None:
                    contribs = contributions_map.get((entity_type, entity_id), [])
                else:
                    contribs = self.repo.get_contributions_for_entity(entity_type, entity_id)
            except Exception:
                continue
            for c in contribs:
                if c.agent_id and c.agent_id not in agent_ids:
                    agent_ids.append(c.agent_id)

        rels_map: dict[int, list[dict[str, Any]]] = {}
        if agent_ids:
            if agent_rels_map is not None:
                rels_map = {aid: agent_rels_map.get(aid, []) for aid in agent_ids}
            elif hasattr(self.repo, "get_agent_relationships_map"):
                try:
                    rels_map = self.repo.get_agent_relationships_map(agent_ids)
                except Exception:
                    rels_map = {}

        agents: list[Agent] = []
        for aid in agent_ids:
            if agents_map is not None and aid in agents_map:
                entity = agents_map[aid]
            else:
                entity = self.repo.get_agent(aid)
            if entity is None:
                continue
            u0, u1 = extract_agent_uris_from_identifiers(entity, self.repo)
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
                    relationships=rels_map.get(aid, []),
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
                if manifestation_identifiers_map is not None:
                    m_idents = manifestation_identifiers_map.get(manifestation.id, [])
                else:
                    m_idents = self.repo.get_identifiers_for_entity(
                        "manifestation", manifestation.id
                    )
                for ident in m_idents:
                    bucket = getattr(identifiers, (ident.scheme or "").lower(), None)
                    if isinstance(bucket, list) and ident.value not in bucket:
                        bucket.append(ident.value)
            except Exception:
                pass

        candidate_record = BibliographicRecord(
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
        max_cache = getattr(self.config, "entity_cache_size", 50000) if self.config else 50000
        if len(self._hydration_cache) < max_cache:
            self._hydration_cache[cache_key] = candidate_record
        return candidate_record

    def rank_candidates(
        self,
        record: MatchRecord,
        candidates: list[tuple[WorkEntity, float]],
        acceptance_threshold: Optional[float] = None,
        require_threshold: bool = True,
        target_encoding: Optional[Any] = None,
    ) -> list[tuple[WorkEntity, ReconciliationMatch]]:
        """Score candidates against the record and return them best-first."""
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

        target = self.target_view(record)

        max_editions = (
            getattr(self.config, "max_editions_compared", DEFAULT_MAX_EDITIONS_COMPARED)
            if self.config
            else DEFAULT_MAX_EDITIONS_COMPARED
        )
        work_ids = [work.id for work, _ in candidates if work.id]
        uncached_work_ids = [wid for wid in work_ids if wid not in self._work_manifestations_cache]
        if uncached_work_ids:
            if hasattr(self.repo, "get_manifestations_for_works"):
                fetched_manifestations = self.repo.get_manifestations_for_works(uncached_work_ids)
                self._work_manifestations_cache.update(fetched_manifestations)
            else:
                for wid in uncached_work_ids:
                    self._work_manifestations_cache[wid] = self.repo.get_manifestations_for_work(
                        wid
                    )

        needed_pairs: list[tuple[WorkEntity, Optional[ManifestationEntity]]] = []
        for work, _ in candidates:
            manifestations = self._work_manifestations_cache.get(work.id, []) if work.id else []
            for manifestation in (manifestations or [None])[:max_editions]:
                cache_key = (
                    work.id if work else None,
                    None,
                    manifestation.id if manifestation else None,
                )
                if cache_key not in self._hydration_cache:
                    needed_pairs.append((work, manifestation))

        contributions_map: dict[tuple[str, int], list[ContributionEntity]] = {}
        agents_map: dict[int, AgentEntity] = {}
        agent_rels_map: dict[int, list[dict[str, Any]]] = {}
        manifestation_idents_map: dict[int, list[IdentifierEntity]] = {}

        if needed_pairs:
            entity_specs: list[tuple[str, int]] = []
            m_ids: list[int] = []
            for w, m in needed_pairs:
                if w.id:
                    entity_specs.append(("work", w.id))
                if m and m.id:
                    entity_specs.append(("manifestation", m.id))
                    m_ids.append(m.id)

            if hasattr(self.repo, "get_contributions_for_entities"):
                contributions_map = self.repo.get_contributions_for_entities(entity_specs)
            else:
                for etype, eid in entity_specs:
                    try:
                        contributions_map[(etype, eid)] = self.repo.get_contributions_for_entity(
                            etype, eid
                        )
                    except Exception:
                        pass

            if m_ids:
                if hasattr(self.repo, "get_identifiers_for_entities"):
                    manifestation_idents_map = self.repo.get_identifiers_for_entities(
                        "manifestation", m_ids
                    )
                else:
                    for mid in m_ids:
                        try:
                            manifestation_idents_map[mid] = self.repo.get_identifiers_for_entity(
                                "manifestation", mid
                            )
                        except Exception:
                            pass

            all_agent_ids = list(
                {c.agent_id for clist in contributions_map.values() for c in clist if c.agent_id}
            )
            if all_agent_ids:
                agent_profiles = self.repo.get_agents_by_ids(all_agent_ids)
                for aid, data in agent_profiles.items():
                    agents_map[aid] = AgentEntity.model_validate(data)
                if hasattr(self.repo, "get_agent_relationships_map"):
                    try:
                        agent_rels_map = self.repo.get_agent_relationships_map(all_agent_ids)
                    except Exception:
                        agent_rels_map = {}

        eval_items: list[tuple[WorkEntity, BibliographicRecord, float]] = []
        for work, dense_score in candidates:
            manifestations = self._work_manifestations_cache.get(work.id, []) if work.id else []
            for manifestation in (manifestations or [None])[:max_editions]:
                candidate = self.hydrate_candidate_record(
                    work,
                    None,
                    manifestation,
                    contributions_map=contributions_map,
                    agents_map=agents_map,
                    agent_rels_map=agent_rels_map,
                    manifestation_identifiers_map=manifestation_idents_map,
                )
                if candidate is None:
                    continue
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
                eval_items.append((work, candidate, dense_score))

        if not eval_items:
            return []

        cand_pairs = [(c_rec, d_score) for _, c_rec, d_score in eval_items]
        if hasattr(self.evaluator, "evaluate_candidates_batch"):
            verdicts = self.evaluator.evaluate_candidates_batch(
                target, cand_pairs, acceptance_threshold=threshold, target_encoding=target_encoding
            )
        else:
            verdicts = [
                self.evaluator.evaluate_candidate(
                    target, c_rec, d_score, acceptance_threshold=threshold
                )
                for c_rec, d_score in cand_pairs
            ]

        best_by_work: dict[Any, tuple[WorkEntity, ReconciliationMatch]] = {}
        for (work, _, _), verdict in zip(eval_items, verdicts):
            if not require_threshold and not verdict.accepted:
                verdict = verdict.model_copy(update={"accepted": self.hard_checks_pass(verdict)})
            wid = work.id if work.id is not None else id(work)
            if wid not in best_by_work:
                best_by_work[wid] = (work, verdict)
            else:
                existing_verdict = best_by_work[wid][1]
                if (verdict.accepted, verdict.match_score) > (
                    existing_verdict.accepted,
                    existing_verdict.match_score,
                ):
                    best_by_work[wid] = (work, verdict)

        scored = list(best_by_work.values())
        scored.sort(key=lambda pair: (pair[1].accepted, pair[1].match_score), reverse=True)
        return scored
