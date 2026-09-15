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

import importlib
import importlib.util
import re
import sys
from pathlib import Path
from types import ModuleType
from typing import Optional

from reconciliator.funnels.builtin import BUILTIN_SPECS
from reconciliator.funnels.engine import FunnelContext, default_tier2, run_funnel
from reconciliator.funnels.spec import FunnelSpec, FunnelWhen

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

_BARE_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class FunnelHandlerError(ValueError):
    """A funnel's handler could not be resolved."""


def _is_path(value: str) -> bool:
    return value.endswith(".py") or "/" in value or "\\" in value


def resolve_handler(
    value: Optional[str],
    funnel_id: str = "<unknown>",
    config_dir: Optional[Path] = None,
) -> Optional[ModuleType]:
    """Resolve a handler reference to a module, or None when there is none.

    `config_dir` anchors relative paths - the directory of the config file, not
    the process working directory, so a config keeps working wherever the CLI is
    invoked from.
    """
    if not value:
        return None

    if not _is_path(value):
        if not _BARE_NAME.match(value):
            raise FunnelHandlerError(
                f"funnel {funnel_id!r}: handler {value!r} is neither a path "
                f"(ending in .py) nor a valid built-in name"
            )
        target = BUILTIN_HANDLERS.get(value)
        if target is None:
            raise FunnelHandlerError(
                f"funnel {funnel_id!r}: unknown built-in handler {value!r}. "
                f"Available: {', '.join(sorted(BUILTIN_HANDLERS))}"
            )
        return importlib.import_module(target)

    path = Path(value)
    if not path.is_absolute() and config_dir is not None:
        path = (config_dir / path).resolve()
    if not path.is_file():
        raise FunnelHandlerError(
            f"funnel {funnel_id!r}: handler file not found: {path}"
        )

    # A synthetic, unique module name, so two funnels whose handler files share a
    # basename do not collide in sys.modules.
    mod_name = f"reconciliator_funnel_handler_{funnel_id}_{abs(hash(str(path))):x}"
    spec = importlib.util.spec_from_file_location(mod_name, path)
    if spec is None or spec.loader is None:
        raise FunnelHandlerError(f"funnel {funnel_id!r}: cannot load handler from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


def resolve_specs(
    specs: list[FunnelSpec], config_dir: Optional[Path] = None
) -> list[tuple[FunnelSpec, Optional[ModuleType]]]:
    """Pair every spec with its resolved handler, failing fast on a bad one."""
    return [(s, resolve_handler(s.handler, s.id, config_dir)) for s in specs]
