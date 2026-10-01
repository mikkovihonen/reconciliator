"""MARC 21 Authority record decomposition and relationship extraction."""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any, Optional

from lxml import etree

from reconciliator.embedding.template import WEMISerializationEngine
from reconciliator.loaders.base import DEFAULT_ISBD_PUNCTUATION, strip_isbd
from reconciliator.loaders.marc.classification import determine_work_type
from reconciliator.loaders.marc.xml_utils import (
    _get_controlfield,
    _get_datafields,
    _get_leader,
    _get_subfield,
    _get_subfields,
    derive_agent_id,
    derive_content_id,
)
from reconciliator.models import (
    AgentEntity,
    AgentRelationshipEntity,
    ContributionEntity,
    EntitySubject,
    ExpressionEntity,
    IdentifierEntity,
    ItemEntity,
    ManifestationEntity,
    ManifestationExpressionAssociation,
    RecordOrigin,
    WorkEntity,
    WorkRelationship,
)
from reconciliator.normalizers.agent import (
    parse_agent_dates,
)
from reconciliator.normalizers.blocking import compute_title_stem
from reconciliator.utils.curie import (
    parse_curie,
    to_curie,
)
from reconciliator.vocab import extract_concept_id


def is_marc_authority_record(record_elem: etree._Element) -> bool:
    """Determine whether a MARCXML element represents a MARC 21 Authority record.

    A MARC 21 Authority record has Leader/06 == 'z'. If leader is missing or short,
    it is considered an authority record if it has an established 1xx heading
    (100, 110, 111, 130, 147, 148, 150, 151, 155) and NO 245 title statement
    (which is mandatory in MARC 21 bibliographic records).
    """
    ldr = _get_leader(record_elem)
    if len(ldr) > 6 and ldr[6].lower() == "z":
        return True

    # Heuristic fallback: bibliographic records require 245, authority records never have 245
    has_245 = bool(_get_datafields(record_elem, "245"))
    if not has_245:
        authority_tags = {"100", "110", "111", "130", "147", "148", "150", "151", "155"}
        for tag in authority_tags:
            if _get_datafields(record_elem, tag):
                return True
    return False


def extract_agent_qualifiers(
    raw_name: str,
    sub_c_values: Iterable[str] = (),
    qualifier_config: Optional[Any] = None,
) -> tuple[str, list[str]]:
    """Extract qualifiers from subfield values and/or parenthesized name suffix according to configuration."""
    from reconciliator.config import AppConfig

    if qualifier_config is None:
        try:
            qualifier_config = AppConfig().agent_authority.qualifiers
        except Exception:
            qualifier_config = None

    pattern_str = getattr(qualifier_config, "qualifier_pattern", r"\(([^)]+)\)$")
    strip_brackets = getattr(qualifier_config, "strip_enclosing_brackets", True)
    bracket_pairs = getattr(qualifier_config, "bracket_pairs", [["(", ")"], ["[", "]"]])

    qualifiers: list[str] = []
    clean_name = raw_name

    # Check parenthesized suffix in raw_name
    if pattern_str and raw_name:
        m = re.search(pattern_str, raw_name.strip())
        if m:
            q_val = m.group(1).strip()
            if q_val:
                qualifiers.append(q_val)
            clean_name = raw_name[: m.start()].rstrip(" ,;.")

    # Process subfield c values
    for val in sub_c_values:
        v = val.strip(" ,;.")
        if not v:
            continue
        if strip_brackets:
            for pair in bracket_pairs:
                open_b, close_b = pair[0], pair[1]
                if v.startswith(open_b) and v.endswith(close_b) and len(v) >= 2:
                    v = v[len(open_b) : -len(close_b)].strip(" ,;.")
        if v and v not in qualifiers:
            qualifiers.append(v)

    return clean_name, qualifiers


