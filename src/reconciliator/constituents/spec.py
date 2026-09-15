"""What a source's constituents look like, as configuration.

Two sources in the same format do not agree about constituents. Melinda writes
enhanced 505s; a sound-recording database keeps its tracks in its own
conventions, with durations; an EN 15907 filmography has episodes of a series;
plenty of sources have no aggregates at all, and for those, guessing is worse
than doing nothing.

So the fields to read, the shape they produce and the order they carry belong to
the *source*, and the code is one profile-driven extractor rather than a chain of
per-source special cases. See plans/aggregates.md.
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from reconciliator.models import ComponentType

#: How a constituent is embodied.
#:
#: `embodied` - an album track, an anthology story: a Work realised as an
#: Expression inside the *container's* manifestation, with no carrier of its own.
#: It gets a work row, an expression row and an ordered junction row.
#:
#: `component` - a journal article, a chapter with its own pagination: it has its
#: own extent, so it gets its own manifestation carrying `container_id`,
#: `is_component` and `component_type`.
ConstituentShape = Literal["embodied", "component"]

#: What ingest does with a source's constituents.
#:
#: `off` - do not look. `keys_only` - the historical behaviour: derive the
#: `analytic` and `analytic_title` blocking keys and no rows. `entities` - also
#: write the works, expressions, junction rows and `has_constituent` edges.
ConstituentMode = Literal["off", "keys_only", "entities"]


class ConstituentField(BaseModel):
    """One place a source records a constituent.

    A MARC entry names a `tag` and subfield codes; an RDF entry names a
    `predicate` and predicates. Exactly one of the two, because a profile that
    named both would be describing two sources at once.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    tag: Optional[str] = None
    predicate: Optional[str] = None

    #: Indicator values that must match for this entry to claim a field, e.g.
    #: `{ind1: "0", ind2: "0"}` for an *enhanced* 505 or `{ind2: "2"}` for an
    #: analytic added entry. An entry with no indicators claims every instance of
    #: the tag - which is what `extract_marc_analytics` does today, and why a
    #: basic 505's single `$a` blob would be read as if it were a title.
    indicators: dict[str, str] = Field(default_factory=dict)

    title: Optional[str] = None
    creator: Optional[str] = None
    duration: Optional[str] = None
    pagination: Optional[str] = None
    disc: Optional[str] = None
    role: Optional[str] = None

    #: `position` numbers constituents by the order they appear, which is what a
    #: tracklist means; `subfield:<code>` / `predicate:<name>` reads a stated
    #: number; `none` leaves them unordered.
    ordinal: str = "position"

    #: Overrides the profile's shape, so one source can yield embodied
    #: constituents from its contents note *and* component manifestations from
    #: its host-item records.
    shape: Optional[ConstituentShape] = None
    component_type: Optional[ComponentType] = None

    #: For a record that is itself a part: the subfield carrying its container's
    #: *record control number*, which is the only link in a host-item entry that
    #: identifies a row rather than describing one - MARC 773 `$w`.
    container_source_id: Optional[str] = None

    #: An entity key that identifies the container instead, for a source whose
    #: parts name their host some other way. Not used by `marc_enhanced`: the
    #: `citation` key is ISSN::year::page::author, which identifies the *article*
    #: and matches nothing on the host record.
    container_key: Optional[str] = None

    #: Whether the agent named beside the constituent is also a contributor to
    #: the *container*. False for a name-title analytic entry, where the agent
    #: wrote the constituent and not the volume - which is how an anthology's
    #: story authors came to be stored as its primary authors. A catalogue that
    #: means them as the volume's contributors can say true.
    contributes_to_container: bool = False

    #: Whether `creator` here is an authority-controlled name. A 7XX `$a` is; a
    #: contents note's `$r` is transcribed from the item. When one record
    #: describes a constituent twice, the controlled form is the one to keep,
    #: whichever entry otherwise takes precedence - "Rautavaara, Einojuhani"
    #: rather than "Rautavaara".
    creator_controlled: bool = False

    @model_validator(mode="after")
    def exactly_one_locator(self) -> ConstituentField:
        if bool(self.tag) == bool(self.predicate):
            raise ValueError(
                "a constituent field names exactly one of `tag` (MARC) or "
                f"`predicate` (RDF); got tag={self.tag!r} predicate={self.predicate!r}"
            )
        if not self.title:
            raise ValueError(
                f"constituent field {self.tag or self.predicate!r} names no `title`; "
                "a constituent with no title cannot become a work"
            )
        valid_ordinal = self.ordinal in ("position", "none") or self.ordinal.startswith(
            ("subfield:", "predicate:")
        )
        if not valid_ordinal:
            raise ValueError(
                f"ordinal {self.ordinal!r} must be 'position', 'none', "
                "'subfield:<code>' or 'predicate:<name>'"
            )
        return self


class ConstituentProfile(BaseModel):
    """One source's conventions for recording what a publication contains."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str

    #: Default for entries that state no shape of their own.
    shape: ConstituentShape = "embodied"

    #: `normalized_title` is why an album does not get two constituents for a
    #: track listed in both 505 `$t` and 700 `$t`; the richer entry - the one
    #: with a creator - wins, and entry order in `fields` decides ties. A source
    #: that legitimately repeats a title can say `none`.
    dedup: Literal["none", "normalized_title", "title_and_creator"] = "normalized_title"

    #: A real contents note can list a 500-track box set. One malformed record
    #: should not be able to write 500 works.
    max_per_record: int = 100

    fields: list[ConstituentField] = Field(default_factory=list)

    #: A built-in name or a path to a Python file, for a source whose
    #: constituents cannot be described declaratively. Same rule as a funnel's
    #: `handler`; see reconciliator.module_ref.
    extractor: Optional[str] = None

    def shape_of(self, field: ConstituentField) -> ConstituentShape:
        return field.shape or self.shape

    def fields_for_tag(self, tag: str) -> list[ConstituentField]:
        return [f for f in self.fields if f.tag == tag]

    @property
    def is_empty(self) -> bool:
        """Whether this profile would look for nothing at all."""
        return not self.fields and not self.extractor
