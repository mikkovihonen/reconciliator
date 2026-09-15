"""Constituent extraction profiles: bundled, per source, or a source's own file.

A source says how it records what a publication contains:

    constituents: marc_enhanced          # bundled, by name
    constituents: profiles/fono.yaml     # a profile of its own, config-relative
    constituents: off                    # nothing to look for - first-class

and separately what to do with what is found - `off`, `keys_only` (the
historical behaviour: blocking keys and no rows) or `entities`.

Selection mirrors `schema_profile`: a global default, a bundled profile by name,
a per-source override. Resolution happens when configuration loads, so an
unknown profile or a missing extractor file is a startup error naming the source.
"""

from __future__ import annotations

from pathlib import Path
from types import ModuleType
from typing import Any, NamedTuple, Optional

import yaml

from reconciliator.constituents.spec import (
    ConstituentField,
    ConstituentMode,
    ConstituentProfile,
    ConstituentShape,
)
from reconciliator.module_ref import ModuleRefError, is_path_reference, resolve_module_ref

__all__ = [
    "BUILTIN_EXTRACTORS",
    "ConstituentField",
    "ConstituentMode",
    "ConstituentProfile",
    "ConstituentProfileError",
    "ConstituentShape",
    "SourceConstituents",
    "available_profiles",
    "get_constituent_profile",
    "resolve_extractor",
    "resolve_for_source",
]

_BUILTIN_PROFILES_DIR = Path(__file__).parent.parent / "defaults" / "constituent_profiles"

#: Built-in extractor modules, by name. Importable paths rather than imported
#: modules so naming one does not cost an import when it is unused.
BUILTIN_EXTRACTORS: dict[str, str] = {}

#: Spellings that mean a bundled profile. `off`, `none` and `disabled` all name
#: the empty profile, because a config author reaching for "this source has no
#: constituents" should not have to guess which word the parser wants.
_ALIASES: dict[str, str] = {
    "off": "none",
    "none": "none",
    "disabled": "none",
    "no": "none",
    "marc_none": "none",
    "marc": "marc_enhanced",
    "marc_enhanced": "marc_enhanced",
    "marc21": "marc_enhanced",
    "marcxml": "marc_enhanced",
    "bibframe": "bibframe_haspart",
    "bibframe_haspart": "bibframe_haspart",
    "bffi": "bibframe_haspart",
    "bf": "bibframe_haspart",
}


class ConstituentProfileError(ValueError):
    """A constituent profile or its extractor could not be resolved."""


class SourceConstituents(NamedTuple):
    """What one source's configuration resolves to, once.

    `profile` is None only when the mode is `off`, so a caller that has a profile
    knows it is meant to use it.
    """

    mode: ConstituentMode
    profile: Optional[ConstituentProfile]
    extractor: Optional[ModuleType]

    @property
    def enabled(self) -> bool:
        return self.mode != "off" and self.profile is not None

    @property
    def writes_entities(self) -> bool:
        return self.mode == "entities" and self.profile is not None


def available_profiles() -> list[str]:
    """Bundled profile names, for error messages and `--help` text."""
    if not _BUILTIN_PROFILES_DIR.is_dir():
        return []
    return sorted(p.stem for p in _BUILTIN_PROFILES_DIR.glob("*.yaml"))


def get_constituent_profile(
    name_or_path: Optional[str] = None,
    *,
    config_dir: Optional[Path] = None,
    subject: str = "<unknown>",
) -> ConstituentProfile:
    """Load a profile by bundled name, alias, or path to a YAML file.

    The same three steps as `loaders.bibframe.profiles.get_profile`: an existing
    file wins, then the alias map, then a bundled name, then an error listing
    what is available. `config_dir` anchors a relative path to the config file's
    directory rather than the process working directory.
    """
    target = (name_or_path or "none").strip()

    if is_path_reference(target) or target.endswith((".yaml", ".yml")):
        path = Path(target)
        if not path.is_absolute() and config_dir is not None:
            path = (config_dir / path).resolve()
        if not path.is_file():
            raise ConstituentProfileError(
                f"source {subject!r}: constituent profile file not found: {path}"
            )
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return _validate(data, subject=subject, origin=str(path), default_name=path.stem)

    canonical = _ALIASES.get(target.lower().replace("-", "_"), target.lower())
    bundled = _BUILTIN_PROFILES_DIR / f"{canonical}.yaml"
    if bundled.is_file():
        data = yaml.safe_load(bundled.read_text(encoding="utf-8")) or {}
        return _validate(data, subject=subject, origin=str(bundled), default_name=canonical)

    raise ConstituentProfileError(
        f"source {subject!r}: unknown constituent profile {target!r}. "
        f"Available: {', '.join(available_profiles())}, "
        f"or a path to a YAML profile."
    )


def resolve_extractor(
    profile: ConstituentProfile,
    *,
    subject: str = "<unknown>",
    config_dir: Optional[Path] = None,
) -> Optional[ModuleType]:
    """Resolve a profile's `extractor`, by built-in name or path."""
    return resolve_module_ref(
        profile.extractor,
        registry=BUILTIN_EXTRACTORS,
        kind="constituent extractor for source",
        subject=subject,
        config_dir=config_dir,
        error_cls=ConstituentProfileError,
    )


def resolve_for_source(
    source_name: str,
    source_profile: Optional[str] = None,
    source_mode: Optional[str] = None,
    *,
    default_profile: Optional[str] = None,
    default_mode: ConstituentMode = "keys_only",
    config_dir: Optional[Path] = None,
) -> SourceConstituents:
    """Resolve one source's constituent configuration against the global default.

    A source that names nothing inherits the global default, and the shipped
    default profile is the empty one - so adding this feature changes nothing for
    a source nobody has described yet.
    """
    profile_ref = source_profile if source_profile is not None else default_profile
    mode: ConstituentMode = source_mode or default_mode  # type: ignore[assignment]

    # `constituents: off` is the natural way to write it, and it means the mode
    # rather than a profile named "off", so accept it in either field.
    if profile_ref is not None and profile_ref.strip().lower() in ("off", "disabled", "no"):
        mode = "off"

    if mode == "off":
        return SourceConstituents(mode="off", profile=None, extractor=None)

    profile = get_constituent_profile(
        profile_ref, config_dir=config_dir, subject=source_name
    )
    if profile.is_empty:
        # Nothing to look for, whatever the mode says. Reported as `off` so a
        # caller does not have to ask a second question.
        return SourceConstituents(mode="off", profile=None, extractor=None)
    extractor = resolve_extractor(profile, subject=source_name, config_dir=config_dir)
    return SourceConstituents(mode=mode, profile=profile, extractor=extractor)


def _validate(
    data: Any, *, subject: str, origin: str, default_name: str
) -> ConstituentProfile:
    if not isinstance(data, dict):
        raise ConstituentProfileError(
            f"source {subject!r}: constituent profile at {origin} is not a mapping"
        )
    data = dict(data)
    data.setdefault("name", default_name)
    try:
        return ConstituentProfile.model_validate(data)
    except ModuleRefError:
        raise
    except Exception as exc:  # pydantic validation, with the file named
        raise ConstituentProfileError(
            f"source {subject!r}: invalid constituent profile at {origin}: {exc}"
        ) from exc