def resolve_marc_relationship_type(
    w_code: str,
    i_desig: str,
    tag: str = "500",
    config: Optional[Any] = None,
) -> str:
    """Resolve relationship type from MARC 5xx $w code and $i designation according to config."""
    from reconciliator.config import AppConfig

    marc_rel_cfg = getattr(getattr(config, "agent_authority", None), "marc_relationships", None)
    if marc_rel_cfg is None:
        try:
            marc_rel_cfg = AppConfig().agent_authority.marc_relationships
        except Exception:
            marc_rel_cfg = None

    code_mappings = getattr(marc_rel_cfg, "code_mappings", None) or {
        "a": "predecessor_of",
        "b": "successor_of",
        "t": "hierarchical_superior_of",
        "n": "related_corporate_body",
    }
    designation_patterns = getattr(marc_rel_cfg, "designation_patterns", None) or []
    default_rel_type = getattr(marc_rel_cfg, "default_relationship_type", "related_to")

    w_clean = w_code.strip()
    if w_clean and w_clean in code_mappings:
        return code_mappings[w_clean]

    i_clean = i_desig.strip()
    if i_clean:
        for pat_entry in designation_patterns:
            pat = (
                pat_entry.get("pattern", "")
                if isinstance(pat_entry, dict)
                else getattr(pat_entry, "pattern", "")
            )
            r_type = (
                pat_entry.get("rel_type", "")
                if isinstance(pat_entry, dict)
                else getattr(pat_entry, "rel_type", "")
            )
            if pat and r_type and re.search(pat, i_clean):
                return r_type

    if tag == "510":
        return "related_corporate_body"
    elif tag == "500":
        return "related_person"
    return default_rel_type


