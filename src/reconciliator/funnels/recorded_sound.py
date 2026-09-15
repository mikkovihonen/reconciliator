"""Funnel B (recorded sound): a tracklist-overlap tier before the generic fallback.

Two releases of the same album often share no publisher number and carry
different album titles, but agree on their track listing. This tier compares the
record's analytics against candidate works and accepts a Jaccard overlap of 0.75.

NOTE: the comparison is ported unchanged from the original funnel and is
suspect - `constituent_titles` collects the titles of *other music works* rather
than the candidate's own constituents, so the overlap is not measuring what the
docstring claims. It is preserved verbatim here because this stage is meant to be
output-identical; fixing it is a behavioural change and belongs in its own diff.
"""

from __future__ import annotations

from typing import Any, Optional

from reconciliator.funnels.engine import FunnelContext

JACCARD_THRESHOLD = 0.75
CANDIDATE_LIMIT = 50


def tier3(record: Any, ctx: FunnelContext) -> Optional[Any]:
    from reconciliator.matcher import compute_jaccard_overlap, extract_target_track_titles

    tracks = extract_target_track_titles(record)
    if tracks:
        for candidate in ctx.repo.find_works_by_type("music", limit=CANDIDATE_LIMIT):
            constituent_titles = {
                w.title for w in ctx.repo.find_works_by_type("music") if w.id != candidate.id
            }
            if compute_jaccard_overlap(tracks, constituent_titles) >= JACCARD_THRESHOLD:
                expressions = ctx.repo.get_expressions_for_work(candidate.id)
                expression = ctx.align_expression(
                    expressions, record.language, content_type=ctx.spec.content_type
                )
                return ctx.result_cls(
                    target_record_id=record.record_id,
                    matched_work=candidate,
                    matched_expression=expression,
                    work_uri=candidate.work_uri,
                    expression_uri=expression.expression_uri if expression else None,
                    match_tier="tier3_tracklist_jaccard",
                    funnel_used=ctx.spec.id,
                    confidence_score=0.85,
                    constituent_matches=[ctx.constituent_cls(title=t) for t in tracks],
                )

    return ctx.vector_or_fts(record, ctx.spec.fallback_work_type, ctx.spec.id)
