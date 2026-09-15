"""Funnel D (notated music): tier 2 reports whether a medium of performance exists.

The only thing this funnel does beyond the template is name its match tier after
the evidence it actually found: a work with a recorded medium of performance is a
music signature match, one without is an ordinary domain-key match.
"""

from __future__ import annotations

from typing import Any, Optional

from reconciliator.funnels.engine import FunnelContext, default_tier2


def tier2(record: Any, ctx: FunnelContext) -> Optional[Any]:
    result = default_tier2(record, ctx)
    if result is None or result.matched_work is None:
        return result
    if not ctx.repo.get_mediums_of_performance(result.matched_work.id):
        result.match_tier = "tier2_domain_key"
    return result
