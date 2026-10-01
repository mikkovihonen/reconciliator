"""Authority agent URI extraction and agent resolution stage."""

from __future__ import annotations

import json
from typing import Any, Callable, Optional

from reconciliator.config import ReconcileConfig
from reconciliator.database import WEMIRepository
from reconciliator.embedding.protocol import VectorEmbedder
from reconciliator.indexer.base import VectorIndex
from reconciliator.models import (
    AgentEntity,
    AgentURI,
    BibliographicRecord,
    CorpusLinkage,
    ReconciliationMatch,
)
from reconciliator.reconciliation.agents import AgentAligner, AgentFallbackResolver
from reconciliator.reconciliation.results import (
    ReconciliationResult,
    WEMIMatchResult,
)
from reconciliator.vocab import (
    VocabularyManager,
    normalize_subject_vocabulary,
)


def extract_agent_uris_from_identifiers(
    agent: AgentEntity,
    repo: Optional[WEMIRepository] = None,
) -> tuple[list[str], list[str]]:
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
    if not idents and agent.id and repo is not None:
        idents = repo.get_identifiers_for_entity("agent", agent.id)
    from reconciliator.utils.curie import format_source_uri
    from reconciliator.vocab import format_wikidata_uri

    for ident in idents:
        src = (ident.source or "").lower()
        if src == "wikidata":
            _add(format_wikidata_uri(ident.source_id), "1")
        else:
            _add(format_source_uri(src, ident.source_id), "0")

    if agent.source and agent.source_id and agent.source.lower() != "unknown":
        src = agent.source.lower()
        if src == "wikidata":
            _add(format_wikidata_uri(agent.source_id), "1")
        else:
            _add(format_source_uri(src, agent.source_id), "0")

    return u0, u1


class AgentResolutionStage:
    """Manages coupled agent alignment on matched works and standalone agent fallback resolution."""

    def __init__(
        self,
        repo: WEMIRepository,
        agent_aligner: Optional[AgentAligner] = None,
        agent_fallback_resolver: Optional[AgentFallbackResolver] = None,
        vocab_manager: Optional[VocabularyManager] = None,
        agent_indexer: Optional[VectorIndex] = None,
        embedder: Optional[VectorEmbedder] = None,
        config: Optional[ReconcileConfig] = None,
        hydrate_candidate_record_fn: Optional[Callable[..., Optional[BibliographicRecord]]] = None,
        retrieval_top_k_fn: Optional[Callable[[int], int]] = None,
    ) -> None:
        self.repo = repo
        self.agent_aligner = agent_aligner or AgentAligner()
        self.agent_fallback_resolver = agent_fallback_resolver
        self.vocab_manager = vocab_manager
        self.agent_indexer = agent_indexer
        self.embedder = embedder
        self.config = config
        self.hydrate_candidate_record_fn = hydrate_candidate_record_fn
        self.retrieval_top_k_fn = retrieval_top_k_fn or (lambda default=50: default)

    def agent_uris_from_identifiers(self, agent: AgentEntity) -> tuple[list[str], list[str]]:
        """Collect an authority agent's $0 and $1 URIs from the identifiers table and its own columns."""
        return extract_agent_uris_from_identifiers(agent, self.repo)

    def resolve_agent_fallback(
        self,
        target_record: BibliographicRecord,
        discovered_uris: dict[int, list[AgentURI]],
        matches: list[ReconciliationMatch],
    ) -> None:
        """Tier 3: resolve target agents left unlinked by coupled work matching."""
        cfg = self.config
        if (
            cfg is None
            or not cfg.fallback_agent_reconciliation
            or self.agent_indexer is None
            or self.embedder is None
            or self.agent_fallback_resolver is None
        ):
            return

        from reconciliator.embedding.template import WEMISerializationEngine
        from reconciliator.vocab import detect_vocabulary

        wemi_engine = WEMISerializationEngine()
        for idx, target_agent in enumerate(target_record.agents):
            if discovered_uris.get(idx) or target_agent.is_fictional:
                continue

            agent_text = wemi_engine.render_agent(target_agent)
            if not agent_text or not agent_text.strip():
                continue

            if hasattr(self.repo, "embed_texts_cached"):
                q_vec = self.repo.embed_texts_cached(self.embedder, [agent_text], entity_type="agent")
            else:
                q_vec = self.embedder.embed_texts([agent_text])
            cand_scores, cand_ids = self.agent_indexer.search(q_vec, top_k=self.retrieval_top_k_fn(50))
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

            from reconciliator.utils.curie import (
                _get_sources_registry,
                format_source_uri,
                to_actionable_uri,
            )
            from reconciliator.vocab import format_wikidata_uri

            if best_cand.get("id"):
                try:
                    agent_idents = self.repo.get_identifiers_for_entity(
                        "agent", int(best_cand["id"])
                    )
                    for ident in agent_idents:
                        is_wd = ident.scheme.lower() == "wikidata"
                        uri = (
                            format_wikidata_uri(ident.source_id)
                            if is_wd
                            else format_source_uri(ident.scheme, ident.source_id)
                        )
                        _accept(
                            uri,
                            "1" if is_wd else "0",
                            "WIKIDATA" if is_wd else ident.scheme.upper(),
                        )
                except Exception:
                    pass

            c_src = best_cand.get("source")
            c_sid = best_cand.get("source_id")
            if c_src and c_sid and not str(c_src).startswith(("urn", "unknown")):
                c_src_l = str(c_src).lower()
                formatted_uri = format_source_uri(c_src_l, str(c_sid))
                src_info = _get_sources_registry().get(c_src_l, {})
                subf = str(src_info.get("subfield", "1" if c_src_l == "wikidata" else "0"))
                _accept(formatted_uri, subf, str(c_src).upper())

            if best_cand.get("agent_uri"):
                raw_a_uri = str(best_cand["agent_uri"])
                if not raw_a_uri.startswith(("urn:agent:", "urn:reconciliator:")):
                    a_uri = to_actionable_uri(raw_a_uri)
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
                from reconciliator.utils.curie import format_source_uri

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
        """Resolve authority URIs for a record's agents, given its work match."""
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
            if self.hydrate_candidate_record_fn is not None:
                candidate = self.hydrate_candidate_record_fn(
                    result.matched_work,
                    result.matched_expression,
                    result.matched_manifestation,
                )
                if candidate is not None:
                    accepted_candidates.append(candidate)

        discovered_uris: dict[int, list[AgentURI]] = self.agent_aligner.align_target_agents(
            target_record.agents, accepted_candidates
        )

        self.resolve_agent_fallback(target_record, discovered_uris, matches)

        for idx, uri_items in discovered_uris.items():
            raw_uri_strs = [item.uri for item in uri_items]
            if self.vocab_manager is not None:
                sel, sup = self.vocab_manager.select_agent_uris(raw_uri_strs)
            else:
                sel, sup = raw_uri_strs, []
            selected_uris[idx] = sel
            suppressed_uris[idx] = sup

        corpus_linkage: Optional[CorpusLinkage] = None
        try:
            corpus_linkage = self.repo.get_corpus_linkage(target_record.source_id)
        except Exception:
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
