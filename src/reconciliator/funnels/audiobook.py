"""Funnel E (audiobooks): resolve to the underlying textual work, cross-format.

An audiobook is an expression of the same work as the print edition, so tier 2
looks for a text work and then for that work's spoken-word expression. When the
authority holds the work but not a spoken-word expression of it, one is
synthesised rather than reporting no match: the work-level link is the useful
answer, and the expression URI is derived from the work's own.
"""

from __future__ import annotations

from typing import Any, Optional

from reconciliator.funnels.engine import FunnelContext


def tier2(record: Any, ctx: FunnelContext) -> Optional[Any]:
    from reconciliator.models import ExpressionEntity

    candidates: list = []
    for work_type in ctx.spec.work_types or [None]:
        candidates = ctx.repo.find_works_by_author_and_title(
            record.author_key, record.title_stem, work_type=work_type
        )
        if candidates:
            break
    if not candidates:
        return None

    work = candidates[0]
    expressions = ctx.repo.get_expressions_for_work(work.id)
    spoken = [e for e in expressions if e.content_type == "spoken_word"]
    expression = (
        ctx.align_expression(spoken, record.language, content_type="spoken_word")
        if spoken
        else None
    )
    if not expression:
        expression = ExpressionEntity(
            id=0,
            work_id=work.id or 0,
            expression_uri=f"{work.work_uri}#spoken_word_{record.language or 'und'}",
            source=work.source,
            source_id=f"{work.source_id}_audio",
            language=record.language or "fin",
            content_type="spoken_word",
            title=f"{work.title} (äänikirja)",
        )

    return ctx.result_cls(
        target_record_id=record.record_id,
        matched_work=work,
        matched_expression=expression,
        work_uri=work.work_uri,
        expression_uri=expression.expression_uri,
        match_tier=ctx.spec.match_tier,
        funnel_used=ctx.spec.id,
        confidence_score=ctx.spec.confidence,
    )
