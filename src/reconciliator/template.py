"""File-based Jinja2 text serialization engine for dense embeddings."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any, Optional

from jinja2 import Environment, FileSystemLoader, Template

from reconciliator.models import Agent, BibliographicRecord, ClassificationItem, SubjectItem


def join_names(agents: list[Agent | dict[str, Any]], delimiter: str = ", ") -> str:
    """Safe null-handling extractor for Agent names."""
    names: list[str] = []
    for a in agents:
        if isinstance(a, Agent):
            if a.name:
                names.append(a.name)
        elif isinstance(a, dict):
            name = a.get("name")
            if name:
                names.append(str(name))
    return delimiter.join(names)


def join_surnames(agents: list[Agent | dict[str, Any]], delimiter: str = ", ") -> str:
    """Extract agent surnames, falling back to name if surname is absent."""
    surnames: list[str] = []
    for a in agents:
        if isinstance(a, Agent):
            val = a.surname or a.name
            if val:
                surnames.append(val)
        elif isinstance(a, dict):
            dict_val = a.get("surname") or a.get("name")
            if dict_val:
                surnames.append(str(dict_val))
    return delimiter.join(surnames)


def join_labels(subjects: list[SubjectItem | dict[str, Any]], delimiter: str = ", ") -> str:
    """Safe null-handling extractor for SubjectItem pref_labels."""
    labels: list[str] = []
    for s in subjects:
        if isinstance(s, SubjectItem):
            if s.pref_label:
                labels.append(s.pref_label)
        elif isinstance(s, dict):
            lbl = s.get("pref_label")
            if lbl:
                labels.append(str(lbl))
    return delimiter.join(labels)


def primary_code(classifications: list[ClassificationItem | dict[str, Any]]) -> str:
    """Safe extractor for the code of the primary classification item."""
    for c in classifications:
        if isinstance(c, ClassificationItem):
            if c.is_primary:
                return c.code
        elif isinstance(c, dict):
            if c.get("is_primary"):
                return str(c.get("code", ""))
    return ""


def join_classifications(
    classifications: list[ClassificationItem | dict[str, Any]],
    delimiter: str = ", ",
    scheme: Optional[str] = None,
) -> str:
    """Join classification codes, optionally filtering by scheme."""
    codes: list[str] = []
    for c in classifications:
        if isinstance(c, ClassificationItem):
            if scheme is None or c.scheme.lower() == scheme.lower():
                codes.append(c.code)
        elif isinstance(c, dict):
            c_scheme = str(c.get("scheme", "")).lower()
            if scheme is None or c_scheme == scheme.lower():
                code = c.get("code")
                if code:
                    codes.append(str(code))
    return delimiter.join(codes)


def collapse_ws(text: str) -> str:
    """Collapse consecutive whitespace, tabs, and newlines into a single clean space."""
    if not text:
        return ""
    return re.sub(r"\s+", " ", text).strip()


class RecordTemplateEngine:
    """Loads and compiles Jinja2 templates strictly from filesystem paths for record serialization."""

    def __init__(self, template_path: Optional[Path | str] = None) -> None:
        if template_path is None:
            template_path = Path(__file__).parent / "templates" / "default_serialization.j2"
        self.template_path = Path(template_path)

        if not self.template_path.exists():
            pkg_default = Path(__file__).parent / "templates" / self.template_path.name
            if pkg_default.exists():
                self.template_path = pkg_default
            elif (Path(__file__).parent.parent / self.template_path).exists():
                self.template_path = Path(__file__).parent.parent / self.template_path
            else:
                raise FileNotFoundError(f"Template file not found at: {self.template_path}")

        self._template_content = self.template_path.read_text(encoding="utf-8")
        self.template_hash = hashlib.sha256(self._template_content.encode("utf-8")).hexdigest()

        # Jinja2 environment with FileSystemLoader
        self.env = Environment(
            loader=FileSystemLoader(str(self.template_path.parent)),
            autoescape=False,
            trim_blocks=True,
            lstrip_blocks=True,
        )

        # Register custom filters
        self.env.filters["join_names"] = join_names
        self.env.filters["join_surnames"] = join_surnames
        self.env.filters["join_labels"] = join_labels
        self.env.filters["primary_code"] = primary_code
        self.env.filters["join_classifications"] = join_classifications
        self.env.filters["collapse_ws"] = collapse_ws

        self._template: Template = self.env.get_template(self.template_path.name)

    def compute_template_hash(self) -> str:
        """Return the SHA-256 hash of the template file content."""
        return self.template_hash

    def render(self, record: BibliographicRecord | dict[str, Any]) -> str:
        """Render a BibliographicRecord into a serialized string."""
        if isinstance(record, BibliographicRecord):
            context = record.model_dump()
        else:
            context = dict(record)

        rendered = self._template.render(**context)
        return collapse_ws(rendered)
