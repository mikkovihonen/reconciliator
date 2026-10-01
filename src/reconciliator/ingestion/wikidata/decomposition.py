"""Wikidata bibliographic entity parsing and WEMI decomposition."""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any, Optional

from reconciliator.config import (
    WikidataIngestConfig,
    get_bundled_defaults,
    get_bundled_record_defaults,
    get_category_default_field_map,
    get_subject_field_category_map,
)
from reconciliator.embedding.template import WEMISerializationEngine
from reconciliator.ingestion.wikidata.cache import AuthorLRUCache
from reconciliator.ingestion.wikidata.claims import (
    _AUTHOR_PROPERTIES,
    _CREATIVE_WORK_MAPPING,
    _DEFAULT_WIKIDATA_CONFIG,
    _HUMAN_AND_ENTITY_TYPES,
    WIKIDATA_LANG_MAP,
    WIKIDATA_PREF_LANGS,
    extract_claim_qualifier_string,
    get_claim_qids,
    get_claim_snak_value,
    is_qid,
)
from reconciliator.models import (
    AgentEntity,
    CarrierType,
    ContributionEntity,
    EntityType,
    ExpressionEntity,
    IdentifierEntity,
    ItemEntity,
    ManifestationEntity,
    ManifestationExpressionAssociation,
    WorkEntity,
    WorkRelationship,
    WorkType,
)
from reconciliator.normalizers.blocking import compute_title_stem
from reconciliator.vocab import concept_id_to_uri, format_wikidata_uri

if TYPE_CHECKING:
    from reconciliator.database import WEMIRepository

logger = logging.getLogger(__name__)


def _lookup_entity_name(
    qid: str,
    store: Optional[WEMIRepository],
    cache: Optional[AuthorLRUCache] = None,
) -> Optional[str]:
    """Look up an entity name from agents table by QID with LRU cache support."""
    if not qid:
        return None
    if cache is not None and qid in cache:
        author = cache.get(qid)
        if author:
            name = author.get("name")
            if name and not is_qid(name):
                return str(name)
        return None
    if not store:
        return None
    author = store.get_wikidata_author(qid)
    if cache is not None:
        cache.set(qid, author)
    if author:
        name = author.get("name")
        if name and not is_qid(name):
            return str(name)
    return None


