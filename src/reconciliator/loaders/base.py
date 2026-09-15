"""The contract every record loader satisfies.

Three formats reach the datastore through this interface. They do not extract
alike - MARC bibliographic, MARC authority and BIBFRAME read largely disjoint
field sets, and merging their extraction was measured and declined (see
plans/wemi_data_structure.md 14.9.1). What they must share is the *contract*: one
return shape, one options object, one set of post-decomposition guarantees, and
an explicit statement of what each format cannot yield.

That last part is the point. A loader that silently omits an output is
indistinguishable from one that cannot produce it, which is how the BibFrame path
came to apply none of the normalisation the MARC path does without anything
saying so. `LoaderCapabilities.not_produced` turns every such absence into a
declared boundary with a reason.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, ClassVar, Iterable, Optional

from pydantic import BaseModel, ConfigDict, Field

from reconciliator.models import (
    AgentEntity,
    ContributionEntity,
    EntityKey,
    ExpressionEntity,
    IdentifierEntity,
    ItemEntity,
    ManifestationEntity,
    ManifestationExpressionAssociation,
    MediumOfPerformance,
    WorkEntity,
    WorkRelationship,
)

DEFAULT_ISBD_PUNCTUATION = " ./,;:"

#: The fields whose stored value is a human-readable name, title or imprint
#: statement. These are the ones that carry ISBD presentation punctuation - a
#: trailing " /", "," or "." that belongs to the catalogue card rather than to
#: the data - whenever the record came from MARC, or from anything converted out
#: of MARC, which most BIBFRAME in circulation is.
#:
#: Deliberately excludes `*_normalized` (the normalisers already discard
#: punctuation) and the enumeration fields `volume_designation` and
#: `series_numbering`, which feed `check_volume_match` through
#: `normalize_volume`; those are left alone so this pass cannot move a matching
#: result.
ISBD_TEXT_FIELDS: dict[str, tuple[str, ...]] = {
    "works": ("title", "original_title"),
    "expressions": ("title",),
    "manifestations": (
        "title", "subtitle", "publisher", "publication_place",
        "edition_statement", "series_title", "host_title",
    ),
    "agents": ("name", "surname", "fuller_name"),
}

#: Same, for fields holding a list of such strings.
ISBD_LIST_FIELDS: dict[str, tuple[str, ...]] = {
    "agents": ("variant_names",),
}


def strip_isbd(
    value: Optional[str],
    enabled: bool = True,
    chars: str = DEFAULT_ISBD_PUNCTUATION,
) -> Optional[str]:
    """Strip trailing ISBD punctuation from a heading string.

    Single definition, because the authority path stripped it and the
    bibliographic path did not, so the same agent was stored as "Waltari, Mika"
    from one and "Waltari, Mika," from the other. Blocking keys already discarded
    punctuation, so this governs the name that is stored and exported rather than
    how records match.
    """
    if value is None:
        return None
    return value.rstrip(chars) if enabled else value


@dataclass
class DecomposedRecord:
    """Everything one source record becomes.

    Always lists, including the WEMI tiers. The MARC authority decomposer already
    returned lists while the other two returned singletons, so callers had to know
    which they were holding; and the ten-tuple this replaces was unpacked
    positionally at five sites, one of them counting from the end, which is why
    entity keys had to arrive through an out-parameter rather than an eleventh
    element.
    """

    works: list[WorkEntity] = field(default_factory=list)
    expressions: list[ExpressionEntity] = field(default_factory=list)
    manifestations: list[ManifestationEntity] = field(default_factory=list)
    items: list[ItemEntity] = field(default_factory=list)
    agents: list[AgentEntity] = field(default_factory=list)
    relationships: list[WorkRelationship] = field(default_factory=list)
    identifiers: list[IdentifierEntity] = field(default_factory=list)
    associations: list[ManifestationExpressionAssociation] = field(default_factory=list)
    mediums: list[MediumOfPerformance] = field(default_factory=list)
    contributions: list[ContributionEntity] = field(default_factory=list)
    entity_keys: list[EntityKey] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not (self.works or self.manifestations or self.agents)

    def extend(self, other: DecomposedRecord) -> None:
        """Accumulate another record's entities into this one."""
        for name in (
            "works", "expressions", "manifestations", "items", "agents",
            "relationships", "identifiers", "associations", "mediums",
            "contributions", "entity_keys",
        ):
            getattr(self, name).extend(getattr(other, name))


