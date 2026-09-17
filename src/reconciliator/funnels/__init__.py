"""Domain funnels: declared specs, a generic engine, and handlers for the rest.

A funnel is a `FunnelSpec`. Most are pure data; the few with real logic name a
`handler`, which is either a built-in name or a path to a Python file.

    handler: recorded_sound                              # built-in, by name
    handler: src/reconciliator/funnels/recorded_sound.py # path, config-relative
    handler: /opt/site/funnels/my_rule.py                # path, absolute

A value containing a path separator or ending in `.py` is a path; anything else
is a built-in name. The two cannot collide, since built-in names are bare
identifiers. Built-ins are named rather than pathed so they keep resolving after
`pip install`, where no `src/` directory exists.

Handlers resolve once, when configuration loads, so a bad path or an unknown name
is a startup error naming the offending funnel rather than a funnel that silently
degrades to the generic template.
"""

from __future__ import annotations

from pathlib import Path
from types import ModuleType
from typing import Optional

from reconciliator.funnels.builtin import BUILTIN_SPECS
from reconciliator.funnels.engine import FunnelContext, default_tier2, run_funnel
from reconciliator.funnels.spec import FunnelSpec, FunnelWhen
from reconciliator.module_ref import resolve_module_ref

__all__ = [
    "BUILTIN_HANDLERS",
    "BUILTIN_SPECS",
    "FunnelContext",
    "FunnelSpec",
    "FunnelWhen",
    "default_tier2",
    "resolve_handler",
    "run_funnel",
]

# Built-in handler modules, by name. Importable paths rather than imported
# modules so that naming one does not cost an import when it is unused.
BUILTIN_HANDLERS: dict[str, str] = {
    "recorded_sound": "reconciliator.funnels.recorded_sound",
    "notated_music": "reconciliator.funnels.notated_music",
    "audiobook": "reconciliator.funnels.audiobook",
}


class FunnelHandlerError(ValueError):
    """A funnel's handler could not be resolved."""


def resolve_handler(
    value: Optional[str],
    funnel_id: str = "<unknown>",
    config_dir: Optional[Path] = None,
) -> Optional[ModuleType]:
    """Resolve a handler reference to a module, or None when there is none.

    The reference rule itself lives in `reconciliator.module_ref`, shared with the
    `extractor` on a constituent profile: a user who has learned one of the two
    has learned the other, so they must not drift.
    """
    return resolve_module_ref(
        value,
        registry=BUILTIN_HANDLERS,
        kind="funnel",
        subject=funnel_id,
        config_dir=config_dir,
        error_cls=FunnelHandlerError,
    )


def resolve_specs(
    specs: list[FunnelSpec], config_dir: Optional[Path] = None
) -> list[tuple[FunnelSpec, Optional[ModuleType]]]:
    """Pair every spec with its resolved handler, failing fast on a bad one."""
    return [(s, resolve_handler(s.handler, s.id, config_dir)) for s in specs]
