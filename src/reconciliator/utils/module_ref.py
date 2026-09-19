"""Resolving a "built-in name or path to a Python file" reference to a module.

Two subsystems let configuration name Python code: a funnel names a `handler`,
and a source's constituent profile names an `extractor`. They must agree about
what a reference means, because a user who has learned one has learned the other:

    recorded_sound                              # built-in, by name
    src/reconciliator/funnels/recorded_sound.py # path, config-relative
    /opt/site/funnels/my_rule.py                # path, absolute

A value ending in `.py` or containing a path separator is a path; anything else
is a built-in name. The two cannot collide, since built-in names are bare
identifiers. Built-ins are named rather than pathed so they keep resolving after
`pip install`, where no `src/` directory exists.

References resolve once, when configuration loads, so a bad path or an unknown
name is a startup error naming what referred to it rather than a component that
silently degrades.
"""

from __future__ import annotations

import importlib
import importlib.util
import re
import sys
from pathlib import Path
from types import ModuleType
from typing import Optional


class ModuleRefError(ValueError):
    """A configured module reference could not be resolved."""


_BARE_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def is_path_reference(value: str) -> bool:
    """Whether a reference is a file path rather than a built-in name."""
    return value.endswith(".py") or "/" in value or "\\" in value


def resolve_module_ref(
    value: Optional[str],
    *,
    registry: dict[str, str],
    kind: str,
    subject: str = "<unknown>",
    config_dir: Optional[Path] = None,
    error_cls: type[Exception] = ModuleRefError,
) -> Optional[ModuleType]:
    """Resolve a reference to a module, or None when there is none.

    `registry` maps a built-in name to an importable module path. `kind` and
    `subject` appear in every error - "funnel 'funnel_b_music_sound'", "source
    'melinda'" - because the one thing a startup failure must say is which piece
    of configuration is wrong.

    `config_dir` anchors relative paths: the directory of the config file, not
    the process working directory, so a config keeps working wherever the CLI is
    invoked from.
    """
    if not value:
        return None

    if not is_path_reference(value):
        if not _BARE_NAME.match(value):
            raise error_cls(
                f"{kind} {subject!r}: {value!r} is neither a path "
                f"(ending in .py) nor a valid built-in name"
            )
        target = registry.get(value)
        if target is None:
            raise error_cls(
                f"{kind} {subject!r}: unknown built-in {value!r}. "
                f"Available: {', '.join(sorted(registry))}"
            )
        return importlib.import_module(target)

    path = Path(value)
    if not path.is_absolute() and config_dir is not None:
        path = (config_dir / path).resolve()
    if not path.is_file():
        raise error_cls(f"{kind} {subject!r}: file not found: {path}")

    # A synthetic, unique module name, so two references to different files that
    # happen to share a basename do not collide in sys.modules.
    mod_name = f"reconciliator_{kind}_{subject}_{abs(hash(str(path))):x}"
    spec = importlib.util.spec_from_file_location(mod_name, path)
    if spec is None or spec.loader is None:
        raise error_cls(f"{kind} {subject!r}: cannot load module from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module
