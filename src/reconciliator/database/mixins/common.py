"""Shared helper functions for repository mixins."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any


def row_to_dict(row: Any) -> dict[str, Any]:
    """Convert a SQLAlchemy Row/RowMapping or dict to a dictionary with formatted dates."""
    if hasattr(row, "_mapping"):
        d = dict(row._mapping)
    elif isinstance(row, dict):
        d = dict(row)
    else:
        d = dict(row)
    for k, v in d.items():
        if isinstance(v, (datetime, date)):
            d[k] = v.isoformat()
    return d