def decompose_marc_authority(
    record_elem: etree._Element,
    source: str = "authority",
    id_source: str = "marc_001",
    record_origin: RecordOrigin = "authority",
    serialization_engine: Optional[WEMISerializationEngine] = None,
    strip_isbd_punctuation: bool = True,
    isbd_punctuation: str = DEFAULT_ISBD_PUNCTUATION,
    subject_sink: Optional[list[EntitySubject]] = None,
    agent_relationship_sink: Optional[list[AgentRelationshipEntity]] = None,
    config: Optional[Any] = None,
) -> tuple[
    list[WorkEntity],
    list[ExpressionEntity],
    list[ManifestationEntity],
    list[ItemEntity],
    list[AgentEntity],
    list[WorkRelationship],
    list[IdentifierEntity],
    list[ManifestationExpressionAssociation],
    list[ContributionEntity],
]:
    """Decompose a MARC 21 Authority record (Leader/06 = 'z') into WEMI domain entities.

    Extracts:
    - Work authorities (130 Uniform Title, or 100/110/111 with $t Author/Title):
      creates WorkEntity and canonical ExpressionEntity, with variant titles from 4xx,
      work relationships from 5xx, medium of performance from 382, and standard identifiers from 024.
    - Agent authorities (100/110/111 without $t):
      creates AgentEntity with dates, variant names (4xx), affiliations (373), and standard identifiers from 024.
    """
    # 1. Resolve source_id
    source_id: Optional[str] = None
    if id_source == "marc_001":
        source_id = _get_controlfield(record_elem, "001")
    elif id_source == "marc_024":
        for f in _get_datafields(record_elem, "024"):
            v = _get_subfield(f, "a")
            if v:
                source_id = v
                break

    if not source_id or source_id == "unknown_id":
        source_id = derive_content_id(record_elem, prefix="auth")

    # 2. Extract standard identifiers from 024 and 035
    raw_identifiers: list[tuple[str, str]] = []
    for f in _get_datafields(record_elem, "024"):
        val = _get_subfield(f, "a")
        scheme = (_get_subfield(f, "2") or "other").lower().strip()
        if val:
            raw_identifiers.append((scheme, val))
    for f in _get_datafields(record_elem, "035"):
        val = _get_subfield(f, "a")
        if val:
            raw_identifiers.append(("control_number", val))

    # 3. Detect heading tags
    f130 = _get_datafields(record_elem, "130")
    f100 = _get_datafields(record_elem, "100")
    f110 = _get_datafields(record_elem, "110")
    f111 = _get_datafields(record_elem, "111")

    is_work_authority = bool(f130) or any(
        _get_subfield(f, "t") is not None for f in (f100 + f110 + f111)
    )

    if is_work_authority:
        # -------------------------------------------------------------
        # Work Authority (130 Uniform title, or 100/110/111 with $t)
        # -------------------------------------------------------------
        heading_field: etree._Element
        agent_field: Optional[etree._Element] = None
        if f130:
            heading_field = f130[0]
            title = _get_subfield(heading_field, "a") or "Untitled Work"
        else:
            cand_heading = (f100 or f110 or f111)[0]
            heading_field = cand_heading
            title = _get_subfield(heading_field, "t") or "Untitled Work"
            agent_field = cand_heading

        # Clean trailing punctuation
        title = strip_isbd(title, strip_isbd_punctuation, isbd_punctuation) or title

        agents_list: list[AgentEntity] = []
        contributions: list[ContributionEntity] = []
        if agent_field is not None:
            agent_name = _get_subfield(agent_field, "a")
            if agent_name:
                agent_name_clean = (
                    strip_isbd(agent_name, strip_isbd_punctuation, isbd_punctuation) or agent_name
                )
                agent_name_clean, parsed_qualifiers = extract_agent_qualifiers(
                    agent_name_clean, _get_subfields(agent_field, "c"), config
                )
                agent_dates = _get_subfield(agent_field, "d")
                b_yr, d_yr = parse_agent_dates(agent_dates)
                sub0 = _get_subfield(agent_field, "0") or _get_subfield(agent_field, "1")
                agent_uri = sub0 or (
                    f"urn:agent:{source}:{source_id}:{derive_agent_id(agent_name_clean, agent_dates)}"
                )
                agent = AgentEntity(
                    agent_uri=agent_uri,
                    source=source,
                    source_id=source_id,
                    record_origin=record_origin,
                    name=agent_name_clean,
                    name_normalized=agent_name_clean.strip().lower(),
                    surname=agent_name_clean.split(",")[0].strip()
                    if "," in agent_name_clean
                    else None,
                    dates=agent_dates,
                    birth_year=b_yr,
                    death_year=d_yr,
                    fuller_name=_get_subfield(agent_field, "q"),
                    is_personal=bool(f100),
                    qualifiers=parsed_qualifiers,
                )
                agents_list.append(agent)
                contributions.append(
                    ContributionEntity(
                        entity_type="work",
                        entity_id=0,
                        agent_id=0,
                        agent_uri=agent_uri,
                        role="aut",
                        is_primary=True,
                    )
                )

        # Language from 377 $a
        orig_lang: Optional[str] = None
        for f in _get_datafields(record_elem, "377"):
            orig_lang = _get_subfield(f, "a")
            if orig_lang:
                break

        # Work type from 380 or leader
        work_type = determine_work_type(record_elem)

        # Medium of performance from 382
        if subject_sink is not None:
            seen_mop_keys: set[str] = set()
            for f in _get_datafields(record_elem, "382"):
                h = _get_subfield(f, "a")
                if h:
                    uri = _get_subfield(f, "0") or _get_subfield(f, "1")
                    vocab = _get_subfield(f, "2") or "seko"
                    cid = extract_concept_id(uri) if uri else None
                    key = uri or f"{vocab}:{h}"
                    if key not in seen_mop_keys:
                        seen_mop_keys.add(key)
                        subject_sink.append(
                            EntitySubject(
                                entity_type="work",
                                entity_id=0,
                                entity_uri=to_curie(source, source_id),
                                source=source,
                                source_record_id=source_id,
                                relation_type="medium_of_performance",
                                vocabulary=vocab,
                                concept_id=cid or uri,
                                raw_label=h,
                                is_resolved=bool(vocab and (cid or uri)),
                                is_primary=False,
                            )
                        )

        # Variant titles from 4xx
        variant_titles: list[str] = []
        for f in _get_datafields(record_elem, "430"):
            vt = _get_subfield(f, "a")
            if vt:
                variant_titles.append(
                    strip_isbd(vt, strip_isbd_punctuation, isbd_punctuation) or vt
                )
        for tag in ("400", "410", "411"):
            for f in _get_datafields(record_elem, tag):
                vt = _get_subfield(f, "t")
                if vt:
                    variant_titles.append(
                        strip_isbd(vt, strip_isbd_punctuation, isbd_punctuation) or vt
                    )

        # Relationships from 5xx
        relationships: list[WorkRelationship] = []
        for tag in ("500", "510", "511", "530"):
            for f in _get_datafields(record_elem, tag):
                rel_text = _get_subfield(f, "i") or ""
                w_code = _get_subfield(f, "w") or ""
                rel_type = "adaptation_of"
                if "continues" in rel_text.lower() or w_code == "a":
                    rel_type = "continues"
                elif "continued by" in rel_text.lower() or w_code == "b":
                    rel_type = "continued_by"
                elif "expansion" in rel_text.lower():
                    rel_type = "expansion_of"
                elif "reproduction" in rel_text.lower():
                    rel_type = "reproduction_of"
                relationships.append(
                    WorkRelationship(
                        source_work_id=0,
                        target_work_id=0,
                        relationship_type=rel_type,  # type: ignore[arg-type]
                    )
                )

        # URI & Identifiers
        work_uri: Optional[str] = None
        identifiers: list[IdentifierEntity] = []
        for scheme, val in raw_identifiers:
            norm = val.replace("-", "").strip().lower()
            if not work_uri and (
                scheme in ("uri", "urn", "asteri")
                or val.startswith(("http://", "https://", "urn:"))
            ):
                work_uri = val
            identifiers.append(
                IdentifierEntity(
                    entity_type="work",
                    entity_id=0,
                    scheme=scheme,
                    value=val,
                    value_normalized=norm,
                )
            )
        if not work_uri:
            work_uri = f"urn:work:{source}:{source_id}"

        for ident in identifiers:
            if not ident.entity_uri and ident.entity_type == "work":
                ident.entity_uri = work_uri
        for contrib in contributions:
            if not contrib.entity_uri and contrib.entity_type == "work":
                contrib.entity_uri = work_uri

        # Build WorkEntity
        work = WorkEntity(
            work_uri=work_uri,
            source=source,
            source_id=source_id,
            record_origin=record_origin,
            title=title,
            title_normalized=compute_title_stem(title, lang=orig_lang),
            original_language=orig_lang,
            work_type=work_type,
        )

        # Route variant titles through subject_sink so they're searchable
        # via BM25 and the dense work index.
        if subject_sink is not None:
            for vt in variant_titles:
                subject_sink.append(
                    EntitySubject(
                        entity_type="work",
                        entity_id=0,
                        source=source,
                        source_record_id=source_id,
                        relation_type="variant_title",
                        raw_label=vt,
                    )
                )

        # Build canonical ExpressionEntity
        expr_lang = orig_lang or "und"
        expression = ExpressionEntity(
            work_id=0,
            work_uri=work_uri,
            expression_uri=f"urn:exp:{source}:{source_id}:{expr_lang}",
            source=source,
            source_id=source_id,
            record_origin=record_origin,
            language=expr_lang,
            content_type="text",
            title=title,
        )

        engine = serialization_engine or WEMISerializationEngine()
        work.serialized_text = engine.render_work(
            work, authors=[{"name": a.name} for a in agents_list]
        )
        expression.serialized_text = engine.render_expression(expression, work_title=work.title)

        return (
            [work],
            [expression],
            [],
            [],
            agents_list,
            relationships,
            identifiers,
            [],
            contributions,
        )

    elif f100 or f110 or f111:
        # -------------------------------------------------------------
        # Agent Authority (100 Personal, 110 Corporate, 111 Meeting)
        # -------------------------------------------------------------
        agent_field = (f100 or f110 or f111)[0]
        agent_name = _get_subfield(agent_field, "a") or "Unknown Agent"
        agent_name_clean = (
            strip_isbd(agent_name, strip_isbd_punctuation, isbd_punctuation) or agent_name
        )
        sub_c_list = _get_subfields(agent_field, "c")
        agent_name_clean, parsed_qualifiers = extract_agent_qualifiers(
            agent_name_clean, sub_c_list, config
        )

        from reconciliator.config import AppConfig

        q_cfg = getattr(getattr(config, "agent_authority", None), "qualifiers", None)
        if q_cfg is None:
            try:
                q_cfg = AppConfig().agent_authority.qualifiers
            except Exception:
                q_cfg = None
        corp_cat = getattr(q_cfg, "corporate_category_as_qualifier", True)
        if corp_cat and not f100:
            for f in _get_datafields(record_elem, "368"):
                cat_val = _get_subfield(f, "a") or _get_subfield(f, "b")
                if cat_val:
                    cat_clean = (
                        strip_isbd(cat_val, strip_isbd_punctuation, isbd_punctuation) or cat_val
                    )
                    if cat_clean and cat_clean not in parsed_qualifiers:
                        parsed_qualifiers.append(cat_clean)

        agent_dates = _get_subfield(agent_field, "d")
        birth_yr, death_yr = parse_agent_dates(agent_dates)

        # Check 046 for birth/death dates
        if birth_yr is None or death_yr is None:
            for f in _get_datafields(record_elem, "046"):
                f_b = _get_subfield(f, "f")
                f_d = _get_subfield(f, "g")
                if f_b and birth_yr is None:
                    birth_yr = int(f_b[:4]) if f_b[:4].isdigit() else None
                if f_d and death_yr is None:
                    death_yr = int(f_d[:4]) if f_d[:4].isdigit() else None

        # Variant names from 400, 410, 411
        variant_names: list[str] = []
        for tag in ("400", "410", "411"):
            for f in _get_datafields(record_elem, tag):
                v = _get_subfield(f, "a")
                if v:
                    variant_names.append(
                        strip_isbd(v, strip_isbd_punctuation, isbd_punctuation) or v
                    )

        # Affiliations from 373
        affiliations: list[str] = []
        for f in _get_datafields(record_elem, "373"):
            aff = _get_subfield(f, "a")
            if aff:
                aff_clean = strip_isbd(aff, strip_isbd_punctuation, isbd_punctuation) or aff
                if aff_clean and aff_clean not in affiliations:
                    affiliations.append(aff_clean)

        # Identifiers & Agent URI
        agent_uri: Optional[str] = None
        identifiers: list[IdentifierEntity] = []
        for scheme, val in raw_identifiers:
            norm = val.replace("-", "").strip().lower()
            if not agent_uri and (
                scheme in ("isni", "viaf", "uri", "urn", "asteri")
                or val.startswith(("http://", "https://", "urn:"))
            ):
                agent_uri = val
            identifiers.append(
                IdentifierEntity(
                    entity_type="agent",
                    entity_id=0,
                    scheme=scheme,
                    value=val,
                    value_normalized=norm,
                )
            )

        # Authority headings carry their own $0/$1 links; keep them as identifiers
        # so reconciliation can transfer them, not just the one chosen as agent_uri.
        for sub_code in ("0", "1"):
            for uri in _get_subfields(agent_field, sub_code):
                if not uri or not uri.strip():
                    continue
                if not agent_uri:
                    agent_uri = uri.strip()
                scheme, val = parse_curie(
                    uri.strip(), default_source="wikidata" if sub_code == "1" else "uri"
                )
                if not val or any(
                    (i.source == scheme and i.source_id == val) or i.source_id == uri.strip()
                    for i in identifiers
                ):
                    continue
                identifiers.append(
                    IdentifierEntity(
                        entity_type="agent",
                        entity_id=0,
                        scheme=scheme,
                        value=val,
                        value_normalized=val.replace("-", "").strip().lower(),
                    )
                )

        if not agent_uri:
            sub0 = _get_subfield(agent_field, "0") or _get_subfield(agent_field, "1")
            agent_uri = sub0 or (
                f"urn:agent:{source}:{source_id}:{derive_agent_id(agent_name_clean, agent_dates)}"
            )

        for ident in identifiers:
            if ident.entity_type == "agent" and not ident.entity_uri:
                ident.entity_uri = agent_uri

        agent = AgentEntity(
            agent_uri=agent_uri,
            source=source,
            source_id=source_id,
            record_origin=record_origin,
            name=agent_name_clean,
            name_normalized=agent_name_clean.strip().lower(),
            surname=agent_name_clean.split(",")[0].strip() if "," in agent_name_clean else None,
            dates=agent_dates,
            birth_year=birth_yr,
            death_year=death_yr,
            fuller_name=_get_subfield(agent_field, "q"),
            is_personal=bool(f100),
            variant_names=variant_names,
            qualifiers=parsed_qualifiers,
            affiliations=affiliations,
            identifiers=identifiers,
        )

        if agent_relationship_sink is not None:
            # 373 affiliations as relationships
            for aff in affiliations:
                agent_relationship_sink.append(
                    AgentRelationshipEntity(
                        source_agent_id=0,
                        target_agent_id=0,
                        source_agent_curie=agent_uri,
                        relationship_type="affiliation",
                        target_name=aff,
                    )
                )

            # 500, 510, 511 See Also From Tracings
            for tag in ("500", "510", "511"):
                for f in _get_datafields(record_elem, tag):
                    if _get_subfield(f, "t") is not None:
                        continue
                    t_name = _get_subfield(f, "a")
                    if not t_name:
                        continue
                    t_name_clean = (
                        strip_isbd(t_name, strip_isbd_punctuation, isbd_punctuation) or t_name
                    )
                    w_code = _get_subfield(f, "w") or ""
                    i_desig = _get_subfield(f, "i") or ""
                    t_sub0 = _get_subfield(f, "0") or _get_subfield(f, "1")

                    rel_type = resolve_marc_relationship_type(
                        w_code=w_code,
                        i_desig=i_desig,
                        tag=tag,
                        config=config,
                    )

                    agent_relationship_sink.append(
                        AgentRelationshipEntity(
                            source_agent_id=0,
                            target_agent_id=0,
                            source_agent_curie=agent_uri,
                            target_agent_curie=t_sub0.strip() if t_sub0 else None,
                            relationship_type=rel_type,
                            target_name=t_name_clean,
                        )
                    )

        if subject_sink is not None:
            for f in _get_datafields(record_elem, "372"):
                val = _get_subfield(f, "a")
                if val:
                    val_clean = strip_isbd(val, strip_isbd_punctuation, isbd_punctuation) or val
                    v_sub2 = _get_subfield(f, "2")
                    v_sub0 = _get_subfield(f, "0") or _get_subfield(f, "1")
                    cid = extract_concept_id(v_sub0) if v_sub0 else None
                    subject_sink.append(
                        EntitySubject(
                            entity_type="agent",
                            entity_id=0,
                            entity_uri=agent_uri,
                            source=source,
                            source_record_id=source_id,
                            relation_type="field_of_activity",
                            vocabulary=v_sub2,
                            concept_id=cid,
                            raw_label=val_clean,
                            is_resolved=bool(v_sub2 and cid),
                        )
                    )
            for tag in ("368", "374"):
                for f in _get_datafields(record_elem, tag):
                    val = _get_subfield(f, "c") or _get_subfield(f, "d") or _get_subfield(f, "a")
                    if val:
                        val_clean = strip_isbd(val, strip_isbd_punctuation, isbd_punctuation) or val
                        v_sub2 = _get_subfield(f, "2")
                        v_sub0 = _get_subfield(f, "0") or _get_subfield(f, "1")
                        cid = extract_concept_id(v_sub0) if v_sub0 else None
                        subject_sink.append(
                            EntitySubject(
                                entity_type="agent",
                                entity_id=0,
                                entity_uri=agent_uri,
                                source=source,
                                source_record_id=source_id,
                                relation_type="occupation",
                                vocabulary=v_sub2,
                                concept_id=cid,
                                raw_label=val_clean,
                                is_resolved=bool(v_sub2 and cid),
                            )
                        )
            for f in _get_datafields(record_elem, "370"):
                val = _get_subfield(f, "c") or _get_subfield(f, "f") or _get_subfield(f, "e")
                if val:
                    val_clean = strip_isbd(val, strip_isbd_punctuation, isbd_punctuation) or val
                    v_sub2 = _get_subfield(f, "2")
                    v_sub0 = _get_subfield(f, "0") or _get_subfield(f, "1")
                    cid = extract_concept_id(v_sub0) if v_sub0 else None
                    subject_sink.append(
                        EntitySubject(
                            entity_type="agent",
                            entity_id=0,
                            entity_uri=agent_uri,
                            source=source,
                            source_record_id=source_id,
                            relation_type="associated_place",
                            vocabulary=v_sub2,
                            concept_id=cid,
                            raw_label=val_clean,
                            is_resolved=bool(v_sub2 and cid),
                        )
                    )

        return ([], [], [], [], [agent], [], identifiers, [], [])

    # Subject/topical/form authority without work or agent heading
    return ([], [], [], [], [], [], [], [], [])