@dataclass
class LoadContext:
    """Everything a loader may need, handed to every loader alike.

    A loader that cannot honour part of this declares so in its capabilities
    rather than omitting the parameter. The BibFrame decomposer accepted neither
    `record_origin` nor any normalisation option, so those gaps were invisible at
    the call site.
    """

    source: str
    record_origin: Optional[str] = None
    id_source: str = "auto"
    include_items: bool = False
    serialization_engine: Optional[Any] = None

    # Normalisation, uniform across formats.
    strip_isbd_punctuation: bool = True
    isbd_punctuation: str = DEFAULT_ISBD_PUNCTUATION
    relator_mappings: Optional[dict[str, str]] = None
    vocab_store: Optional[Any] = None
    supported_subject_vocabularies: Optional[Iterable[str]] = None
    legacy_to_modern: Optional[dict[str, str]] = None
    profile: Optional[Any] = None

    @property
    def origin(self) -> str:
        """The record_origin to stamp: explicit value, else the source name."""
        return self.record_origin or self.source or "unknown"


class LoaderCapabilities(BaseModel):
    """What a loader does, and what it explicitly does not.

    Read by `tests/test_funnel_reachability.py` in place of parsing loader source
    for string literals and dictionary values - an inference that has twice
    reported the wrong answer.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    formats: list[str] = Field(default_factory=list)

    work_types: set[str] = Field(default_factory=set)
    content_types: set[str] = Field(default_factory=set)
    key_types: set[str] = Field(default_factory=set)
    identifier_schemes: set[str] = Field(default_factory=set)

    # One flag per LoadContext normalisation concern.
    deterministic_ids: bool = False
    stamps_record_origin: bool = False
    normalises_isbd: bool = False
    folds_subject_vocabularies: bool = False
    normalises_relators: bool = False

    # Every output this loader cannot produce, with the reason. An absence with
    # no entry here is a defect; an absence with one is a documented boundary.
    not_produced: dict[str, str] = Field(default_factory=dict)


class RecordLoader(ABC):
    """Base class for format loaders.

    Subclasses implement `accepts` and `_decompose`. `load` wraps the latter with
    the guarantees the datastore relies on, so they are applied once for every
    format rather than re-implemented per loader.
    """

    capabilities: ClassVar[LoaderCapabilities]

    @abstractmethod
    def accepts(self, item: Any) -> bool:
        """Whether this loader handles the given harvested item."""

    @abstractmethod
    def _decompose(self, item: Any, ctx: LoadContext) -> DecomposedRecord:
        """Format-specific extraction. Normalisation belongs in `load`."""

    def load(self, item: Any, ctx: LoadContext) -> DecomposedRecord:
        record = self._decompose(item, ctx)
        return self.finalise(record, ctx)

    # -- guarantees ------------------------------------------------------

    def finalise(self, record: DecomposedRecord, ctx: LoadContext) -> DecomposedRecord:
        """Apply the datastore contract to a decomposed record.

        Stamps `record_origin` on every tier. Previously the loaders each guessed
        an origin and `insert_wemi_batch(record_origin=...)` overwrote it
        afterwards, so a caller that inserted without that argument stored the
        wrong origin - and `record_origin` plus the `sources` registry is what
        decides which rows may be matched against.

        Strips trailing ISBD punctuation from every stored name and title. The
        toggle existed before this, but reached only the MARC decomposers, so
        `strip_isbd_punctuation=True` cleaned a Melinda heading and left the
        identical heading from a BIBFRAME export as "Waltari, Mika," - two rows
        for one agent in any report that groups by name. Doing it here rather
        than in each decomposer makes it a property of the interface: a loader
        cannot forget it, and a new format gets it without writing any code.
        """
        origin = ctx.origin
        for entities in (
            record.works,
            record.expressions,
            record.manifestations,
            record.items,
            record.agents,
        ):
            for entity in entities:
                entity.record_origin = origin  # type: ignore[assignment]

        if ctx.strip_isbd_punctuation:
            self._strip_isbd_fields(record, ctx.isbd_punctuation)
        return record

    @staticmethod
    def _strip_isbd_fields(record: DecomposedRecord, chars: str) -> None:
        """Strip trailing ISBD punctuation in place across the stored text fields.

        Idempotent, so the MARC decomposers going on stripping as they parse
        (where the cleaned title feeds key derivation) costs nothing here.

        A value that is punctuation all the way down is left as it was: `title`
        is a required `str` on three of the tiers, and replacing a bad title with
        an empty one loses the only evidence that the source record was odd.
        """
        for tier, fields in ISBD_TEXT_FIELDS.items():
            for entity in getattr(record, tier):
                for name in fields:
                    value = getattr(entity, name, None)
                    if not isinstance(value, str):
                        continue
                    stripped = value.rstrip(chars)
                    if stripped and stripped != value:
                        setattr(entity, name, stripped)

        for tier, fields in ISBD_LIST_FIELDS.items():
            for entity in getattr(record, tier):
                for name in fields:
                    values = getattr(entity, name, None)
                    if not isinstance(values, list):
                        continue
                    setattr(
                        entity,
                        name,
                        [
                            (v.rstrip(chars) or v) if isinstance(v, str) else v
                            for v in values
                        ],
                    )

    def check_output(
        self, record: DecomposedRecord, ctx: Optional[LoadContext] = None
    ) -> list[str]:
        """Compare what was produced against what this loader claims.

        Used by tests, not in the ingest path: a declaration nothing verifies
        drifts from the code the moment either changes.

        Pass the `ctx` the record was loaded with to check only the claims that
        context asked for. `normalises_isbd` means the loader honours
        `strip_isbd_punctuation`, not that it strips unconditionally, so with the
        toggle off the punctuation is supposed to survive.
        """
        caps = self.capabilities
        findings: list[str] = []

        produced_keys = {key.key_type for key in record.entity_keys}
        undeclared = produced_keys - caps.key_types
        if undeclared:
            findings.append(
                f"{caps.name} emitted undeclared key types: {sorted(undeclared)}"
            )
        contradicted = produced_keys & set(caps.not_produced)
        if contradicted:
            findings.append(
                f"{caps.name} declares it does not produce {sorted(contradicted)} "
                f"but emitted them"
            )

        produced_schemes = {i.scheme for i in record.identifiers}
        undeclared_schemes = produced_schemes - caps.identifier_schemes
        if undeclared_schemes:
            findings.append(
                f"{caps.name} emitted undeclared identifier schemes: "
                f"{sorted(undeclared_schemes)}"
            )

        if caps.deterministic_ids:
            # rdflib blank-node labels are regenerated per parse, so an id built
            # from one is not stable. This is how the BibFrame path's instability
            # went unnoticed.
            unstable = [
                w.work_uri
                for w in record.works
                if w.work_uri and _looks_like_blank_node(w.work_uri)
            ]
            if unstable:
                findings.append(
                    f"{caps.name} claims deterministic_ids but produced "
                    f"parse-local identifiers: {unstable[:3]}"
                )

        if caps.normalises_isbd and (ctx is None or ctx.strip_isbd_punctuation):
            chars = ctx.isbd_punctuation if ctx else DEFAULT_ISBD_PUNCTUATION
            dirty = [
                f"{tier}.{name}={value!r}"
                for tier, fields in ISBD_TEXT_FIELDS.items()
                for entity in getattr(record, tier)
                for name in fields
                if isinstance(value := getattr(entity, name, None), str)
                and value != value.rstrip(chars)
                and value.rstrip(chars)
            ]
            if dirty:
                findings.append(
                    f"{caps.name} claims normalises_isbd but stored headings "
                    f"still carrying ISBD punctuation: {dirty[:3]}"
                )

        if caps.normalises_relators:
            # Not "every role is a MARC code" - an unrecognised role legitimately
            # passes through as its own token - but "the shared normaliser has
            # nothing left to change". That distinguishes "kääntäjä", which maps
            # to trl, from a genuinely unknown role, which does not map at all.
            from reconciliator.normalizers.relators import normalize_role

            def unnormalised(role: Optional[str]) -> bool:
                # A blank-node label is never a role: a BIBFRAME role is usually
                # `bf:role [ rdfs:label "..." ]`, and reading the node instead of
                # its label put rdflib's parse label in the role column.
                return bool(role) and (
                    normalize_role(role) != role or _looks_like_blank_node(role)
                )

            raw_roles = sorted(
                {
                    role
                    for entity in record.agents
                    for role in (entity.role_counts or {})
                    if unnormalised(role)
                }
                | {c.role for c in record.contributions if unnormalised(c.role)}
            )
            if raw_roles:
                findings.append(
                    f"{caps.name} claims normalises_relators but stored roles the "
                    f"shared registry would still change: {raw_roles[:3]}"
                )

        if caps.stamps_record_origin:
            unstamped = [
                w.source_id
                for w in record.works
                if not w.record_origin or w.record_origin == "unknown"
            ]
            if unstamped:
                findings.append(
                    f"{caps.name} claims stamps_record_origin but left "
                    f"{unstamped[:3]} unstamped"
                )

        return findings


def _looks_like_blank_node(value: str) -> bool:
    """Heuristic for an rdflib blank-node label used as an identifier."""
    if value.startswith("_:"):
        return True
    # rdflib default: 'N' + 32 hex chars, lowercased by some paths.
    bare = value.split(":")[-1]
    return len(bare) >= 32 and bare[0] in "nN" and all(
        c in "0123456789abcdefABCDEF" for c in bare[1:33]
    )


class FormatAdapter(ABC):
    """Converts a foreign serialisation into something a loader accepts.

    Deliberately not a `RecordLoader`. `bibra.py` turns bibra JSON into MARCXML
    elements and hands them to the MARC loader; it performs no decomposition of
    its own, and pretending otherwise would put a fourth capability declaration
    on something that has no outputs to declare.
    """

    @abstractmethod
    def accepts(self, item: Any) -> bool: ...

    @abstractmethod
    def adapt(self, item: Any) -> Any: ...