def parse_wikidata_entity(
    entity: dict[str, Any],
    store: Optional[WEMIRepository] = None,
    cache: Optional[AuthorLRUCache] = None,
    config: Optional[WikidataIngestConfig] = None,
) -> Optional[dict[str, Any]]:
    """Parse a Wikidata entity into raw bibliographic dictionary format.

    Author/publisher/subject names are hydrated from the database agents/identifiers tables
    via the store parameter, with LRU cache support for O(1) in-memory resolution.
    """
    cfg = config or _DEFAULT_WIKIDATA_CONFIG
    creative_map = cfg.creative_work_types or _CREATIVE_WORK_MAPPING
    human_types = (
        set(cfg.human_and_entity_types) if cfg.human_and_entity_types else _HUMAN_AND_ENTITY_TYPES
    )

    qid = entity.get("id", "")
    claims = entity.get("claims", {})
    labels = entity.get("labels", {})

    # Check if this is a human/org/place entity — skip bibliographic processing
    instance_of = get_claim_qids(claims, "P31")
    is_human = "Q5" in instance_of
    is_org_or_place = any(k in instance_of for k in human_types - {"Q5"})

    if is_human or is_org_or_place:
        return None

    # Check if creative work or has bibliographic IDs
    is_creative = any(k in instance_of for k in creative_map)
    has_bib_ids = "P212" in claims or "P957" in claims or "P3595" in claims

    if not (is_creative or has_bib_ids):
        return None

    # Title & Subtitle
    title = ""
    p1476_val = get_claim_snak_value(claims, "P1476")
    if isinstance(p1476_val, dict):
        title = p1476_val.get("text", "")
    elif isinstance(p1476_val, str):
        title = p1476_val

    if not title:
        for lang in WIKIDATA_PREF_LANGS:
            if lang in labels:
                title = labels[lang].get("value", "")
                if title:
                    break
    if not title:
        return None

    subtitle = None
    p1680_val = get_claim_snak_value(claims, "P1680")
    if isinstance(p1680_val, dict):
        subtitle = p1680_val.get("text")
    elif isinstance(p1680_val, str):
        subtitle = p1680_val

    # Uniform Title
    uniform_title = None
    p1448_val = get_claim_snak_value(claims, "P1448")
    if isinstance(p1448_val, dict):
        uniform_title = p1448_val.get("text")
    elif isinstance(p1448_val, str):
        uniform_title = p1448_val
    if not uniform_title:
        p1705_val = get_claim_snak_value(claims, "P1705")
        if isinstance(p1705_val, dict):
            uniform_title = p1705_val.get("text")
        elif isinstance(p1705_val, str):
            uniform_title = p1705_val

    # Variant Titles from aliases
    variant_titles: list[str] = []
    aliases = entity.get("aliases", {})
    for lang in WIKIDATA_PREF_LANGS:
        for alias_item in aliases.get(lang, []):
            val = alias_item.get("value")
            if val and val not in variant_titles and val != title:
                variant_titles.append(val)

    # Publication Year
    year_str = None
    pub_time = get_claim_snak_value(claims, "P577")
    if isinstance(pub_time, dict) and "time" in pub_time:
        m = re.search(r"\+(\d{4})", pub_time["time"])
        if m:
            year_str = m.group(1)
    if not year_str:
        inc_time = get_claim_snak_value(claims, "P571")
        if isinstance(inc_time, dict) and "time" in inc_time:
            m = re.search(r"\+(\d{4})", inc_time["time"])
            if m:
                year_str = m.group(1)

    # Publisher: first check qualifier (P1810 / P1932 'stated as'), then database lookup
    publisher = extract_claim_qualifier_string(claims, "P123")
    if not publisher:
        pub_qids = get_claim_qids(claims, "P123")
        if pub_qids:
            publisher = _lookup_entity_name(pub_qids[0], store, cache=cache)
    if publisher and is_qid(publisher):
        publisher = None

    # Publication Place: first check qualifier, then database lookup
    publication_place = extract_claim_qualifier_string(claims, "P291")
    if not publication_place:
        place_qids = get_claim_qids(claims, "P291")
        if place_qids:
            publication_place = _lookup_entity_name(place_qids[0], store, cache=cache)
    if publication_place and is_qid(publication_place):
        publication_place = None

    # Language & Original Language
    language = None
    lang_qids = get_claim_qids(claims, "P407")
    if lang_qids and lang_qids[0] in WIKIDATA_LANG_MAP:
        language = WIKIDATA_LANG_MAP[lang_qids[0]]

    original_language = None
    orig_lang_qids = get_claim_qids(claims, "P364")
    if orig_lang_qids and orig_lang_qids[0] in WIKIDATA_LANG_MAP:
        original_language = WIKIDATA_LANG_MAP[orig_lang_qids[0]]

    # Edition
    edition_val = None
    p393 = get_claim_snak_value(claims, "P393")
    if p393:
        m = re.search(r"(\d+)", str(p393))
        if m:
            edition_val = int(m.group(1))

    # Extent / Page Count
    extent_val = None
    p1104 = get_claim_snak_value(claims, "P1104")
    if p1104:
        if isinstance(p1104, dict) and "amount" in p1104:
            m = re.search(r"(\d+)", str(p1104["amount"]))
            if m:
                extent_val = int(m.group(1))
        elif isinstance(p1104, (int, str)):
            m = re.search(r"(\d+)", str(p1104))
            if m:
                extent_val = int(m.group(1))

    # Series & Series Numbering
    series_title = extract_claim_qualifier_string(claims, "P179")
    series_numbering = None
    p179_stmts = claims.get("P179", [])
    if p179_stmts:
        quals = p179_stmts[0].get("qualifiers", {})
        p1545 = quals.get("P1545", [])
        if p1545:
            series_numbering = str(p1545[0].get("datavalue", {}).get("value", ""))

    if not series_title:
        series_qids = get_claim_qids(claims, "P179")
        if series_qids:
            series_title = _lookup_entity_name(series_qids[0], store, cache=cache)
    if series_title and is_qid(series_title):
        series_title = None

    # Volume Designation
    volume_str = None
    p478 = get_claim_snak_value(claims, "P478")
    if p478:
        volume_str = str(p478)
    elif series_numbering:
        volume_str = series_numbering

    # Classifications
    classifications: list[dict[str, Any]] = []
    for prop, scheme in (("P1190", "udc"), ("P1036", "ddc"), ("P1149", "lcc")):
        for stmt in claims.get(prop, []):
            code_val = stmt.get("mainsnak", {}).get("datavalue", {}).get("value")
            if code_val:
                is_pref = stmt.get("rank") == "preferred" or len(classifications) == 0
                classifications.append(
                    {
                        "code": str(code_val),
                        "scheme": scheme,
                        "is_primary": is_pref,
                    }
                )

    # Identifiers
    id_rules = get_bundled_record_defaults().get("standard_identifier_rules", {})
    isbn_props = id_rules.get("isbn", {}).get("wikidata_properties", ["P212", "P957"])
    issn_props = id_rules.get("issn", {}).get("wikidata_properties", ["P236", "P7363"])
    doi_props = id_rules.get("doi", {}).get("wikidata_properties", ["P356"])

    isbns: list[str] = []
    for prop in isbn_props:
        for stmt in claims.get(prop, []):
            val = stmt.get("mainsnak", {}).get("datavalue", {}).get("value")
            if val:
                isbns.append(str(val))

    issns: list[str] = []
    for prop in issn_props:
        for stmt in claims.get(prop, []):
            val = stmt.get("mainsnak", {}).get("datavalue", {}).get("value")
            if val:
                issns.append(str(val))

    urns: list[str] = []
    p1184 = get_claim_snak_value(claims, "P1184")
    if p1184:
        urns.append(str(p1184))

    dois: list[str] = []
    for prop in doi_props:
        p_doi = get_claim_snak_value(claims, prop)
        if p_doi:
            dois.append(str(p_doi))

    other_ids: list[str] = [qid]
    for d in dois:
        other_ids.append(f"doi:{d}")

    # Agents — hydrate from database or cache
    agents: list[dict[str, Any]] = []
    for prop, role in _AUTHOR_PROPERTIES:
        for a_qid in get_claim_qids(claims, prop):
            author_info: dict[str, Any] = {}
            if cache is not None and a_qid in cache:
                author_info = cache.get(a_qid) or {}
            elif store:
                author_info = store.get_wikidata_author(a_qid) or {}
                if cache is not None:
                    cache.set(a_qid, author_info)
            qual_name = extract_claim_qualifier_string(claims, prop)
            name = author_info.get("name") or qual_name or a_qid
            u0: list[str] = []
            for id_scheme, id_val in author_info.get("identifiers", {}).items():
                if not id_val:
                    continue
                uri = concept_id_to_uri(f"{id_scheme}:{id_val}")
                if uri:
                    u0.append(uri)

            u1 = [format_wikidata_uri(a_qid)]

            agents.append(
                {
                    "name": name,
                    "is_personal": True,
                    "role": role,
                    "uri_subfield_0": u0,
                    "uri_subfield_1": u1,
                }
            )

    # Fallback: if no author agents found from P50, check claims.P2093 (author name string)
    if not any(a.get("role") == "aut" for a in agents):
        for stmt in claims.get("P2093", []):
            datavalue = stmt.get("mainsnak", {}).get("datavalue", {})
            val = datavalue.get("value")
            if isinstance(val, str) and val.strip():
                agents.append(
                    {
                        "name": val.strip(),
                        "is_personal": True,
                        "role": "aut",
                        "uri_subfield_0": [],
                        "uri_subfield_1": [],
                    }
                )

    # Subjects — hydrate names from database or cache
    cat_defaults = get_category_default_field_map()
    field_to_cat = get_subject_field_category_map()
    subjects: dict[str, list[dict[str, Any]]] = {cat: [] for cat in field_to_cat.values()}
    topical_field = cat_defaults["topical"]
    genre_field = cat_defaults["genre_form"]
    geo_field = cat_defaults["geographic"]
    temporal_field = cat_defaults.get("temporal") or cat_defaults["chronological"]
    mop_field = cat_defaults["medium_of_performance"]

    subj_mappings = dict(
        get_bundled_defaults().get("ingestion", {}).get("wikidata", {}).get("subject_mappings")
        or {"topical": "yso", "geographic": "yso-paikat", "temporal": "yso-aika"}
    )

    for s_qid in get_claim_qids(claims, "P921"):
        s_heading = _lookup_entity_name(s_qid, store, cache=cache) or s_qid
        subjects["topical"].append(
            {
                "heading": s_heading,
                "field": topical_field,
                "vocabulary": subj_mappings["topical"],
                "uri": None,
            }
        )
    for g_qid in get_claim_qids(claims, "P136"):
        g_heading = _lookup_entity_name(g_qid, store, cache=cache) or g_qid
        subjects["genre_form"].append(
            {
                "heading": g_heading,
                "field": genre_field,
                "vocabulary": subj_mappings["genre_form"],
                "uri": None,
            }
        )
    for geo_qid in get_claim_qids(claims, "P840"):
        geo_heading = _lookup_entity_name(geo_qid, store, cache=cache) or geo_qid
        subjects["geographic"].append(
            {
                "heading": geo_heading,
                "field": geo_field,
                "vocabulary": subj_mappings["geographic"],
                "uri": None,
            }
        )
    for t_qid in get_claim_qids(claims, "P2348"):
        t_heading = _lookup_entity_name(t_qid, store, cache=cache) or t_qid
        subjects["chronological"].append(
            {
                "heading": t_heading,
                "field": temporal_field,
                "vocabulary": subj_mappings["temporal"],
                "uri": None,
            }
        )
    for mus_qid in get_claim_qids(claims, "P870"):
        mus_heading = _lookup_entity_name(mus_qid, store, cache=cache) or mus_qid
        subjects["medium_of_performance"].append(
            {
                "heading": mus_heading,
                "field": mop_field,
                "vocabulary": subj_mappings["medium_of_performance"],
                "uri": None,
            }
        )

    work_type = "text"
    for q in instance_of:
        if q in creative_map:
            work_type = creative_map[q]
            break

    parsed_year = int(year_str) if (year_str and year_str.isdigit()) else None
    if not cfg.accepts(work_type=work_type, language=language, year=parsed_year):
        return None

    return {
        "source_id": qid,
        "work_type": work_type,
        "source": "wikidata",
        "title": title,
        "subtitle": subtitle,
        "uniform_title": uniform_title,
        "variant_titles": variant_titles,
        "publication_year": year_str,
        "publisher": publisher,
        "publication_place": publication_place,
        "language": language,
        "original_language": original_language,
        "edition": edition_val,
        "extent": extent_val,
        "series_title": series_title,
        "series_numbering": series_numbering,
        "volume_designation": volume_str,
        "classifications": classifications,
        "identifiers": {
            "isbn": isbns,
            "issn": issns,
            "urn": urns,
            "doi": dois,
            "other_ids": other_ids,
        },
        "agents": agents,
        "subjects": subjects,
    }


