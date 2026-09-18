"""Funnel D (notated music): tier 2 reports whether a medium of performance exists.

The only thing this funnel does beyond the template is name its match tier after
the evidence it actually found: a work with a recorded medium of performance is a
music signature match, one without is an ordinary domain-key match.
"""

from __future__ import annotations

from typing import Any, Optional

from reconciliator.config import get_bundled_section_defaults
from reconciliator.funnels.engine import FunnelContext, default_tier2

_NM_DEFAULTS = dict(get_bundled_section_defaults("funnels").get("notated_music") or {})
DEFAULT_FALLBACK_MATCH_TIER = str(
    _NM_DEFAULTS.get("fallback_match_tier", "tier2_domain_key")
)


def tier2(record: Any, ctx: FunnelContext) -> Optional[Any]:
    result = default_tier2(record, ctx)
    if result is None or result.matched_work is None:
        return result
    if not ctx.repo.get_mediums_of_performance(result.matched_work.id):
        funnel_settings = getattr(ctx.config, "funnel_settings", None)
        nm_conf = getattr(funnel_settings, "notated_music", None) if funnel_settings else None
        fallback_tier = (
            ctx.spec.extra.get("fallback_match_tier")
            or (getattr(nm_conf, "fallback_match_tier", None) if nm_conf else None)
            or DEFAULT_FALLBACK_MATCH_TIER
        )
        result.match_tier = fallback_tier
    return result
