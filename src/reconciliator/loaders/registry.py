"""The concrete loaders and the registry that dispatches to them.

Each wraps an existing decomposer rather than reimplementing it: the extraction
logic is format-specific and stays where it is. What is new is that all three now
satisfy one contract and declare what they produce.
"""

from __future__ import annotations

import logging
from typing import Any, ClassVar, Optional

from reconciliator.loaders.base import (
    DecomposedRecord,
    FormatAdapter,
    LoadContext,
    LoaderCapabilities,
    RecordLoader,
)

logger = logging.getLogger(__name__)

# --- MARC bibliographic ---------------------------------------------------


class MarcBibliographicLoader(RecordLoader):
    """MARC 21 Bibliographic, the best-served path."""

    capabilities: ClassVar[LoaderCapabilities]

    def accepts(self, item: Any) -> bool:
        from reconciliator.loaders.marcxml import is_marc_authority_record

        return _is_xml_element(item) and not is_marc_authority_record(item)

    def _decompose(self, item: Any, ctx: LoadContext) -> DecomposedRecord:
        from reconciliator.loaders.marcxml import decompose_marc_wemi

        keys: list[Any] = []
        subjects: list[Any] = []
        w, e, m, items, agents, rels, idents, assoc, mops, contribs = decompose_marc_wemi(
            item,
            source=ctx.source,
            id_source=ctx.id_source,
            include_items=ctx.include_items,
            record_origin=ctx.origin,
            serialization_engine=ctx.serialization_engine,
            supported_subject_vocabularies=ctx.supported_subject_vocabularies,
            legacy_to_modern=ctx.legacy_to_modern,
            relator_mappings=ctx.relator_mappings,
            vocab_store=ctx.vocab_store,
            strip_isbd_punctuation=ctx.strip_isbd_punctuation,
            isbd_punctuation=ctx.isbd_punctuation,
            key_sink=keys,
            subject_sink=subjects,
        )
        record = DecomposedRecord(
            works=[w],
            expressions=[e],
            manifestations=[m],
            items=list(items),
            agents=list(agents),
            relationships=list(rels),
            identifiers=list(idents),
            associations=list(assoc),
            mediums=list(mops),
            contributions=list(contribs),
            entity_keys=keys,
            entity_subjects=subjects,
        )
        _add_constituents(record, item, ctx, w, e, m, keys=None)
        return record


# --- MARC authority -------------------------------------------------------


class MarcAuthorityLoader(RecordLoader):
    """MARC 21 Authority (Leader/06 = 'z').

    A separate implementation rather than a branch inside the bibliographic one:
    the two read 4 shared tags out of 36 and 12, so merging them concatenates two
    disjoint bodies. See plans/wemi_data_structure.md 14.9.1.
    """

    capabilities: ClassVar[LoaderCapabilities]

    def accepts(self, item: Any) -> bool:
        from reconciliator.loaders.marcxml import is_marc_authority_record

        return _is_xml_element(item) and bool(is_marc_authority_record(item))

    def _decompose(self, item: Any, ctx: LoadContext) -> DecomposedRecord:
        from reconciliator.loaders.marcxml import decompose_marc_authority

        subjects: list[Any] = []
        works, exprs, manifs, items, agents, rels, idents, assoc, mops, contribs = (
            decompose_marc_authority(
                item,
                source=ctx.source,
                id_source=ctx.id_source,
                record_origin=ctx.origin,
                serialization_engine=ctx.serialization_engine,
                strip_isbd_punctuation=ctx.strip_isbd_punctuation,
                isbd_punctuation=ctx.isbd_punctuation,
                subject_sink=subjects,
            )
        )
        return DecomposedRecord(
            works=list(works),
            expressions=list(exprs),
            manifestations=list(manifs),
            items=list(items),
            agents=list(agents),
            relationships=list(rels),
            identifiers=list(idents),
            associations=list(assoc),
            mediums=list(mops),
            contributions=list(contribs),
            entity_subjects=subjects,
        )


# --- BIBFRAME -------------------------------------------------------------