def decompose_wikidata_entity(
    entity: dict[str, Any],
    store: Optional[WEMIRepository] = None,
    serialization_engine: Optional[WEMISerializationEngine] = None,
    cache: Optional[AuthorLRUCache] = None,
    config: Optional[WikidataIngestConfig] = None,
) -> Optional[
    tuple[
        WorkEntity,
        ExpressionEntity,
        ManifestationEntity,
        list[ItemEntity],
        list[AgentEntity],
        list[WorkRelationship],
        list[IdentifierEntity],
        list[ManifestationExpressionAssociation],
        list[ContributionEntity],
    ]
]:
    """Decompose a raw Wikidata entity dictionary into normalized WEMI domain entities.

    Distinguishes Abstract Works from Editions/Manifestations (P629).
    Hydrates author/creator names from database or LRU cache for O(1) memory consumption.
    """
    cfg = config or _DEFAULT_WIKIDATA_CONFIG
    creative_map = cfg.creative_work_types or _CREATIVE_WORK_MAPPING
    human_types = (
        set(cfg.human_and_entity_types) if cfg.human_and_entity_types else _HUMAN_AND_ENTITY_TYPES
    )

    qid = entity.get("id", "")
    claims = entity.get("claims", {})
    labels = entity.get("labels", {})

    # Check if this is a human/org/place entity — skip bibliographic processing
    instance_of = get_claim_qids(claims, "P31")
    is_human = "Q5" in instance_of
    is_org_or_place = any(k in instance_of for k in human_types - {"Q5"})
    if is_human or is_org_or_place:
        return None

    # Check bibliographic / creative work criteria
    has_bib_ids = "P212" in claims or "P957" in claims or "P3595" in claims
    has_p629 = bool(get_claim_qids(claims, "P629"))
    is_edition = has_p629 or "Q3331189" in instance_of
    is_creative_work = any(k in instance_of for k in creative_map)

    if not (is_edition or is_creative_work or has_bib_ids):
        return None

    # Title extraction
    title = ""
    p1476_val = get_claim_snak_value(claims, "P1476")
    if isinstance(p1476_val, dict):
        title = p1476_val.get("text", "")
    elif isinstance(p1476_val, str):
        title = p1476_val

    if not title:
        for lang_code in WIKIDATA_PREF_LANGS:
            if lang_code in labels:
                title = labels[lang_code].get("value", "")
                if title:
                    break
    if not title:
        return None

    # Subtitle
    subtitle = None
    p1680_val = get_claim_snak_value(claims, "P1680")
    if isinstance(p1680_val, dict):
        subtitle = p1680_val.get("text")
    elif isinstance(p1680_val, str):
        subtitle = p1680_val

    # Publication Year
    pub_year_int: Optional[int] = None
    pub_time = get_claim_snak_value(claims, "P577")
    if isinstance(pub_time, dict) and "time" in pub_time:
        m = re.search(r"\+(\d{4})", pub_time["time"])
        if m:
            pub_year_int = int(m.group(1))
    if not pub_year_int:
        inc_time = get_claim_snak_value(claims, "P571")
        if isinstance(inc_time, dict) and "time" in inc_time:
            m = re.search(r"\+(\d{4})", inc_time["time"])
            if m:
                pub_year_int = int(m.group(1))

    # Publisher
    publisher = extract_claim_qualifier_string(claims, "P123")
    if not publisher:
        pub_qids = get_claim_qids(claims, "P123")
        if pub_qids:
            publisher = _lookup_entity_name(pub_qids[0], store, cache=cache)
    if publisher and is_qid(publisher):
        publisher = None

    # Publication Place
    publication_place = extract_claim_qualifier_string(claims, "P291")
    if not publication_place:
        place_qids = get_claim_qids(claims, "P291")
        if place_qids:
            publication_place = _lookup_entity_name(place_qids[0], store, cache=cache)
    if publication_place and is_qid(publication_place):
        publication_place = None

    # Languages
    language: Optional[str] = None
    lang_qids = get_claim_qids(claims, "P407")
    if lang_qids and lang_qids[0] in WIKIDATA_LANG_MAP:
        language = WIKIDATA_LANG_MAP[lang_qids[0]]

    original_language: Optional[str] = None
    orig_lang_qids = get_claim_qids(claims, "P364")
    if orig_lang_qids and orig_lang_qids[0] in WIKIDATA_LANG_MAP:
        original_language = WIKIDATA_LANG_MAP[orig_lang_qids[0]]
    if not original_language and language:
        original_language = language

    # Work type determined dynamically from configured creative_work_types mapping
    work_type: WorkType = "text"
    for inst_qid in instance_of:
        if inst_qid in creative_map:
            work_type = creative_map[inst_qid]  # type: ignore[assignment]
            break

    # Universal ingestion filter check
    if not cfg.accepts(work_type=work_type, language=language, year=pub_year_int):
        return None

    # Edition statement & extent
    edition_statement: Optional[str] = None
    p393 = get_claim_snak_value(claims, "P393")
    if p393:
        edition_statement = str(p393)

    extent_str: Optional[str] = None
    p1104 = get_claim_snak_value(claims, "P1104")
    if p1104:
        extent_str = f"{p1104} s."

    # Carriers
    carrier: CarrierType = "volume"
    p437_qids = get_claim_qids(claims, "P437")
    if any(k in p437_qids for k in {"Q134900", "Q178659"}):
        carrier = "audio_disc"
    elif any(k in p437_qids for k in {"Q136822", "Q1028781"}):
        carrier = "online_resource"
    elif any(k in p437_qids for k in {"Q251"}):
        carrier = "videodisc"

    # WEMI IDs
    engine = serialization_engine or WEMISerializationEngine()
    agents_map: dict[str, AgentEntity] = {}
    contributions: list[ContributionEntity] = []

    # Creators
    creator_props = (
        ("P50", "aut"),
        ("P86", "cmp"),
        ("P57", "drt"),
        ("P170", "cre"),
        ("P287", "dsr"),
        ("P178", "prg"),
        ("P84", "arc"),
    )
    # Realizers
    realizer_props = (
        ("P655", "trl"),
        ("P110", "ill"),
        ("P98", "edt"),
        ("P175", "prf"),
        ("P345", "nrt"),
    )

    work_qid = get_claim_qids(claims, "P629")[0] if has_p629 else qid
    work_curie = f"wikidata:{work_qid}"
    expr_curie = f"wikidata:{qid}_exp_{language or 'und'}"
    manif_curie = f"wikidata:{qid}"

    primary_creator_name: Optional[str] = None
    for prop, role in creator_props:
        for c_qid in get_claim_qids(claims, prop):
            c_name = _lookup_entity_name(c_qid, store, cache=cache) or c_qid
            if not primary_creator_name and not is_qid(c_name):
                primary_creator_name = c_name
            author_data: Optional[dict[str, Any]] = None
            if cache is not None and c_qid in cache:
                author_data = cache.get(c_qid)
            elif store:
                author_data = store.get_wikidata_author(c_qid)
                if cache is not None:
                    cache.set(c_qid, author_data)
            author_idents: list[IdentifierEntity] = []
            if author_data and author_data.get("identifiers"):
                for s_name, s_val in author_data["identifiers"].items():
                    author_idents.append(
                        IdentifierEntity(
                            entity_type="agent",
                            entity_id=0,
                            source=s_name,
                            source_id=s_val,
                            source_id_normalized=s_val.strip().lower(),
                        )
                    )
            author_quals: list[str] = []
            if author_data:
                author_quals = [
                    q
                    for q in dict.fromkeys(
                        (author_data.get("qualifiers") or [])
                        + (author_data.get("occupations") or [])
                    )
                    if q
                ]
            agent = AgentEntity(
                name=c_name,
                name_normalized=c_name.strip().lower(),
                surname=author_data.get("family_name") if author_data else None,
                birth_year=author_data.get("birth_year") if author_data else None,
                death_year=author_data.get("death_year") if author_data else None,
                source="wikidata",
                source_id=c_qid,
                variant_names=author_data.get("variant_names", []) if author_data else [],
                qualifiers=author_quals,
                identifiers=author_idents,
            )

            agents_map[c_qid] = agent
            contributions.append(
                ContributionEntity(
                    entity_type="work",
                    entity_id=0,
                    entity_curie=work_curie,
                    agent_id=0,
                    agent_curie=f"wikidata:{c_qid}",
                    role=role,
                    is_primary=(primary_creator_name == c_name),
                )
            )

    for prop, role in realizer_props:
        for r_qid in get_claim_qids(claims, prop):
            r_name = _lookup_entity_name(r_qid, store, cache=cache) or r_qid
            author_data = None
            if cache is not None and r_qid in cache:
                author_data = cache.get(r_qid)
            elif store:
                author_data = store.get_wikidata_author(r_qid)
                if cache is not None:
                    cache.set(r_qid, author_data)
            author_idents = []
            if author_data and author_data.get("identifiers"):
                for s_name, s_val in author_data["identifiers"].items():
                    author_idents.append(
                        IdentifierEntity(
                            entity_type="agent",
                            entity_id=0,
                            source=s_name,
                            source_id=s_val,
                            source_id_normalized=s_val.strip().lower(),
                        )
                    )
            author_quals = []
            if author_data:
                author_quals = [
                    q
                    for q in dict.fromkeys(
                        (author_data.get("qualifiers") or [])
                        + (author_data.get("occupations") or [])
                    )
                    if q
                ]
            agent = AgentEntity(
                name=r_name,
                name_normalized=r_name.strip().lower(),
                surname=author_data.get("family_name") if author_data else None,
                birth_year=author_data.get("birth_year") if author_data else None,
                death_year=author_data.get("death_year") if author_data else None,
                source="wikidata",
                source_id=r_qid,
                variant_names=author_data.get("variant_names", []) if author_data else [],
                qualifiers=author_quals,
                identifiers=author_idents,
            )

            agents_map[r_qid] = agent
            contributions.append(
                ContributionEntity(
                    entity_type="expression",
                    entity_id=0,
                    entity_curie=expr_curie,
                    agent_id=0,
                    agent_curie=f"wikidata:{r_qid}",
                    role=role,
                    is_primary=False,
                )
            )

    # Identifiers
    identifiers: list[IdentifierEntity] = [
        IdentifierEntity(
            entity_type="work",
            entity_id=0,
            source="wikidata",
            source_id=work_qid,
            curie=work_curie,
            entity_curie=work_curie,
        )
    ]
    if work_qid != qid:
        identifiers.append(
            IdentifierEntity(
                entity_type="manifestation",
                entity_id=0,
                source="wikidata",
                source_id=qid,
                curie=manif_curie,
                entity_curie=manif_curie,
            )
        )

    id_claims = (
        ("P212", "isbn"),
        ("P957", "isbn"),
        ("P236", "issn"),
        ("P356", "doi"),
        ("P243", "oclc"),
        ("P345", "imdb"),
        ("P8383", "bgg"),
    )
    for prop, scheme in id_claims:
        for stmt in claims.get(prop, []):
            mainsnak = stmt.get("mainsnak", {})
            val = mainsnak.get("datavalue", {}).get("value")
            if val and isinstance(val, str):
                v_clean = val.strip()
                v_norm = re.sub(r"[^0-9a-zA-Z]", "", v_clean).lower()
                target_etype: EntityType = (
                    "manifestation" if scheme in ("isbn", "issn", "doi", "oclc") else "work"
                )
                identifiers.append(
                    IdentifierEntity(
                        entity_type=target_etype,
                        entity_id=0,
                        source=scheme,
                        source_id=v_clean,
                        source_id_normalized=v_norm,
                        curie=f"{scheme}:{v_clean}",
                        entity_curie=manif_curie if target_etype == "manifestation" else work_curie,
                    )
                )

    # Relationships
    relationships: list[WorkRelationship] = []
    rel_claims = (
        ("P144", "adaptation_of"),
        ("P179", "series_member_of"),
        ("P155", "continues"),
        ("P156", "continued_by"),
        ("P527", "has_series_member"),
        ("P361", "series_member_of"),
    )
    for prop, rel_type in rel_claims:
        for target_qid in get_claim_qids(claims, prop):
            relationships.append(
                WorkRelationship(
                    source_work_id=0,
                    target_work_id=0,
                    source_work_curie=work_curie,
                    target_work_curie=f"wikidata:{target_qid}",
                    relationship_type=rel_type,
                )
            )

    work = WorkEntity(
        title=title,
        title_normalized=compute_title_stem(title, lang=original_language),
        original_language=original_language,
        work_type=work_type,
        source="wikidata",
        source_id=work_qid,
    )
    work.serialized_text = engine.render_work(
        work,
        authors=[{"name": primary_creator_name}] if primary_creator_name else [],
    )

    expression = ExpressionEntity(
        work_id=0,
        work_curie=work_curie,
        title=title,
        content_type="audio_music" if work_type == "music" else "text",
        language=language or "und",
        source="wikidata",
        source_id=f"{qid}_{language or 'und'}",
    )
    expression.serialized_text = engine.render_expression(
        expression,
        work_title=work.title,
    )

    manifestation = ManifestationEntity(
        work_id=0,
        work_curie=work_curie,
        title=title,
        subtitle=subtitle,
        title_normalized=compute_title_stem(title, lang=language),
        carrier_type=carrier,
        edition_statement=edition_statement,
        publication_year=pub_year_int,
        publisher=publisher,
        publication_place=publication_place,
        extent=extent_str,
        source="wikidata",
        source_id=qid,
    )
    manifestation.serialized_text = engine.render_manifestation(manifestation)

    associations = [
        ManifestationExpressionAssociation(
            manifestation_id=0,
            expression_id=0,
            manifestation_curie=manif_curie,
            expression_curie=expr_curie,
            is_primary=True,
        )
    ]

    return (
        work,
        expression,
        manifestation,
        [],
        list(agents_map.values()),
        relationships,
        identifiers,
        associations,
        contributions,
    )
