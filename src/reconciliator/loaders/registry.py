"""The concrete loaders and the registry that dispatches to them.

Each wraps an existing decomposer rather than reimplementing it: the extraction
logic is format-specific and stays where it is. What is new is that all three now
satisfy one contract and declare what they produce.
"""

from __future__ import annotations

from typing import Any, ClassVar

from reconciliator.loaders.base import (
    DecomposedRecord,
    FormatAdapter,
    LoadContext,
    LoaderCapabilities,
    RecordLoader,
)

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
        )
        return DecomposedRecord(
            works=[w], expressions=[e], manifestations=[m], items=list(items),
            agents=list(agents), relationships=list(rels), identifiers=list(idents),
            associations=list(assoc), mediums=list(mops), contributions=list(contribs),
            entity_keys=keys,
        )


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

        works, exprs, manifs, items, agents, rels, idents, assoc, mops, contribs = (
            decompose_marc_authority(
                item,
                source=ctx.source,
                id_source=ctx.id_source,
                record_origin=ctx.origin,
                serialization_engine=ctx.serialization_engine,
                strip_isbd_punctuation=ctx.strip_isbd_punctuation,
                isbd_punctuation=ctx.isbd_punctuation,
            )
        )
        return DecomposedRecord(
            works=list(works), expressions=list(exprs), manifestations=list(manifs),
            items=list(items), agents=list(agents), relationships=list(rels),
            identifiers=list(idents), associations=list(assoc), mediums=list(mops),
            contributions=list(contribs),
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

        w, e, m, items, agents, rels, idents, assoc, mops, contribs = (
            decompose_bibframe_work(
                g=item,
                profile=ctx.profile,
                source=ctx.source,
                serialization_engine=ctx.serialization_engine,
                relator_mappings=ctx.relator_mappings,
                supported_subject_vocabularies=ctx.supported_subject_vocabularies,
                legacy_to_modern=ctx.legacy_to_modern,
                vocab_store=ctx.vocab_store,
            )
        )
        return DecomposedRecord(
            works=[w], expressions=[e], manifestations=[m], items=list(items),
            agents=list(agents), relationships=list(rels), identifiers=list(idents),
            associations=list(assoc), mediums=list(mops), contributions=list(contribs),
            entity_keys=bibframe_entity_keys(m, w, list(agents), list(idents)),
        )


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


def _is_xml_element(item: Any) -> bool:
    from lxml import etree

    return isinstance(item, etree._Element)


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
    work_types = (
        literals | set(SLM_WORK_TYPES.values()) | set(CONTENT_TYPE_WORK_TYPES.values())
    )

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

    return LoaderCapabilities(
        name=name,
        formats=["marcxml", "marc21"],
        work_types=work_types,
        content_types=set(RDA_CONTENT_TYPES.values()),
        key_types=key_types,
        identifier_schemes=(
            {
                "isbn", "issn", "issn_l", "ismn", "isrc", "ean", "doi", "urn",
                "other_ids", "fingerprint", "patent_number", "publisher_number",
                "plate_number", "strn", "control_number",
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
        key_types={"author", "title_stem", "work", "music_work", "standard"},
        identifier_schemes={"isbn", "issn", "urn", "doi", "other"},
        # Blank-node subjects now get an identifier derived from the node's own
        # triples, so the same input yields the same URIs on every ingest.
        deterministic_ids=True,
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
                "bound-with is a holdings-level relationship with no BIBFRAME "
                "equivalent"
            ),
            "translators_signature": (
                "no relator-based translator detection; roles are resolved "
                "through the profile instead"
            ),
        },
    )


MarcBibliographicLoader.capabilities = _marc_capabilities(
    "marc_bibliographic", keys=True, folds=True
)
MarcAuthorityLoader.capabilities = _marc_capabilities(
    "marc_authority", keys=False, folds=False
)
BibframeLoader.capabilities = _bibframe_capabilities()