class BibframeLoader(RecordLoader):
    """BIBFRAME RDF, via rdflib."""

    capabilities: ClassVar[LoaderCapabilities]

    def accepts(self, item: Any) -> bool:
        from rdflib import Graph

        return isinstance(item, Graph)

    def _decompose(self, item: Any, ctx: LoadContext) -> DecomposedRecord:
        from reconciliator.loaders.bibframe import (
            bibframe_entity_keys,
            decompose_bibframe_work,
        )

        constituents: list[Any] = []
        subjects: list[Any] = []
        w, e, m, items, agents, rels, idents, assoc, mops, contribs = decompose_bibframe_work(
            g=item,
            profile=ctx.profile,
            source=ctx.source,
            serialization_engine=ctx.serialization_engine,
            relator_mappings=ctx.relator_mappings,
            supported_subject_vocabularies=ctx.supported_subject_vocabularies,
            legacy_to_modern=ctx.legacy_to_modern,
            vocab_store=ctx.vocab_store,
            constituent_profile=ctx.constituent_profile,
            constituent_sink=constituents,
            subject_sink=subjects,
        )
        record = DecomposedRecord(
            works=[w],
            expressions=[e],
            manifestations=[m],
            items=list(items),
            agents=list(agents),
            relationships=list(rels),
            identifiers=list(idents),
            associations=list(assoc),
            mediums=list(mops),
            contributions=list(contribs),
            entity_keys=bibframe_entity_keys(m, w, list(agents), list(idents)),
            entity_subjects=subjects,
        )
        _add_constituents(record, item, ctx, w, e, m, constituents)
        return record


# --- bibra: an adapter, not a loader -------------------------------------


class BibraAdapter(FormatAdapter):
    """Turns a bibra JSON record into a MARCXML element for the MARC loader."""

    def accepts(self, item: Any) -> bool:
        return isinstance(item, dict)

    def adapt(self, item: Any) -> Any:
        from reconciliator.loaders.bibra import bibra_to_marcxml_elem

        return bibra_to_marcxml_elem(item)


# --- registry -------------------------------------------------------------

#: Order matters: the authority test is narrower than the bibliographic one.
LOADERS: list[RecordLoader] = [
    MarcAuthorityLoader(),
    MarcBibliographicLoader(),
    BibframeLoader(),
]

ADAPTERS: list[FormatAdapter] = [BibraAdapter()]


def select_loader(item: Any) -> RecordLoader:
    """The first loader that accepts the item."""
    for loader in LOADERS:
        if loader.accepts(item):
            return loader
    raise ValueError(f"no loader accepts an item of type {type(item).__name__}")


def load_record(item: Any, ctx: LoadContext) -> DecomposedRecord:
    """Dispatch one harvested item to its loader, adapting first if needed."""
    for adapter in ADAPTERS:
        if adapter.accepts(item):
            item = adapter.adapt(item)
            break
    return select_loader(item).load(item, ctx)


def _extend_relationships(record: DecomposedRecord, incoming: list[Any]) -> None:
    """Append edges the record does not already state."""

    def key(rel: Any) -> tuple[Any, Any, Any]:
        return (
            rel.relationship_type,
            rel.source_work_uri or rel.source_work_id,
            rel.target_work_uri or rel.target_work_id,
        )

    seen = {key(r) for r in record.relationships}
    for rel in incoming:
        if key(rel) not in seen:
            seen.add(key(rel))
            record.relationships.append(rel)


def _container_creator(item: Any, ctx: LoadContext) -> Any:
    """The container's main entry, for a constituent that names no creator.

    MARC has one: a 1XX heading. BIBFRAME has no equivalent notion of a main
    entry, and a part there carries its own URI as identity anyway, so nothing is
    inherited.
    """
    if not _is_xml_element(item):
        return None
    from reconciliator.loaders.marcxml import marc_main_entry_name

    return marc_main_entry_name(item)


def _is_xml_element(item: Any) -> bool:
    from lxml import etree

    return isinstance(item, etree._Element)


