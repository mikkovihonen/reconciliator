"""Statistical and similarity metric utilities."""

from __future__ import annotations

import re


def compute_jaccard_overlap(set1: set[str], set2: set[str]) -> float:
    """Compute Jaccard similarity between two sets of string tokens/titles."""
    if not set1 or not set2:
        return 0.0
    norm1 = {re.sub(r"[^0-9a-zA-Z]", "", s).lower() for s in set1 if s}
    norm2 = {re.sub(r"[^0-9a-zA-Z]", "", s).lower() for s in set2 if s}
    norm1.discard("")
    norm2.discard("")
    if not norm1 or not norm2:
        return 0.0
    intersection = len(norm1 & norm2)
    union = len(norm1 | norm2)
    return intersection / union if union > 0 else 0.0
