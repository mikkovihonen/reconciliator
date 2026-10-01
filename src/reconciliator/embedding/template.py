"""File-based Jinja2 text serialization engine for dense embeddings."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Optional

from jinja2 import Environment, FileSystemLoader, Template

from reconciliator.models import (
    Agent,
    AgentEntity,
    ClassificationItem,
    ExpressionEntity,
    ManifestationEntity,
    SubjectItem,
    WorkEntity,
)


def join_names(agents: Sequence[Agent | dict[str, Any]], delimiter: str = ", ") -> str:
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


def join_surnames(agents: Sequence[Agent | dict[str, Any]], delimiter: str = ", ") -> str:
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


def join_labels(subjects: Sequence[SubjectItem | dict[str, Any]], delimiter: str = ", ") -> str:
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


def primary_code(classifications: Sequence[ClassificationItem | dict[str, Any]]) -> str:
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
    classifications: Sequence[ClassificationItem | dict[str, Any]],
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

    def render(self, record: Any, **kwargs: Any) -> str:
        """Render a record (BibliographicRecord, entity model, or dict) into a serialized string."""
        if isinstance(record, dict):
            context = dict(record)
        elif hasattr(record, "__dict__"):
            context = dict(record.__dict__)
        elif hasattr(record, "model_dump"):
            context = record.model_dump()
        else:
            context = {}

        context.update(kwargs)
        rendered = self._template.render(**context)
        return collapse_ws(rendered)


class WEMISerializationEngine:
    """Manages compiled Jinja2 templates for the WEMI Triad:

    - Work (work_serialization.j2)
    - Expression (expression_serialization.j2)
    - Manifestation (manifestation_serialization.j2)
    """

    def __init__(
        self,
        work_template_path: Optional[Path | str] = None,
        expression_template_path: Optional[Path | str] = None,
        manifestation_template_path: Optional[Path | str] = None,
        agent_template_path: Optional[Path | str] = None,
    ) -> None:
        templates_dir = Path(__file__).parent / "templates"
        self.work_engine = RecordTemplateEngine(
            work_template_path or (templates_dir / "work_serialization.j2")
        )
        self.expression_engine = RecordTemplateEngine(
            expression_template_path or (templates_dir / "expression_serialization.j2")
        )
        self.manifestation_engine = RecordTemplateEngine(
            manifestation_template_path or (templates_dir / "manifestation_serialization.j2")
        )
        self.agent_engine = RecordTemplateEngine(
            agent_template_path or (templates_dir / "agent_serialization.j2")
        )

    def render_work(
        self,
        work: WorkEntity | dict[str, Any],
        authors: Optional[Sequence[AgentEntity | Agent | dict[str, Any] | str]] = None,
        subjects: Optional[Sequence[SubjectItem | dict[str, Any] | str]] = None,
        classifications: Optional[Sequence[ClassificationItem | dict[str, Any] | str]] = None,
        **kwargs: Any,
    ) -> str:
        """Render a conceptual Work entity into its dense embedding string."""
        if isinstance(work, WorkEntity):
            ctx = {
                "preferred_title": work.title,
                "uniform_title": getattr(work, "uniform_title", None),
                "original_title": getattr(work, "original_title", None),
                "work_type": work.work_type,
                "original_language": work.original_language or "",
                "subjects": subjects or [],
                "classifications": (
                    [{"code": work.classification, "scheme": work.classification_scheme}]
                    if work.classification
                    else []
                ),
            }
        else:
            ctx = dict(work)
            if "title" in ctx and "preferred_title" not in ctx:
                ctx["preferred_title"] = ctx["title"]

        if authors is not None:
            ctx["authors"] = [
                {
                    "name": (
                        a.name
                        if hasattr(a, "name")
                        else (a.get("name") if isinstance(a, dict) else str(a))
                    )
                }
                for a in authors
            ]
        elif "authors" not in ctx:
            ctx["authors"] = []

        if subjects is not None:
            ctx["subjects"] = subjects
        if "subjects" in ctx and ctx["subjects"]:
            ctx["subjects"] = [
                {
                    "pref_label": (
                        s.pref_label
                        if hasattr(s, "pref_label")
                        else (s.get("pref_label") if isinstance(s, dict) else str(s))
                    )
                }
                for s in ctx["subjects"]
            ]
        else:
            ctx["subjects"] = []

        if classifications is not None:
            ctx["classifications"] = [
                {
                    "code": (
                        c.code
                        if hasattr(c, "code")
                        else (c.get("code") if isinstance(c, dict) else str(c))
                    )
                }
                for c in classifications
            ]
        elif "classifications" not in ctx:
            ctx["classifications"] = []

        ctx.update(kwargs)
        return self.work_engine.render(ctx)

    def render_expression(
        self,
        expression: ExpressionEntity | dict[str, Any],
        work_title: Optional[str] = None,
        translators: Optional[Sequence[Any]] = None,
        performers: Optional[Sequence[Any]] = None,
        narrators: Optional[Sequence[Any]] = None,
        **kwargs: Any,
    ) -> str:
        """Render an Expression entity into its dense embedding string."""
        if isinstance(expression, ExpressionEntity):
            ctx: dict[str, Any] = {
                "work_title": work_title or expression.title or "",
                "expression_title": expression.title or work_title or "",
                "language": expression.language,
                "content_type": expression.content_type,
                "version_designation": expression.version_designation,
                "arrangement_note": expression.arrangement_note,
                "medium_of_performance": expression.medium_of_performance,
            }
        else:
            ctx = dict(expression)
            if work_title:
                ctx["work_title"] = work_title
            elif "work_title" not in ctx:
                ctx["work_title"] = ctx.get("expression_title") or ctx.get("title") or ""

        def _to_names(seq: Optional[Sequence[Any]]) -> list[dict[str, str]]:
            if not seq:
                return []
            return [
                {
                    "name": str(
                        x.name
                        if hasattr(x, "name")
                        else (x.get("name") if isinstance(x, dict) else str(x))
                    )
                }
                for x in seq
            ]

        if translators is not None:
            ctx["translators"] = _to_names(translators)
        elif "translators" in ctx and ctx["translators"]:
            ctx["translators"] = _to_names(ctx["translators"])

        if performers is not None:
            ctx["performers"] = _to_names(performers)
        elif "performers" in ctx and ctx["performers"]:
            ctx["performers"] = _to_names(ctx["performers"])

        if narrators is not None:
            ctx["narrators"] = _to_names(narrators)
        elif "narrators" in ctx and ctx["narrators"]:
            ctx["narrators"] = _to_names(ctx["narrators"])

        ctx.update(kwargs)
        return self.expression_engine.render(ctx)

    def render_manifestation(
        self,
        manifestation: ManifestationEntity | dict[str, Any],
        **kwargs: Any,
    ) -> str:
        """Render a Manifestation entity into its dense embedding string."""
        if isinstance(manifestation, ManifestationEntity):
            ctx = {
                "title": manifestation.title,
                "subtitle": manifestation.subtitle,
                "publisher": manifestation.publisher or "",
                "publication_place": manifestation.publication_place,
                "publication_year": manifestation.publication_year,
                "edition_statement": manifestation.edition_statement,
                "carrier_type": manifestation.carrier_type,
                "extent": manifestation.extent,
                "series_title": manifestation.series_title,
                "series_numbering": manifestation.series_numbering,
            }
        else:
            ctx = dict(manifestation)

        ctx.update(kwargs)
        return self.manifestation_engine.render(ctx)

    def render_agent(
        self,
        agent: AgentEntity | Agent | dict[str, Any],
        **kwargs: Any,
    ) -> str:
        """Render an Agent entity into its dense embedding string."""
        if hasattr(agent, "model_dump"):
            ctx = agent.model_dump()
        elif isinstance(agent, dict):
            ctx = dict(agent)
        else:
            ctx = {}
        ctx.update(kwargs)
        return self.agent_engine.render(ctx)

    def render_match_record_work(self, record: Any) -> str:
        """Render a MatchRecord into its Work-level dense embedding string."""
        subjects = [{"pref_label": s} for s in (getattr(record, "subjects", None) or [])]
        authors = [{"name": record.author_key}] if getattr(record, "author_key", None) else []
        return self.render_work(
            work={
                "preferred_title": (
                    getattr(record, "uniform_title", None)
                    or getattr(record, "title", None)
                    or getattr(record, "record_id", "Unknown")
                ),
                "uniform_title": getattr(record, "uniform_title", None),
                "work_type": getattr(record, "work_type", "text"),
                "original_language": getattr(record, "language", "") or "",
            },
            authors=authors,
            subjects=subjects,
        )

    def render_match_record_expression(
        self, record: Any, work_title: Optional[str] = None
    ) -> str:
        """Render a MatchRecord into its Expression-level dense embedding string."""
        keys = getattr(record, "keys", {}) or {}
        trans = (
            [{"name": keys["translators_signature"][0]}]
            if "translators_signature" in keys
            else []
        )
        return self.render_expression(
            expression={
                "work_title": (
                    work_title
                    or getattr(record, "uniform_title", None)
                    or getattr(record, "title", "")
                ),
                "expression_title": getattr(record, "title", ""),
                "language": getattr(record, "language", "und"),
                "content_type": getattr(record, "work_type", "text"),
                "version_designation": getattr(record, "volume_designation", None),
            },
            translators=trans,
        )