def _add_constituents(
    record: DecomposedRecord,
    item: Any,
    ctx: LoadContext,
    work: Any,
    expression: Any,
    manifestation: Any,
    extracted: Optional[list[Any]] = None,
    keys: Any = None,
) -> None:
    """Append the constituent rows this source's profile asks for.

    Here rather than inside `decompose_marc_wemi` because a record's constituents
    are *more works*, and that function returns one of each tier; the loader
    holds a `DecomposedRecord`, which has held lists since the loader contract
    landed. The blocking keys are derived either way, as they always were - this
    only adds rows, and only when the source asked for them.
    """
    profile = ctx.constituent_profile
    if profile is None or not ctx.writes_constituent_entities:
        return

    from reconciliator.loaders.aggregates import (
        apply_component_containment,
        build_embodied_constituents,
        reassign_constituent_contributions,
    )

    if extracted is None:
        # MARC reads them here: the element is in hand and nothing about the
        # extraction needs anything the decomposer resolved.
        from reconciliator.loaders.marcxml import extract_marc_constituents

        extracted = extract_marc_constituents(
            item, profile, record_id=f"{ctx.source}/{work.source_id}"
        )
    constituents = extracted
    if not constituents:
        return

    # A record that is itself a part: the host-item entry describes its
    # container, so this is a fact about the manifestation already in hand rather
    # than more rows.
    apply_component_containment(constituents, manifestation=manifestation, source=ctx.source)

    main_entry = _container_creator(item, ctx)
    rows = build_embodied_constituents(
        constituents,
        container_work=work,
        container_manifestation=manifestation,
        source=ctx.source,
        record_origin=ctx.origin,
        language=expression.language or "und",
        work_type=work.work_type,
        content_type=expression.content_type,
        container_creator=main_entry,
        serialization_engine=ctx.serialization_engine,
    )
    record.works.extend(rows.works)
    record.expressions.extend(rows.expressions)
    record.associations.extend(rows.associations)
    # A format may already have stated the containment itself - BIBFRAME's
    # `bf:hasPart` is both where the parts are found and a relationship the
    # decomposer reads - so the same edge can arrive twice. The unique constraint
    # would absorb it, but an edge list that says a thing twice is a worse
    # description of the record.
    _extend_relationships(record, rows.relationships)

    # Only once the constituent works exist: in `keys_only` there is nowhere for
    # the contribution to go, and dropping it would lose the agent link.
    moved = reassign_constituent_contributions(
        agents=record.agents,
        contributions=record.contributions,
        constituents=constituents,
        rows=rows,
        container_work_uri=work.work_uri,
        protected_name=main_entry,
    )
    if moved:
        logger.debug(
            "Moved %d contribution(s) from %s to their own constituent works.",
            moved,
            work.work_uri,
        )


# --- capability declarations ----------------------------------------------
#
# Derived from what each loader actually does, and verified by
# `RecordLoader.check_output` plus tests/test_loader_contract.py. Every absence
# carries a reason, so a reviewer can tell a decision from an oversight.


def _marc_capabilities(name: str, *, keys: bool, folds: bool) -> LoaderCapabilities:
    import ast
    import inspect

    from reconciliator.loaders.marcxml import (
        CONTENT_TYPE_WORK_TYPES,
        RDA_CONTENT_TYPES,
        SLM_WORK_TYPES,
        derive_domain_keys,
        determine_work_type,
    )
    from reconciliator.vocabularies import get_vocabulary_patterns

    # Read from the functions rather than transcribed, so a declaration cannot
    # drift from the code it describes.
    tree = ast.parse(inspect.getsource(determine_work_type))
    literals = {
        n.value.value
        for n in ast.walk(tree)
        if isinstance(n, ast.Return)
        and isinstance(n.value, ast.Constant)
        and isinstance(n.value.value, str)
    }
    work_types = literals | set(SLM_WORK_TYPES.values()) | set(CONTENT_TYPE_WORK_TYPES.values())

    key_types: set[str] = set()
    if keys:
        key_tree = ast.parse(inspect.getsource(derive_domain_keys))
        key_types = {
            n.args[0].value
            for n in ast.walk(key_tree)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Name)
            and n.func.id == "_add"
            and n.args
            and isinstance(n.args[0], ast.Constant)
        }

    not_produced: dict[str, str] = {}
    if not keys:
        not_produced["entity_keys"] = (
            "the authority decomposer derives no blocking keys: an authority "
            "record describes a heading, not a bibliographic resource, so the "
            "work/author/title-stem keys the clusterer blocks on have no subject"
        )
    if not folds:
        not_produced["subject_vocabulary_folding"] = (
            "takes no vocab_store: authority records carry their own headings "
            "rather than subject terms needing folding to a modern ontology"
        )
        not_produced["relator_normalisation"] = (
            "takes no relator_mappings; authority headings carry no relator codes"
        )
        not_produced["constituents"] = (
            "an authority record describes a work, not a publication with contents"
        )

    return LoaderCapabilities(
        name=name,
        formats=["marcxml", "marc21"],
        work_types=work_types,
        content_types=set(RDA_CONTENT_TYPES.values()),
        key_types=key_types,
        identifier_schemes=(
            {
                "isbn",
                "issn",
                "issn_l",
                "ismn",
                "isrc",
                "ean",
                "doi",
                "urn",
                "other_ids",
                "fingerprint",
                "patent_number",
                "publisher_number",
                "plate_number",
                "strn",
                "control_number",
            }
            # Authority URIs carried on agent $0/$1 become identifiers whose
            # scheme is whichever vocabulary `detect_vocabulary` recognises, plus
            # the two fallbacks the loader uses when it recognises none. Read from
            # the vocabulary patterns rather than listed, because the set is
            # configurable - and because listing it by hand is what
            # `check_output` caught as incomplete.
            | {v.lower() for v in get_vocabulary_patterns()}
            | {"wikidata", "uri"}
        ),
        deterministic_ids=True,
        stamps_record_origin=True,
        # Only the bibliographic path: an authority record describes a work, not
        # a publication with contents.
        emits_constituents=keys,
        normalises_isbd=True,
        folds_subject_vocabularies=folds,
        normalises_relators=folds,
        not_produced=not_produced,
    )


