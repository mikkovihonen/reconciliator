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


def _constituents(record: Any, ctx: FunnelContext, work: Any = None) -> list[Any]:
    """Build constituent-work matches from the analytics extracted at ingest.

    `mode: resolve` used to run one `find_works_by_author_and_title` per
    constituent title, hardcoded to `work_type="text"` - so an album's tracks
    could never resolve however they were catalogued, and an anthology's stories
    cost a query each. The matched container now answers the question directly:
    its stored `has_constituent` rows are the works it contains, in one indexed
    lookup, and the target's titles are aligned against them.

    The per-title lookup remains as a fallback, without the hardcoded work type,
    for a container ingested before constituents existed or under
    `constituents_mode: keys_only`.
    """
    from reconciliator.utils import extract_target_analytics

    mode = ctx.spec.constituents
    if mode == "none":
        return []
    analytics = extract_target_analytics(record)
    if mode == "titles":
        return [ctx.constituent_cls(title=t) for t, _a in analytics]

    stored = _stored_constituents(work, ctx)
    out = []
    for title, author in analytics:
        match = stored.get(_stem(title))
        if match is None:
            candidates = ctx.repo.find_works_by_author_and_title(
                author, title, work_type=getattr(work, "work_type", None)
            )
            match = candidates[0] if candidates else None
        out.append(
            ctx.constituent_cls(
                title=title,
                matched_work=match,
                work_uri=match.work_uri if match else None,
                relationship_type="constituent_of",
            )
        )
    return out


def _stored_constituents(work: Any, ctx: FunnelContext) -> dict[str, Any]:
    """The matched container's own constituents, keyed by title stem."""
    if work is None or not getattr(work, "id", None):
        return {}
    if not hasattr(ctx.repo, "get_constituent_works"):
        return {}
    return {_stem(c.title): c for c in ctx.repo.get_constituent_works(work.id) if c.title}


def _stem(title: str) -> str:
    from reconciliator.normalizers.blocking import compute_title_stem

    return compute_title_stem(title) or title.strip().casefold()


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
        constituent_matches=_constituents(record, ctx, work),
    )


def run_funnel(
    spec: FunnelSpec,
    record: Any,
    ctx: FunnelContext,
    handler: Optional[Any] = None,
) -> Any:
    """Run one funnel: tier 1, then tier 2, then tier 3, then unmatched.

    A tier decides *what matched*; the funnel decides what to report about it.
    That split is why `_report` runs over every tier's result: constituents and
    stored relationships were filled in by `default_tier2` alone, so an anthology
    matched on its ISBN - the strongest match there is - reported no
    constituents, while the same anthology matched by author and title reported
    all of them.
    """
    tier1 = getattr(handler, "tier1", None) if handler else None
    result = (
        tier1(record, ctx)
        if tier1
        else ctx.try_identifiers(record, ctx.identifiers, spec.id, spec.identifier_schemes)
    )
    if result:
        return _report(result, record, ctx)

    tier2 = getattr(handler, "tier2", None) if handler else None
    result = tier2(record, ctx) if tier2 else default_tier2(record, ctx)
    if result:
        return _report(result, record, ctx)

    tier3 = getattr(handler, "tier3", None) if handler else None
    result = (
        tier3(record, ctx) if tier3 else ctx.vector_or_fts(record, spec.fallback_work_type, spec.id)
    )
    if result:
        return _report(result, record, ctx)

    return ctx.unmatched(record)


def _report(result: Any, record: Any, ctx: FunnelContext) -> Any:
    """Fill in what the funnel reports about a match, whichever tier found it."""
    work = getattr(result, "matched_work", None)
    if work is None:
        return result

    if not getattr(result, "constituent_matches", None):
        result.constituent_matches = _constituents(record, ctx, work)
    # Only the stored kind: an asserted relationship is a claim the matching tier
    # makes, and a tier that did not make it should not have one attributed to it.
    if ctx.spec.relationships and not getattr(result, "relationships", None):
        result.relationships = ctx.repo.get_work_relationships(work.id)
    return result
