"""Declarative funnel specifications and their dispatch predicates.

The 42 domain funnels were 1564 lines of method bodies that differed only in a
handful of values: which identifier schemes to try first, which work type to
filter candidates by, which content type to align an expression on, and what to
call the resulting match tier. They are data now, and this module is its shape.
"""

from __future__ import annotations

import re
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


class FunnelWhen(BaseModel):
    """Dispatch predicate: which records a funnel claims.

    Every field that is set must hold, so a `when` with several fields is a
    conjunction and an empty `when` matches everything (which is what the
    fall-through funnel uses). The seven kinds between them reproduce the whole
    of the former 44-condition if-chain; the three `*_matches` kinds take regexes.
    """

    model_config = ConfigDict(extra="forbid")

    work_type: list[str] = Field(default_factory=list)
    # The expression's content type, which the MARC leader determines. This is
    # what separates notated music from a sound recording: both are work_type
    # "music", and every music record carries a `music_work` key, so keys cannot
    # tell them apart.
    content_type: list[str] = Field(default_factory=list)
    is_component: Optional[bool] = None
    has_key: list[str] = Field(default_factory=list)
    has_identifier: list[str] = Field(default_factory=list)
    title_matches: Optional[str] = None
    key_matches: dict[str, str] = Field(default_factory=dict)
    identifier_matches: dict[str, str] = Field(default_factory=dict)

    def matches(self, record: Any, identifiers: dict[str, list[str]]) -> bool:
        """Test this predicate against a record and its extracted identifiers."""
        if self.work_type and record.work_type not in self.work_type:
            return False
        if self.content_type and record.content_type not in self.content_type:
            return False
        if self.is_component is not None and bool(record.is_component) != self.is_component:
            return False
        if any(not record.get_keys(k) for k in self.has_key):
            return False
        if any(not identifiers.get(s.lower()) for s in self.has_identifier):
            return False
        if self.title_matches and not re.search(
            self.title_matches, record.title_stem or record.title or ""
        ):
            return False
        for key_type, pattern in self.key_matches.items():
            if not any(re.search(pattern, v) for v in record.get_keys(key_type)):
                return False
        for scheme, pattern in self.identifier_matches.items():
            if not any(re.search(pattern, v) for v in identifiers.get(scheme.lower(), [])):
                return False
        return True


class FunnelSpec(BaseModel):
    """One domain funnel, declared rather than written.

    The generic template runs tier 1 (exact identifiers), tier 2 (author plus
    title stem, filtered by work type), then tier 3 (vector or FTS fallback). A
    `handler` replaces whichever tiers it defines; everything it omits falls back
    to the template.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    # A list of alternatives: the funnel claims a record when ANY entry matches,
    # and each entry is itself a conjunction. Disjunctive normal form, because two
    # of the original dispatch conditions were genuine disjunctions ("a component
    # OR carrying a host citation OR typed as an article"). An empty list matches
    # everything, which is what the fall-through funnel uses.
    when: list[FunnelWhen] = Field(default_factory=list)

    # Tier 1: schemes tried before the rest of the record's identifiers.
    identifier_schemes: list[str] = Field(default_factory=list)

    # Tier 2: candidate work types, tried in order until one yields candidates.
    # Several funnels genuinely widen their net this way - funnel AA looks for a
    # "software" work and then an "ai_model" one - so this is a list, not a value.
    work_types: list[str] = Field(default_factory=list)
    match_tier: str = "tier2_domain_key"
    confidence: float = 0.90
    content_type: Optional[str] = None
    align_expression: bool = False
    # Stored relationships of the matched work, read from work_relationships.
    relationships: bool = False
    # A relationship this funnel asserts by matching at all: funnel MM matching a
    # critical edition to its base work *is* the claim "critical_edition_of". The
    # edge is minted from the match rather than looked up, since it did not exist
    # before the match was made.
    asserts_relationship: Optional[str] = None
    constituents: Literal["none", "titles", "resolve"] = "none"

    # Tier 3: work type filter for the vector/FTS fallback. Often the same as
    # `work_type`, but not always - some funnels widen the net at tier 3.
    fallback_work_type: Optional[str] = None

    # A built-in handler name, or a path to a .py file. See funnels/__init__.py.
    handler: Optional[str] = None

    # Optional extra configuration parameters for handlers
    extra: dict[str, Any] = Field(default_factory=dict)

    # Exactly one spec carries this; it claims whatever no earlier spec did.
    default: bool = False

    # Where a configured funnel sits in the order, named as the funnel it should
    # be tried BEFORE. Without it a new funnel lands just ahead of the
    # fall-through, which means it can only claim records nothing else wanted -
    # and the usual reason to add a funnel is to special-case records an existing
    # one currently handles. Ignored when overriding a built-in, which keeps its
    # own position.
    before: Optional[str] = None

    @field_validator("work_types", mode="before")
    @classmethod
    def _accept_single_work_type(cls, v: Any) -> Any:
        """Allow `work_types: text` as shorthand for `work_types: [text]`."""
        if v is None:
            return []
        if isinstance(v, str):
            return [v]
        return v

    @field_validator("when", mode="before")
    @classmethod
    def _accept_single_when(cls, v: Any) -> Any:
        """Allow `when: {...}` as shorthand for `when: [{...}]`."""
        if v is None:
            return []
        if isinstance(v, dict) or isinstance(v, FunnelWhen):
            return [v]
        return v

    def claims(self, record: Any, identifiers: dict[str, list[str]]) -> bool:
        """Whether this funnel claims the record. No alternatives means yes."""
        if not self.when:
            return True
        return any(w.matches(record, identifiers) for w in self.when)
