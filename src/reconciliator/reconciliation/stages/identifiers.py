"""Tier 1 exact identifier matching and expression alignment stage."""

from __future__ import annotations

from typing import Optional

from reconciliator.config import ReconcileConfig, get_work_authority_sources
from reconciliator.database import WEMIRepository
from reconciliator.models import (
    ExpressionEntity,
    MatchRecord,
)
from reconciliator.reconciliation.agents import AgentAligner
from reconciliator.reconciliation.results import WEMIMatchResult


class IdentifierMatchingStage:
    """Performs Tier 1 standard identifier resolution and expression alignment."""

    def __init__(
        self,
        repo: WEMIRepository,
        agent_aligner: Optional[AgentAligner] = None,
        config: Optional[ReconcileConfig] = None,
        authoritative_only: bool = True,
    ) -> None:
        self.repo = repo
        self.config = config
        self.authoritative_only = authoritative_only
        self.agent_aligner = agent_aligner or AgentAligner()

    def align_expression(
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
            trans_thresh = (
                getattr(self.config, "translator_alignment_threshold", 0.70)
                if self.config
                else 0.70
            )
            if best_match and best_score >= trans_thresh:
                return best_match

        return filtered[0]

    def try_identifiers(
        self,
        target: MatchRecord,
        ids: dict[str, list[str]],
        funnel_name: str,
        preferred_schemes: Optional[list[str]] = None,
    ) -> Optional[WEMIMatchResult]:
        """Attempt fast-path standard identifier resolution against repository."""
        schemes_order: list[str] = []
        if preferred_schemes:
            schemes_order.extend([s.lower() for s in preferred_schemes])

        for w_src in get_work_authority_sources(self.config):
            w_src_l = w_src.lower()
            if w_src_l in ids and w_src_l not in schemes_order:
                schemes_order.insert(0, w_src_l)
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
                        exp = self.align_expression(
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
                        exp = self.align_expression(
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
