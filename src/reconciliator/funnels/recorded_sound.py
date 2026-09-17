"""Funnel B (recorded sound): a tracklist-overlap tier before the generic fallback.

Two releases of the same album often share no publisher number and carry
different album titles, but agree on their track listing. This tier finds
candidates by shared track title and accepts a Jaccard overlap of 0.75.

The original implementation was ported verbatim into stage 3 and was wrong twice
over. It compared the record's tracks against `{w.title for w in
find_works_by_type("music") if w.id != candidate.id}` — the *titles of every
other music work in the store*, which is near-identical for every candidate and
measures nothing resembling tracklist agreement. It also re-ran that unbounded
query inside the candidate loop, making the tier quadratic in the size of the
music collection. Both are fixed here: a candidate is compared against its own
constituent tracks, and candidates come from a key lookup rather than a scan.
"""

from __future__ import annotations

from typing import Any, Optional

from reconciliator.funnels.engine import FunnelContext

JACCARD_THRESHOLD = 0.75
# Enough to cover a record's tracks appearing across several releases without
# letting a very common track title drag in the whole collection.
MAX_CANDIDATES = 50


def _candidate_manifestations(tracks: set[str], ctx: FunnelContext) -> list[int]:
    """Manifestation ids sharing at least one track title with the record.

    Uses the `analytic_title` keys written at ingest, so this is an index lookup
    per track rather than a scan of every music work.
    """
    seen: list[int] = []
    for title in sorted(tracks):
        for manifestation_id in ctx.repo.find_entities_by_key("analytic_title", title):
            if manifestation_id not in seen:
                seen.append(manifestation_id)
                if len(seen) >= MAX_CANDIDATES:
                    return seen
    return seen


def _tracks_of(manifestation_id: int, ctx: FunnelContext) -> set[str]:
    """The constituent track titles a candidate manifestation actually carries."""
    bag = ctx.repo.get_entity_keys("manifestation", manifestation_id)
    return {t for t in bag.get("analytic_title", []) if t}


def _constituent_matches(work: Any, tracks: set[str], ctx: FunnelContext) -> list[Any]:
    """The matched album's own tracks, resolved to works, in track order.

    This reported the *target's* track titles and nothing else, which told a
    reader what the target said and not what it matched. The accepted album's
    stored constituents are the works it contains, in one indexed lookup, so
    each target track can be reported with the work it corresponds to.

    Falls back to the target's titles for an album ingested before constituents
    existed, or under `constituents_mode: keys_only`, where the store has the
    tracklist as keys and not as rows.
    """
    stored = []
    if (
        work is not None
        and getattr(work, "id", None)
        and hasattr(ctx.repo, "get_constituent_works")
    ):
        stored = ctx.repo.get_constituent_works(work.id)
    if not stored:
        return [ctx.constituent_cls(title=t) for t in sorted(tracks)]

    return [
        ctx.constituent_cls(
            title=constituent.title,
            matched_work=constituent,
            work_uri=constituent.work_uri,
            relationship_type="constituent_of",
        )
        for constituent in stored
    ]


def tier3(record: Any, ctx: FunnelContext) -> Optional[Any]:
    from reconciliator.utils import compute_jaccard_overlap, extract_target_track_titles

    tracks = extract_target_track_titles(record)
    if tracks:
        best: Optional[tuple[float, Any]] = None
        for manifestation_id in _candidate_manifestations(tracks, ctx):
            manifestation = ctx.repo.get_manifestation(manifestation_id)
            if manifestation is None or not manifestation.work_id:
                continue
            if manifestation.source_id == record.record_id:
                continue  # the record itself, when it is already in the store
            overlap = compute_jaccard_overlap(tracks, _tracks_of(manifestation_id, ctx))
            if overlap >= JACCARD_THRESHOLD and (best is None or overlap > best[0]):
                best = (overlap, manifestation)

        if best is not None:
            overlap, manifestation = best
            work = ctx.repo.get_work(manifestation.work_id)
            if work is not None:
                expressions = ctx.repo.get_expressions_for_work(work.id)
                expression = ctx.align_expression(
                    expressions, record.language, content_type=ctx.spec.content_type
                )
                return ctx.result_cls(
                    target_record_id=record.record_id,
                    matched_work=work,
                    matched_expression=expression,
                    matched_manifestation=manifestation,
                    work_uri=work.work_uri,
                    expression_uri=expression.expression_uri if expression else None,
                    manifestation_uri=manifestation.manifestation_uri,
                    match_tier="tier3_tracklist_jaccard",
                    funnel_used=ctx.spec.id,
                    # The overlap itself, rather than a fixed 0.85: a tier that
                    # accepts a range of agreement should report which it found.
                    confidence_score=float(overlap),
                    constituent_matches=_constituent_matches(work, tracks, ctx),
                )

    return ctx.vector_or_fts(record, ctx.spec.fallback_work_type, ctx.spec.id)
