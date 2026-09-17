"""Time and duration parsing utilities."""

from __future__ import annotations


def parse_duration_seconds(duration_str: str) -> float:
    """Parse human-readable duration strings like '7d', '24h', '30m', '60s' into seconds."""
    s = duration_str.strip().lower()
    if not s:
        raise ValueError("Duration string cannot be empty")
    if s.endswith(("days", "day", "d")):
        val = s.rstrip("days").rstrip("d")
        return float(val) * 86400.0
    elif s.endswith(("hours", "hour", "hrs", "hr", "h")):
        val = s.rstrip("hours").rstrip("hrs").rstrip("h")
        return float(val) * 3600.0
    elif s.endswith(("minutes", "minute", "mins", "min", "m")):
        val = s.rstrip("minutes").rstrip("mins").rstrip("m")
        return float(val) * 60.0
    elif s.endswith(("seconds", "second", "secs", "sec", "s")):
        val = s.rstrip("seconds").rstrip("secs").rstrip("s")
        return float(val)
    else:
        # Default numeric strings without unit to days
        return float(s) * 86400.0

