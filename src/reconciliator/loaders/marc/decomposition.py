"""WEMI entity decomposition and MatchRecord extraction from MARCXML."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from lxml import etree
from reconciliator._core.parsers import MarcRecordData

from reconciliator.config import get_category_default_field_map, get_default_carrier_source
from reconciliator.embedding.template import WEMISerializationEngine
from reconciliator.loaders.base import DEFAULT_ISBD_PUNCTUATION, strip_isbd
from reconciliator.loaders.marc.classification import (
    determine_carrier_type,
    determine_content_type,
    determine_work_type,
)
from reconciliator.loaders.marc.parsing import parse_marc_items, parse_marc_record_elem
from reconciliator.loaders.marc.xml_utils import (
    _get_datafields,
    _get_subfield,
    derive_agent_id,
    derive_content_id,
)
from reconciliator.models import (
    AgentEntity,
    ContributionEntity,
    EntityKey,
    EntitySubject,
    ExpressionEntity,
    IdentifierEntity,
    ItemEntity,
    ManifestationEntity,
    ManifestationExpressionAssociation,
    MatchRecord,
    RecordOrigin,
    WorkEntity,
    WorkRelationship,
)
from reconciliator.normalizers.agent import parse_agent_dates
from reconciliator.normalizers.blocking import (
    compute_author_key,
    compute_title_stem,
)
from reconciliator.normalizers.keys import (
    derive_domain_keys,
    to_manifestation_entity_keys,
)
from reconciliator.vocab import extract_concept_id

logger = logging.getLogger(__name__)


@dataclass
class DecomposedWEMIResult:
    """Decomposed core WEMI hierarchy and associated relational entities from a bibliographic record."""

    work: WorkEntity
    expression: ExpressionEntity
    manifestation: ManifestationEntity
    items: list[ItemEntity] = field(default_factory=list)
    agents: list[AgentEntity] = field(default_factory=list)
    relationships: list[WorkRelationship] = field(default_factory=list)
    identifiers: list[IdentifierEntity] = field(default_factory=list)
    associations: list[ManifestationExpressionAssociation] = field(default_factory=list)
    contributions: list[ContributionEntity] = field(default_factory=list)
    keys: list[EntityKey] = field(default_factory=list)
    subjects: list[EntitySubject] = field(default_factory=list)


def decompose_marc_wemi(
    record_elem: etree._Element,
    source: Optional[str] = None,
    id_source: str = "marc_001",
    include_items: bool = False,
    record_origin: Optional[RecordOrigin | str] = None,
    serialization_engine: Optional[WEMISerializationEngine] = None,
    supported_subject_vocabularies: Optional[Iterable[str]] = None,
    work_authority_sources: Optional[Iterable[str]] = None,
    legacy_to_modern: Optional[dict[str, str]] = None,
    vocab_store: Optional[Any] = None,
    strip_isbd_punctuation: bool = True,
    isbd_punctuation: str = DEFAULT_ISBD_PUNCTUATION,
    config: Optional[Any] = None,
) -> DecomposedWEMIResult:
    """Decompose a single MARCXML record into the WEMI entity core hierarchy."""
    if source is None:
        source = get_default_carrier_source(config)

    # record_origin names the ingest source since Phase 13. An explicit value
    # wins; otherwise the source name is the origin.
    effective_origin: RecordOrigin = record_origin or source or "unknown"
    parsed = parse_marc_record_elem(
        record_elem,
        source=source,
        id_source=id_source,
        supported_subject_vocabularies=supported_subject_vocabularies,
        work_authority_sources=work_authority_sources,
        legacy_to_modern=legacy_to_modern,
        vocab_store=vocab_store,
        strip_isbd_punctuation=strip_isbd_punctuation,
        isbd_punctuation=isbd_punctuation,
    )
    source_id = parsed["source_id"]
    if not source_id or source_id == "unknown_id":
        source_id = derive_content_id(record_elem)
        logger.warning(
            "MARCXML record in source '%s' carries no control number; derived '%s' from its content",
            source,
            source_id,
        )

    engine = serialization_engine or WEMISerializationEngine()

    keys: list[EntityKey] = []
    subjects: list[EntitySubject] = []

    # Check 758 Work / Instance links if present
    work_uri = None
    for f in _get_datafields(record_elem, "758"):
        w_sub0 = _get_subfield(f, "0") or _get_subfield(f, "1")
        if w_sub0:
            work_uri = w_sub0
            break
    if not work_uri:
        work_uri = f"urn:work:{source}:{source_id}"

    work_type = determine_work_type(record_elem, config=config)
    content_type = determine_content_type(record_elem, work_type=work_type, config=config)
    carrier_type = determine_carrier_type(record_elem, config=config)

    # Variant titles from 246 (Varying Form of Title) and 740 (Uncontrolled
    # Related/Analytical Title) are routed to subjects so they're
    # searchable via BM25 and the dense work index — the same mechanism
    # MarcAuthorityLoader uses for 4XX tracing entries.
    for vt in parsed.get("variant_titles") or []:
        if vt:
            subjects.append(
                EntitySubject(
                    entity_type="work",
                    entity_id=0,
                    source=source,
                    source_record_id=source_id,
                    relation_type="variant_title",
                    raw_label=vt,
                )
            )

    # Primary classification
    classification = None
    classification_scheme = None
    for c in parsed["classifications"]:
        if isinstance(c, tuple) and len(c) >= 2:
            classification, classification_scheme = c[0], c[1]
            break
        elif isinstance(c, dict):
            classification = c.get("code")
            classification_scheme = c.get("scheme")
            if c.get("is_primary"):
                break
            if classification:
                break

    # 1. Work Entity
    preferred_title = parsed["uniform_title"] or parsed["title"]
    orig_lang = parsed.get("original_language") or parsed.get("language")
    work = WorkEntity(
        work_uri=work_uri,
        source=source,
        source_id=source_id,
        record_origin=effective_origin,
        title=preferred_title,
        # Must use the same stem normalization as the BIBFRAME and Wikidata
        # decomposers, otherwise stem-based work lookup cannot find MARC records.
        title_normalized=compute_title_stem(preferred_title, lang=orig_lang),
        original_title=parsed.get("original_title"),
        original_language=orig_lang,
        work_type=work_type,
        classification=classification,
        classification_scheme=classification_scheme,
    )

    # 2. Expression Entity
    lang = parsed.get("language") or "und"
    expression_uri = f"urn:exp:{source}:{source_id}:{lang}"
    expression = ExpressionEntity(
        work_id=0,  # bound by insert_wemi_batch, from work_uri
        work_uri=work.work_uri,
        expression_uri=expression_uri,
        source=source,
        source_id=source_id,
        record_origin=effective_origin,
        language=lang,
        content_type=content_type,
        title=parsed["title"],
        version_designation=parsed.get("edition"),
    )

    # 3. Manifestation Entity
    pub_year = None
    py_raw = parsed.get("publication_year") or ""
    if py_raw.isdigit():
        pub_year = int(py_raw)

    is_component = bool(_get_datafields(record_elem, "773"))
    manifestation_uri = f"urn:manif:{source}:{source_id}"
    manifestation = ManifestationEntity(
        work_uri=work.work_uri,
        work_id=0,  # Assigned upon DB insertion
        manifestation_uri=manifestation_uri,
        source=source,
        source_id=source_id,
        record_origin=effective_origin,
        title=parsed["title"],
        title_normalized=compute_title_stem(parsed["title"], lang=parsed.get("language")),
        subtitle=parsed.get("subtitle"),
        publication_year=pub_year,
        publisher=parsed.get("publisher"),
        publication_place=parsed.get("publication_place"),
        edition_statement=parsed.get("edition"),
        extent=parsed.get("extent"),
        series_title=parsed.get("series_title"),
        series_numbering=parsed.get("series_numbering"),
        volume_designation=parsed.get("volume_designation"),
        carrier_type=carrier_type,
        is_component=is_component,
    )

    # 4. Association
    assoc = ManifestationExpressionAssociation(
        manifestation_id=0,
        expression_id=0,
        manifestation_uri=manifestation.manifestation_uri,
        expression_uri=expression.expression_uri,
        is_primary=True,
    )

    # 5. Items
    items = (
        parse_marc_items(
            record_elem, manifestation_id=0, source=source, record_origin=effective_origin
        )
        if include_items
        else []
    )

    # 6. Identifiers
    identifiers: list[IdentifierEntity] = []
    for isbn in parsed["identifiers"].get("isbn", []):
        norm = isbn.replace("-", "").strip()
        identifiers.append(
            IdentifierEntity(
                entity_type="manifestation",
                entity_id=0,
                scheme="isbn",
                value=isbn,
                value_normalized=norm,
            )
        )
    for issn in parsed["identifiers"].get("issn", []):
        norm = issn.replace("-", "").strip().lower()
        identifiers.append(
            IdentifierEntity(
                entity_type="manifestation",
                entity_id=0,
                scheme="issn",
                value=issn,
                value_normalized=norm,
            )
        )
    for ismn in parsed["identifiers"].get("ismn", []):
        norm = ismn.replace("-", "").strip()
        identifiers.append(
            IdentifierEntity(
                entity_type="manifestation",
                entity_id=0,
                scheme="ismn",
                value=ismn,
                value_normalized=norm,
            )
        )
    for isrc in parsed["identifiers"].get("isrc", []):
        norm = isrc.replace("-", "").strip().upper()
        identifiers.append(
            IdentifierEntity(
                entity_type="manifestation",
                entity_id=0,
                scheme="isrc",
                value=isrc,
                value_normalized=norm,
            )
        )
    for ean in parsed["identifiers"].get("ean", []):
        norm = ean.replace("-", "").strip()
        identifiers.append(
            IdentifierEntity(
                entity_type="manifestation",
                entity_id=0,
                scheme="ean",
                value=ean,
                value_normalized=norm,
            )
        )
    for doi in parsed["identifiers"].get("doi", []):
        norm = doi.strip().lower()
        identifiers.append(
            IdentifierEntity(
                entity_type="manifestation",
                entity_id=0,
                scheme="doi",
                value=doi,
                value_normalized=norm,
            )
        )
    for urn in parsed["identifiers"].get("urn", []):
        norm = urn.strip().lower()
        identifiers.append(
            IdentifierEntity(
                entity_type="manifestation",
                entity_id=0,
                scheme="urn",
                value=urn,
                value_normalized=norm,
            )
        )
    for oid in parsed["identifiers"].get("other_ids", []):
        identifiers.append(
            IdentifierEntity(
                entity_type="manifestation",
                entity_id=0,
                scheme="other",
                value=oid,
                value_normalized=oid.strip().lower(),
            )
        )

    # Music / Media publisher numbers (028)
    for f in _get_datafields(record_elem, "028"):
        num = _get_subfield(f, "a")
        if num:
            identifiers.append(
                IdentifierEntity(
                    entity_type="manifestation",
                    entity_id=0,
                    scheme="publisher_number",
                    value=num,
                    value_normalized=num.strip().lower(),
                )
            )

    # Early Print Fingerprints (026)
    for fp in parsed.get("fingerprints", []):
        identifiers.append(
            IdentifierEntity(
                entity_type="manifestation",
                entity_id=0,
                scheme="fingerprint",
                value=fp,
                value_normalized=fp.strip().lower(),
            )
        )

    # Patent Numbers (013)
    for pat in parsed.get("patent_numbers", []):
        identifiers.append(
            IdentifierEntity(
                entity_type="work",
                entity_id=0,
                scheme="patent_number",
                value=pat,
                value_normalized=pat.strip().lower(),
            )
        )

    # 1..n Work Authority Identifiers dynamically emitted
    for scheme, wids in (parsed.get("work_identifiers") or {}).items():
        for wid in wids:
            identifiers.append(
                IdentifierEntity(
                    entity_type="work",
                    entity_id=0,
                    scheme=scheme,
                    value=wid,
                    value_normalized=wid.strip().lower(),
                )
            )

    # Digital File Formats (347)
    for fmt in parsed.get("digital_file_formats", []):
        identifiers.append(
            IdentifierEntity(
                entity_type="manifestation",
                entity_id=0,
                scheme="file_format",
                value=fmt,
                value_normalized=fmt.strip().lower(),
            )
        )

    # 7. Contributions are emitted alongside the agents below, where each agent's
    # URI is known, so they can be bound to the right agent and the right work.
    contributions: list[ContributionEntity] = []
    work_roles = {"aut", "cmp", "drt", "cre", "dsr", "prg", "arc"}

    # 8. Work Relationships
    relationships: list[WorkRelationship] = []
    for f in _get_datafields(record_elem, "770"):
        relationships.append(
            WorkRelationship(source_work_id=0, target_work_id=0, relationship_type="has_expansion")
        )
    for f in _get_datafields(record_elem, "772"):
        relationships.append(
            WorkRelationship(source_work_id=0, target_work_id=0, relationship_type="expansion_of")
        )
    for f in _get_datafields(record_elem, "776"):
        relationships.append(
            WorkRelationship(
                source_work_id=0, target_work_id=0, relationship_type="reproduction_of"
            )
        )
    for f in _get_datafields(record_elem, "780"):
        relationships.append(
            WorkRelationship(source_work_id=0, target_work_id=0, relationship_type="continues")
        )
    for f in _get_datafields(record_elem, "785"):
        relationships.append(
            WorkRelationship(source_work_id=0, target_work_id=0, relationship_type="continued_by")
        )

    # 9. Medium of Performance (MARC 382)
    mop_field = get_category_default_field_map()["medium_of_performance"]
    seen_mop_uris: set[str] = set()
    for f in _get_datafields(record_elem, mop_field):
        heading = _get_subfield(f, "a")
        if heading:
            uri = _get_subfield(f, "0") or _get_subfield(f, "1")
            vocab = _get_subfield(f, "2")
            cid = extract_concept_id(uri) if uri else None
            key = uri or f"{vocab or ''}:{heading}"
            if key not in seen_mop_uris:
                seen_mop_uris.add(key)
                subjects.append(
                    EntitySubject(
                        entity_type="work",
                        entity_id=0,
                        entity_uri=work.work_uri,
                        source=source,
                        source_record_id=source_id,
                        relation_type="medium_of_performance",
                        vocabulary=vocab,
                        concept_id=cid or uri,
                        raw_label=heading,
                        is_resolved=bool(vocab and (cid or uri)),
                        is_primary=False,
                    )
                )

    # Pre-render serializations using the WEMI Triad
    translators = [
        a for a in parsed["agents"] if "trl" in a.get("role_counts", {}) or a.get("role") == "trl"
    ]
    narrators = [
        a
        for a in parsed["agents"]
        if any(r in a.get("role_counts", {}) for r in ("nrt", "lbr", "narrator", "lukija"))
        or (a.get("role") and a.get("role").strip().lower() in ("nrt", "lbr", "narrator", "lukija"))
    ]
    work.serialized_text = engine.render_work(
        work, authors=parsed["agents"], subjects=parsed.get("subjects_folded") or []
    )
    expression.serialized_text = engine.render_expression(
        expression, work_title=work.title, translators=translators, narrators=narrators
    )
    manifestation.serialized_text = engine.render_manifestation(manifestation)

    # Agents
    agents_list: list[AgentEntity] = []
    for idx, a in enumerate(parsed["agents"]):
        a_name = a.get("name")
        if not a_name:
            continue
        u0 = a.get("uri_subfield_0") or []
        u1 = a.get("uri_subfield_1") or []
        a_uri = (
            u0[0]
            if u0
            else (
                u1[0]
                if u1
                else f"urn:agent:{source}:{source_id}:{derive_agent_id(a_name, a.get('dates'), a.get('fuller_name'))}"
            )
        )
        norm_name = a_name.strip().lower()
        agents_list.append(
            AgentEntity(
                agent_uri=a_uri,
                source=source,
                source_id=source_id,
                record_origin=effective_origin,
                name=a_name,
                name_normalized=norm_name,
                surname=a.get("surname"),
                dates=a.get("dates"),
                birth_year=a.get("birth_year"),
                death_year=a.get("death_year"),
                fuller_name=a.get("fuller_name"),
                is_personal=a.get("is_personal", True),
                is_fictional=a.get("is_fictional", False),
                variant_names=list(a.get("variant_names", [])),
                qualifiers=list(a.get("qualifiers") or []),
            )
        )

        # The agent's own role, from the histogram the parse already built. This
        # read `a.get("role")`, which no code ever set, so every contribution row
        # came out "aut"/is_primary/work - a translator's contribution was filed
        # against the work as its author, and the expression tier held no
        # contributions at all, which is why the matcher's expression-level
        # contributor lookup never found a name to compare.
        agent_role_counts = a.get("role_counts") or {}
        role = a.get("role") or (
            max(agent_role_counts, key=lambda r: (agent_role_counts[r], r))
            if agent_role_counts
            else "aut"
        )
        is_work_role = role in work_roles
        contributions.append(
            ContributionEntity(
                entity_type="work" if is_work_role else "expression",
                entity_id=0,
                agent_id=0,
                entity_uri=work.work_uri if is_work_role else expression.expression_uri,
                agent_uri=a_uri,
                role=role,
                is_primary=(role in ("aut", "cmp")),
            )
        )

        # Persist every authority URI carried on the agent.
        for ident_data in a.get("identifiers", []):
            identifiers.append(
                IdentifierEntity(
                    entity_type="agent",
                    entity_id=0,
                    entity_uri=a_uri,
                    source=ident_data.get("source", "uri"),
                    source_id=ident_data.get("source_id", ""),
                    source_id_normalized=ident_data.get("source_id_normalized", ""),
                )
            )

    # Bind non-agent identifiers to their owning entity by URI so that a batch
    # holding several records does not attribute them all to the first one.
    _owner_uris: dict[str, Optional[str]] = {
        "work": work.work_uri,
        "expression": expression.expression_uri,
        "manifestation": manifestation.manifestation_uri,
    }
    for ident in identifiers:
        if not ident.entity_uri:
            ident.entity_uri = _owner_uris.get(ident.entity_type)

    _agents = parsed.get("agents") or []
    _author_key = compute_author_key(
        _agents[0]["name"] if _agents else None,
        _agents[0].get("surname") if _agents else None,
    )
    _title_stem = compute_title_stem(
        parsed.get("uniform_title") or parsed.get("title"), lang=parsed.get("language")
    )
    keys.extend(
        to_manifestation_entity_keys(
            manifestation.manifestation_uri,
            derive_domain_keys(record_elem, parsed, work_type, _author_key, _title_stem),
        )
    )

    # 600 personal name subjects -> Agent + Contribution (role="subject")
    for f in _get_datafields(record_elem, "600"):
        name_sub = _get_subfield(f, "a")
        if name_sub:
            name_sub_clean = (
                strip_isbd(name_sub, strip_isbd_punctuation, isbd_punctuation) or name_sub
            )
            dates_sub = _get_subfield(f, "d")
            b_yr, d_yr = parse_agent_dates(dates_sub)
            sub0 = _get_subfield(f, "0") or _get_subfield(f, "1")
            a_sub_uri = (
                sub0
                or f"urn:agent:{source}:{source_id}:{derive_agent_id(name_sub_clean, dates_sub)}"
            )
            if not any(ag.agent_uri == a_sub_uri for ag in agents_list):
                agents_list.append(
                    AgentEntity(
                        agent_uri=a_sub_uri,
                        source=source,
                        source_id=source_id,
                        record_origin=effective_origin,
                        name=name_sub_clean,
                        name_normalized=name_sub_clean.strip().lower(),
                        surname=name_sub_clean.split(",")[0].strip()
                        if "," in name_sub_clean
                        else None,
                        dates=dates_sub,
                        birth_year=b_yr,
                        death_year=d_yr,
                        is_personal=True,
                    )
                )
            contributions.append(
                ContributionEntity(
                    entity_type="work",
                    entity_id=0,
                    agent_id=0,
                    entity_uri=work.work_uri,
                    agent_uri=a_sub_uri,
                    role="subject",
                    is_primary=False,
                    field_tag="600",
                )
            )

    for s in parsed.get("subjects_folded") or []:
        field = str(s.get("field") or "")
        if field == "382" or s.get("category") == "medium_of_performance":
            continue
        relation_type = "topic"
        if field == "651":
            relation_type = "geographic"
        elif field == "655":
            relation_type = "genre_form"
        elif field == "648":
            relation_type = "temporal"
        vocab = s.get("vocabulary")
        cid = s.get("code") or s.get("concept_id")
        raw_lbl = s.get("heading")
        subjects.append(
            EntitySubject(
                entity_type="work",
                entity_id=0,
                entity_uri=work.work_uri,
                source=source,
                source_record_id=source_id,
                relation_type=relation_type,
                vocabulary=vocab,
                concept_id=cid,
                raw_label=raw_lbl,
                is_resolved=bool(vocab and cid),
                is_primary=False,
            )
        )

    return DecomposedWEMIResult(
        work=work,
        expression=expression,
        manifestation=manifestation,
        items=items,
        agents=agents_list,
        relationships=relationships,
        identifiers=identifiers,
        associations=[assoc],
        contributions=contributions,
        keys=keys,
        subjects=subjects,
    )





def build_target_record_from_marc(
    record_elem: etree._Element,
    source: str = "target",
    id_source: str = "marc_001",
    serialization_engine: Optional[WEMISerializationEngine] = None,
    supported_subject_vocabularies: Optional[Iterable[str]] = None,
    work_authority_sources: Optional[Iterable[str]] = None,
    legacy_to_modern: Optional[dict[str, str]] = None,
    vocab_store: Optional[Any] = None,
    strip_isbd_punctuation: bool = True,
    isbd_punctuation: str = DEFAULT_ISBD_PUNCTUATION,
) -> MatchRecord:
    """Extract and stage a MatchRecord with all 42 domain blocking keys and pre-rendered serializations."""
    parsed = parse_marc_record_elem(
        record_elem,
        source=source,
        id_source=id_source,
        supported_subject_vocabularies=supported_subject_vocabularies,
        work_authority_sources=work_authority_sources,
        legacy_to_modern=legacy_to_modern,
        vocab_store=vocab_store,
        strip_isbd_punctuation=strip_isbd_punctuation,
        isbd_punctuation=isbd_punctuation,
    )
    source_id = parsed["source_id"] or "unknown_target"

    work_type = determine_work_type(record_elem)
    title = parsed["title"]
    uniform_title = parsed.get("uniform_title")
    lang = parsed.get("language")

    # Author key
    author_name = parsed["agents"][0]["name"] if parsed["agents"] else None
    author_surname = parsed["agents"][0].get("surname") if parsed["agents"] else None
    author_key = compute_author_key(author_name, author_surname)

    # Title stem
    title_stem = compute_title_stem(uniform_title or title, lang=lang)

    # Publication year
    pub_year = None
    py_raw = parsed.get("publication_year") or ""
    if py_raw.isdigit():
        pub_year = int(py_raw)

    is_component = bool(_get_datafields(record_elem, "773"))

    # One shared derivation, so the staged record and the persisted entity_keys
    # rows cannot drift apart.
    keys = derive_domain_keys(record_elem, parsed, work_type, author_key, title_stem)

    # Pre-render serializations
    engine = serialization_engine or WEMISerializationEngine()
    decomp = decompose_marc_wemi(
        record_elem,
        source=source,
        id_source=id_source,
        serialization_engine=engine,
        supported_subject_vocabularies=supported_subject_vocabularies,
        work_authority_sources=work_authority_sources,
        legacy_to_modern=legacy_to_modern,
    )
    work, expression, manifestation = decomp.work, decomp.expression, decomp.manifestation

    if isinstance(record_elem, MarcRecordData):
        raw_payload = record_elem.to_xml_bytes()
    else:
        raw_payload = etree.tostring(record_elem, encoding="utf-8")

    return MatchRecord(
        record_id=source_id,
        format="marcxml",
        work_type=work_type,
        title=title,
        uniform_title=uniform_title,
        language=lang,
        author_key=author_key,
        title_stem=title_stem,
        keys=keys,
        identifiers={
            scheme.lower(): list(values)
            for scheme, values in (parsed.get("identifiers") or {}).items()
            if values
        },
        content_type=expression.content_type,
        work_uri=work.work_uri,
        expression_uri=expression.expression_uri,
        manifestation_uri=manifestation.manifestation_uri,
        year=pub_year,
        volume_designation=parsed.get("volume_designation"),
        is_component=is_component,
        serialized_work=work.serialized_text,
        serialized_expression=expression.serialized_text,
        serialized_manifestation=manifestation.serialized_text,
        raw_payload=raw_payload,
    )
