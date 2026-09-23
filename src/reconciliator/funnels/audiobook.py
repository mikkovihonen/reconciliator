"""Funnel E (audiobooks): resolve to the underlying textual work, cross-format.

An audiobook is an expression of the same work as the print edition, so tier 2
looks for a text work and then for that work's spoken-word expression. When the
authority holds the work but not a spoken-word expression of it, one is
synthesised rather than reporting no match: the work-level link is the useful
answer, and the expression URI is derived from the work's own.
"""

from __future__ import annotations

from typing import Any, Optional

from reconciliator.config.defaults import get_bundled_section_defaults
from reconciliator.funnels.engine import FunnelContext

_AUDIO_DEFAULTS = dict(get_bundled_section_defaults("funnels").get("audiobook") or {})


def _get_audio_param(ctx: FunnelContext, key: str, default: Any) -> Any:
    if key in ctx.spec.extra:
        return ctx.spec.extra[key]
    funnel_settings = getattr(ctx.config, "funnel_settings", None)
    if funnel_settings is not None:
        audio_conf = getattr(funnel_settings, "audiobook", None)
        if audio_conf is not None and hasattr(audio_conf, key):
            return getattr(audio_conf, key)
    return default


def tier2(record: Any, ctx: FunnelContext) -> Optional[Any]:
    from reconciliator.models import ExpressionEntity

    content_type = str(
        ctx.spec.content_type
        or _get_audio_param(ctx, "content_type", _AUDIO_DEFAULTS["content_type"])
    )

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
    spoken = [e for e in expressions if e.content_type == content_type]
    expression = (
        ctx.align_expression(spoken, record.language, content_type=content_type) if spoken else None
    )
    if not expression:
        default_lang = _get_audio_param(
            ctx, "default_language", _AUDIO_DEFAULTS["default_language"]
        )
        title_suffix = _get_audio_param(ctx, "title_suffix", _AUDIO_DEFAULTS["title_suffix"])
        und_lang = _get_audio_param(
            ctx, "undefined_language", _AUDIO_DEFAULTS["undefined_language"]
        )
        source_id_suffix = _get_audio_param(
            ctx, "source_id_suffix", _AUDIO_DEFAULTS["source_id_suffix"]
        )
        uri_prefix = _get_audio_param(
            ctx,
            "expression_uri_prefix",
            _AUDIO_DEFAULTS["expression_uri_prefix"],
        )

        lang = record.language or default_lang
        uri_lang = record.language or und_lang
        expression = ExpressionEntity(
            id=0,
            work_id=work.id or 0,
            expression_uri=f"{work.work_uri}{uri_prefix}{uri_lang}",
            source=work.source,
            source_id=f"{work.source_id}{source_id_suffix}",
            language=lang,
            content_type=content_type,
            title=f"{work.title}{title_suffix}",
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
