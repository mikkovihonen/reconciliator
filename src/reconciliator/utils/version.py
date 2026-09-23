"""Package version resolution from pyproject.toml and package metadata."""

from __future__ import annotations

import tomllib
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path


def get_version() -> str:
    """Retrieve package version directly from pyproject.toml if accessible, or package metadata."""
    cur = Path(__file__).resolve()
    for parent in cur.parents:
        pyproject = parent / "pyproject.toml"
        if pyproject.is_file():
            try:
                with pyproject.open("rb") as f:
                    data = tomllib.load(f)
                    val = data.get("project", {}).get("version")
                    if isinstance(val, str):
                        return val
            except Exception:
                pass

    try:
        return version("reconciliator")
    except PackageNotFoundError, Exception:
        return "0.1.0"


__version__ = get_version()


def get_default_user_agent() -> str:
    """Return canonical User-Agent header string for Reconciliator HTTP clients."""
    return f"reconciliator/{get_version()}"