def _bibframe_capabilities() -> LoaderCapabilities:
    from reconciliator.loaders.bibframe.decomposition import (
        BIBFRAME_CONTENT_TYPES,
        BIBFRAME_CONTENT_WORK_TYPES,
        BIBFRAME_WORK_TYPES,
    )

    return LoaderCapabilities(
        name="bibframe",
        formats=["bibframe", "bibframe_rdf", "bibframe_jsonld"],
        work_types=(
            set(BIBFRAME_WORK_TYPES.values())
            | set(BIBFRAME_CONTENT_WORK_TYPES.values())
            | {"text", "hub", "music"}
        ),
        content_types=set(BIBFRAME_CONTENT_TYPES.values()) | {"text", "audio_music"},
        key_types={
            "author",
            "title_stem",
            "work",
            "music_work",
            "standard",
            "translators_signature",
        },
        identifier_schemes={"isbn", "issn", "urn", "doi", "other"},
        # Blank-node subjects now get an identifier derived from the node's own
        # triples, so the same input yields the same URIs on every ingest.
        deterministic_ids=True,
        # `bf:hasPart` on a Work, through a predicate-carrying profile. A part
        # keeps its own node URI as its identity; only a blank-node part falls
        # back to the content digest.
        emits_constituents=True,
        stamps_record_origin=True,
        # The decomposer still takes no strip_isbd_punctuation; `finalise` applies
        # it to the record afterwards, which is the whole reason the normalisation
        # moved into the base class. BIBFRAME converted out of MARC - most of what
        # circulates - carries the punctuation in its literals.
        normalises_isbd=True,
        # Subjects are read from bf:subject and folded through the same helper the
        # MARC path uses, with ctx's supported vocabularies, legacy mapping and
        # vocab_store all reaching it.
        folds_subject_vocabularies=True,
        # The profile still resolves roles first, so a deployment can override
        # any of them; what it does not resolve now goes to the same relator
        # registry the MARC path uses, and ctx.relator_mappings reaches it.
        normalises_relators=True,
        not_produced={
            "citation": "BIBFRAME has no host-item concept equivalent to MARC 773",
            "analytic": (
                "constituent works are modelled as related Works rather than as "
                "505/700 $t subfields, so no analytic keys are derived"
            ),
            "sammelband": (
                "bound-with is a holdings-level relationship with no BIBFRAME equivalent"
            ),
        },
    )


MarcBibliographicLoader.capabilities = _marc_capabilities(
    "marc_bibliographic", keys=True, folds=True
)
MarcAuthorityLoader.capabilities = _marc_capabilities("marc_authority", keys=False, folds=False)
BibframeLoader.capabilities = _bibframe_capabilities()
