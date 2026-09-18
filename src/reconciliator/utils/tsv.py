"""TSV formatting, sanitization, and emission utilities adhering to strict TSV hygiene standards."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable


def sanitize_tsv_cell(val: Any) -> str:
    """Sanitize string for TSV cell, replacing tabs and newlines with spaces."""
    if val is None:
        return ""
    if isinstance(val, bool):
        return "true" if val else "false"
    if isinstance(val, float):
        return f"{val:.4f}"
    s = str(val)
    return s.replace("\t", " ").replace("\r", " ").replace("\n", " ").strip()


def write_tsv_file(file_path: Path, columns: list[str], rows: Iterable[Any]) -> None:
    """Write an iterable of dataclass rows to a TSV file with strict sanitization."""
    file_path.parent.mkdir(parents=True, exist_ok=True)
    with file_path.open("w", encoding="utf-8", newline="\n") as f:
        f.write("\t".join(columns) + "\n")
        for r in rows:
            vals = [sanitize_tsv_cell(getattr(r, col)) for col in columns]
            f.write("\t".join(vals) + "\n")


def write_summary_tsv(file_path: Path, columns: list[str], rows: Iterable[tuple[Any, ...]]) -> None:
    """Write tabular summary rows to a TSV file."""
    file_path.parent.mkdir(parents=True, exist_ok=True)
    with file_path.open("w", encoding="utf-8", newline="\n") as f:
        f.write("\t".join(columns) + "\n")
        for row in rows:
            f.write("\t".join(str(cell) for cell in row) + "\n")
