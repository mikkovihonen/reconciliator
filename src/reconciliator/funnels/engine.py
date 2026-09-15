"""The generic funnel template, and the context handlers receive.

One implementation of the three-tier funnel, parameterised by a `FunnelSpec`.
Every funnel that is pure template runs through `run_funnel` unchanged; a funnel
with real logic supplies a handler module defining any of `tier1`, `tier2` or
`tier3`, and the tiers it omits still come from here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional, Protocol

from reconciliator.funnels.spec import FunnelSpec


class FunnelHandler(Protocol):
    """What a handler module may define. Every tier is optional."""

    def tier1(self, record: Any, ctx: FunnelContext) -> Optional[Any]: ...
    def tier2(self, record: Any, ctx: FunnelContext) -> Optional[Any]: ...
    def tier3(self, record: Any, ctx: FunnelContext) -> Optional[Any]: ...


@dataclass
class FunnelContext:
    """The surface a funnel handler is allowed to touch.

    Deliberately narrow. A handler gets the repository, the reconcile config, its
    own spec, the record's extracted identifiers, and the two shared helpers -
    nothing else, so handler code cannot reach into matcher internals and quietly
    depend on them.
    """

    spec: FunnelSpec
    repo: Any
    config: Any
    identifiers: dict[str, list[str]]
    align_expression: Any
    try_identifiers: Any
    vector_or_fts: Any
    rank_candidates: Any
    result_cls: Any
    constituent_cls: Any

    def unmatched(self, record: Any) -> Any:
        """The empty result, so handlers need not import the result class."""
        return self.result_cls(
            target_record_id=record.record_id,
            match_tier="unmatched",
            funnel_used=self.spec.id,
        )


def _constituents(record: Any, ctx: FunnelContext) -> list[Any]:
    """Build constituent-work matches from the analytics extracted at ingest."""
    from reconciliator.matcher import extract_target_analytics

    mode = ctx.spec.constituents
    if mode == "none":
        return []
    analytics = extract_target_analytics(record)
    if mode == "titles":
        return [ctx.constituent_cls(title=t) for t, _a in analytics]

    # "resolve": look each constituent up as a work of its own.
    out = []
    for title, author in analytics:
        found = ctx.repo.find_works_by_author_and_title(author, title, work_type="text")
        if found:
            out.append(
                ctx.constituent_cls(
                    title=title,
                    matched_work=found[0],
                    work_uri=found[0].work_uri,
                    relationship_type="constituent_of",
                )
            )
        else:
            out.append(ctx.constituent_cls(title=title, relationship_type="constituent_of"))
    return out


def _relationships(work: Any, ctx: FunnelContext) -> list[Any]:
    """Stored relationships, or the one this funnel asserts by matching."""
    from reconciliator.models import WorkRelationship

    spec = ctx.spec
    if spec.relationships:
        return ctx.repo.get_work_relationships(work.id)
    if spec.asserts_relationship:
        return [
            WorkRelationship(
                source_work_id=0,
                target_work_id=work.id or 0,
                relationship_type=spec.asserts_relationship,
            )
        ]
    return []


def default_tier2(record: Any, ctx: FunnelContext) -> Optional[Any]:
    """Author key plus title stem, filtered by the spec's work type."""
    spec = ctx.spec
    candidates: list[Any] = []
    for work_type in spec.work_types or [None]:
        candidates = ctx.repo.find_works_by_author_and_title(
            record.author_key, record.title_stem, work_type=work_type
        )
        if candidates:
            break
    if not candidates:
        return None

    # Rank rather than take candidates[0]. The blocking query returns everything
    # sharing an author key and title stem, which for a prolific author under a
    # common title is several genuinely different works; the first row is
    # whichever the index happened to return. Ranking applies the volume,
    # surname and year discriminators and the configured score fusion.
    # require_threshold=False: these candidates already matched the author key
    # and title stem exactly, so the score orders them and only the volume,
    # surname and year vetoes reject them.
    ranked = ctx.rank_candidates(
        record, [(c, spec.confidence) for c in candidates], require_threshold=False
    )
    accepted = [(work, verdict) for work, verdict in ranked if verdict.accepted]
    if not accepted:
        # Every candidate was rejected by a discriminator. Falling through to
        # tier 3 is right: a rejected tier-2 candidate is not evidence of
        # absence, and the dense tier searches on different evidence.
        return None

    work, verdict = accepted[0]
    expression = None
    if spec.align_expression:
        expressions = ctx.repo.get_expressions_for_work(work.id)
        expression = ctx.align_expression(
            expressions,
            record.language,
            content_type=spec.content_type,
            translator_signature=record.get_first_key("translators_signature"),
        )

    return ctx.result_cls(
        target_record_id=record.record_id,
        matched_work=work,
        matched_expression=expression,
        work_uri=work.work_uri,
        expression_uri=expression.expression_uri if expression else None,
        match_tier=spec.match_tier,
        funnel_used=spec.id,
        # The fused score the evaluator produced, not the spec's flat figure: a
        # tier that compares candidates should report which one it found.
        confidence_score=float(verdict.match_score),
        relationships=_relationships(work, ctx),
        constituent_matches=_constituents(record, ctx),
    )


def run_funnel(
    spec: FunnelSpec,
    record: Any,
    ctx: FunnelContext,
    handler: Optional[Any] = None,
) -> Any:
    """Run one funnel: tier 1, then tier 2, then tier 3, then unmatched."""
    tier1 = getattr(handler, "tier1", None) if handler else None
    result = (
        tier1(record, ctx)
        if tier1
        else ctx.try_identifiers(record, ctx.identifiers, spec.id, spec.identifier_schemes)
    )
    if result:
        return result

    tier2 = getattr(handler, "tier2", None) if handler else None
    result = tier2(record, ctx) if tier2 else default_tier2(record, ctx)
    if result:
        return result

    tier3 = getattr(handler, "tier3", None) if handler else None
    result = (
        tier3(record, ctx)
        if tier3
        else ctx.vector_or_fts(record, spec.fallback_work_type, spec.id)
    )
    if result:
        return result

    return ctx.unmatched(record)
