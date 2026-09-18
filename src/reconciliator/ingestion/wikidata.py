"""Two-pass streaming ingestion pipeline for gzipped Wikidata dump latest-all.json.gz.

Pass 1: Pre-scan all human/organization/place entities into SQLite wikidata_authors table.
Pass 2: Stream bibliographic entities, hydrating authors from SQLite for true O(1) memory.
"""

from __future__ import annotations

import gzip
import logging
import os
import re
from pathlib import Path
from typing import Any, Iterator, Optional

import orjson

from reconciliator.config import (
    WikidataIngestConfig,
    get_bundled_defaults,
    get_bundled_record_defaults,
    get_category_default_field_map,
    get_subject_field_category_map,
    get_wikidata_author_properties,
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
    MediumOfPerformance,
    WorkEntity,
    WorkRelationship,
    WorkType,
)
from reconciliator.normalizers.blocking import compute_title_stem
from reconciliator.normalizers.linguistic import fold_label
from reconciliator.progress import ProgressTracker
from reconciliator.repository import WEMIRepository
from reconciliator.template import WEMISerializationEngine
from reconciliator.vocabularies import concept_id_to_uri, format_wikidata_uri

logger = logging.getLogger(__name__)

WIKIDATA_LANG_MAP = dict(get_bundled_defaults().get("wikidata", {}).get("languages") or {})
WIKIDATA_PREF_LANGS: list[str] = list(
    get_bundled_defaults().get("vocabularies", {}).get("preferred_label_languages") or []
)

BIB_INDICATORS = (
    b'"P212"',
    b'"P957"',
    b'"P3595"',
    b'"P213"',
    b'"Q571"',
    b'"Q7725634"',
    b'"Q47461344"',
    b'"Q2085381"',
    b'"P1476"',
)

AUTHOR_INDICATORS = (
    b'"Q5"',
    b'"Q170574"',
    b'"Q2085381"',
    b'"Q4830453"',
    b'"Q43229"',
    b'"Q515"',
    b'"Q6256"',
    b'"P3595"',
    b'"P213"',
    b'"P214"',
    b'"P227"',
    b'"P244"',
    b'"P906"',
    b'"P496"',
    b'"P50"',
    b'"P98"',
    b'"P86"',
)

_HUMAN_AND_ENTITY_TYPES = {"Q5", "Q2085381", "Q4830453", "Q43229", "Q515", "Q6256"}
_BOOK_TYPES = {"Q571", "Q7725634", "Q47461344", "Q3331189"}
_AUTHOR_PROPERTIES = (
    ("P50", "aut"),
    ("P98", "edt"),
    ("P655", "trl"),
    ("P110", "ill"),
    ("P86", "cmp"),
    ("P767", "ctb"),
)


def get_claim_snak_value(claims: dict[str, Any], prop: str) -> Optional[Any]:
    """Extract first value from claims structure."""
    statements = claims.get(prop, [])
    if statements and isinstance(statements, list):
        mainsnak = statements[0].get("mainsnak", {})
        datavalue = mainsnak.get("datavalue", {})
        return datavalue.get("value")
    return None


def get_claim_qids(claims: dict[str, Any], prop: str) -> list[str]:
    """Extract target QIDs from property claims."""
    qids: list[str] = []
    for stmt in claims.get(prop, []):
        mainsnak = stmt.get("mainsnak", {})
        datavalue = mainsnak.get("datavalue", {})
        val = datavalue.get("value")
        if isinstance(val, dict) and "id" in val:
            qids.append(val["id"])
    return qids


def extract_claim_qualifier_string(
    claims: dict[str, Any], prop: str, qual_props: tuple[str, ...] = ("P1810", "P1932")
) -> Optional[str]:
    """Extract string qualifier from property statements (e.g. P1810 'stated as', P1932)."""
    for stmt in claims.get(prop, []):
        quals = stmt.get("qualifiers", {})
        for q_prop in qual_props:
            for q_stmt in quals.get(q_prop, []):
                val = q_stmt.get("datavalue", {}).get("value")
                if isinstance(val, str) and val.strip():
                    return val.strip()
                elif isinstance(val, dict) and "text" in val and str(val["text"]).strip():
                    return str(val["text"]).strip()
    return None


def _has_bib_indicator(line: bytes) -> bool:
    """Fast byte-level pre-filter for lines likely containing bibliographic or author entities."""
    return any(indicator in line for indicator in BIB_INDICATORS)


def _has_author_indicator(line: bytes) -> bool:
    """Fast byte-level pre-filter for lines likely containing author, contributor, or agent entities."""
    return any(indicator in line for indicator in AUTHOR_INDICATORS)


def _extract_author_data(
    entity: dict[str, Any],
    require_authority_id: bool = True,
    authority_properties: tuple[str, ...] | list[str] = (
        "P3595",
        "P213",
        "P214",
        "P227",
        "P244",
        "P906",
        "P496",
    ),
) -> Optional[dict[str, Any]]:
    """Extract author/entity metadata from a Wikidata entity if it matches criteria.

    Returns a dict suitable for insert_wikidata_authors, or None.
    """
    qid = entity.get("id", "")
    claims = entity.get("claims", {})
    labels = entity.get("labels", {})

    instance_of = get_claim_qids(claims, "P31")
    is_human = "Q5" in instance_of
    is_org_or_place = any(k in instance_of for k in _HUMAN_AND_ENTITY_TYPES - {"Q5"})
    has_auth_ids = any(prop in claims for prop in authority_properties)

    if require_authority_id and is_human:
        if not has_auth_ids:
            return None
    elif not (is_human or is_org_or_place or has_auth_ids):
        return None

    name_val = ""
    for lang in WIKIDATA_PREF_LANGS:
        if lang in labels:
            name_val = labels[lang].get("value", "")
            if name_val:
                break
    if not name_val and labels:
        first_label = next(iter(labels.values()), None)
        if isinstance(first_label, dict):
            name_val = first_label.get("value", "")
        elif isinstance(first_label, str):
            name_val = first_label

    fam_name = ""
    fam_qids = get_claim_qids(claims, "P734")
    if fam_qids:
        fam_name = fam_qids[0]

    author_props = get_wikidata_author_properties()
    identifiers: dict[str, str] = {}
    for scheme, prop in author_props.items():
        val = get_claim_snak_value(claims, prop)
        if val is not None:
            val_str = str(val).strip()
            if val_str:
                identifiers[scheme] = val_str

    # Extract birth / death years
    birth_year: Optional[int] = None
    b_val = get_claim_snak_value(claims, "P569")
    if isinstance(b_val, dict) and "time" in b_val:
        m_b = re.search(r"[+-]?(\d{4})", str(b_val["time"]))
        if m_b:
            birth_year = int(m_b.group(1))

    death_year: Optional[int] = None
    d_val = get_claim_snak_value(claims, "P570")
    if isinstance(d_val, dict) and "time" in d_val:
        m_d = re.search(r"[+-]?(\d{4})", str(d_val["time"]))
        if m_d:
            death_year = int(m_d.group(1))

    occupations = get_claim_qids(claims, "P106")
    fields_of_work = get_claim_qids(claims, "P101")
    affiliations = get_claim_qids(claims, "P108") + get_claim_qids(claims, "P1416")

    # Extract aliases / variant names
    aliases_list: list[str] = []
    aliases_dict = entity.get("aliases", {})
    if isinstance(aliases_dict, dict):
        for lang in WIKIDATA_PREF_LANGS:
            if lang in aliases_dict:
                for a_item in aliases_dict[lang]:
                    val = a_item.get("value") if isinstance(a_item, dict) else None
                    if val and val not in aliases_list:
                        aliases_list.append(val)

    # Extract descriptions (qualifier)
    desc_val: Optional[str] = None
    descriptions = entity.get("descriptions", {})
    if isinstance(descriptions, dict):
        for lang in WIKIDATA_PREF_LANGS:
            if lang in descriptions:
                d_item = descriptions[lang]
                if isinstance(d_item, dict) and d_item.get("value"):
                    desc_val = d_item["value"]
                    break

    # Extract qualifiers: descriptions
    qualifiers: list[str] = []
    if desc_val:
        qualifiers.append(desc_val)

    if name_val or has_auth_ids or identifiers:
        return {
            "qid": qid,
            "name": name_val,
            "family_name": fam_name,
            "identifiers": identifiers,
            "birth_year": birth_year,
            "death_year": death_year,
            "occupations": occupations,
            "fields_of_work": fields_of_work,
            "affiliations": affiliations,
            "qualifiers": qualifiers,
            "variant_names": aliases_list,
        }
    return None


def _lookup_entity_name(
    qid: str,
    store: Optional[WEMIRepository],
) -> Optional[str]:
    """Look up an entity name from SQLite agents table by QID."""
    if not store or not qid:
        return None
    author = store.get_wikidata_author(qid)
    if author:
        name = author.get("name")
        if name and not re.match(r"^Q\d+$", str(name)):
            return str(name)
    return None


def parse_wikidata_entity(
    entity: dict[str, Any],
    store: Optional[WEMIRepository] = None,
) -> Optional[dict[str, Any]]:
    """Parse a Wikidata entity into raw bibliographic dictionary format.

    Author/publisher/subject names are hydrated from the SQLite agents/identifiers tables
    via the store parameter, achieving O(1) memory usage.
    """
    qid = entity.get("id", "")
    claims = entity.get("claims", {})
    labels = entity.get("labels", {})

    # Check if this is a human/org/place entity — skip bibliographic processing
    instance_of = get_claim_qids(claims, "P31")
    is_human = "Q5" in instance_of
    is_org_or_place = any(k in instance_of for k in _HUMAN_AND_ENTITY_TYPES - {"Q5"})

    if is_human or is_org_or_place:
        return None

    # Check if bibliographic work / book
    is_book = any(k in instance_of for k in _BOOK_TYPES)
    has_bib_ids = "P212" in claims or "P957" in claims or "P3595" in claims

    if not (is_book or has_bib_ids):
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

    # Publisher: first check qualifier (P1810 / P1932 'stated as'), then SQLite lookup
    publisher = extract_claim_qualifier_string(claims, "P123")
    if not publisher:
        pub_qids = get_claim_qids(claims, "P123")
        if pub_qids:
            publisher = _lookup_entity_name(pub_qids[0], store)
    if publisher and re.match(r"^Q\d+$", publisher.strip()):
        publisher = None

    # Publication Place: first check qualifier, then SQLite lookup
    publication_place = extract_claim_qualifier_string(claims, "P291")
    if not publication_place:
        place_qids = get_claim_qids(claims, "P291")
        if place_qids:
            publication_place = _lookup_entity_name(place_qids[0], store)
    if publication_place and re.match(r"^Q\d+$", publication_place.strip()):
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
            series_title = _lookup_entity_name(series_qids[0], store)
    if series_title and re.match(r"^Q\d+$", series_title.strip()):
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

    # Agents — hydrate from SQLite
    agents: list[dict[str, Any]] = []
    for prop, role in _AUTHOR_PROPERTIES:
        for a_qid in get_claim_qids(claims, prop):
            author_info: dict[str, Any] = {}
            if store:
                author_info = store.get_wikidata_author(a_qid) or {}
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

    # Subjects — hydrate names from SQLite
    cat_defaults = get_category_default_field_map()
    field_to_cat = get_subject_field_category_map()
    subjects: dict[str, list[dict[str, Any]]] = {cat: [] for cat in field_to_cat.values()}
    topical_field = cat_defaults["topical"]
    genre_field = cat_defaults["genre_form"]
    geo_field = cat_defaults["geographic"]
    mop_field = cat_defaults["medium_of_performance"]

    subj_mappings = dict(
        get_bundled_defaults().get("wikidata", {}).get("subject_mappings")
        or {
            "topical": "yso",
            "genre_form": "slm",
            "geographic": "yso",
            "medium_of_performance": "seko",
        }
    )

    for s_qid in get_claim_qids(claims, "P921"):
        s_heading = _lookup_entity_name(s_qid, store) or s_qid
        subjects["topical"].append(
            {
                "heading": s_heading,
                "field": topical_field,
                "vocabulary": subj_mappings.get("topical", "yso"),
                "uri": None,
            }
        )
    for g_qid in get_claim_qids(claims, "P136"):
        g_heading = _lookup_entity_name(g_qid, store) or g_qid
        subjects["genre_form"].append(
            {
                "heading": g_heading,
                "field": genre_field,
                "vocabulary": subj_mappings.get("genre_form", "slm"),
                "uri": None,
            }
        )
    for geo_qid in get_claim_qids(claims, "P840"):
        geo_heading = _lookup_entity_name(geo_qid, store) or geo_qid
        subjects["geographic"].append(
            {
                "heading": geo_heading,
                "field": geo_field,
                "vocabulary": subj_mappings.get("geographic", "yso"),
                "uri": None,
            }
        )
    for mus_qid in get_claim_qids(claims, "P870"):
        mus_heading = _lookup_entity_name(mus_qid, store) or mus_qid
        subjects["medium_of_performance"].append(
            {
                "heading": mus_heading,
                "field": mop_field,
                "vocabulary": subj_mappings.get("medium_of_performance", "seko"),
                "uri": None,
            }
        )

    return {
        "source_id": qid,
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


def _prescan_authors(
    path: Path,
    store: Optional[WEMIRepository],
    tracker: Optional[ProgressTracker],
    config: Optional[WikidataIngestConfig] = None,
) -> int:
    """Pass 1: Stream the Wikidata dump and persist all author/org/place entities to SQLite.

    Processes line-by-line in O(1) memory — no in-memory author dict is maintained.
    Returns the number of author entities persisted.
    """
    author_batch: list[dict[str, Any]] = []
    total_authors = 0
    require_auth_id = True if config is None else config.require_author_authority_id
    auth_props = (
        ("P3595", "P213", "P214", "P227", "P244", "P906", "P496")
        if config is None
        else tuple(config.author_authority_properties)
    )

    with gzip.open(path, "rb") as f:
        for line in f:
            if not _has_author_indicator(line):
                continue

            clean_line = line.strip().rstrip(b",")
            if not clean_line or clean_line in (b"[", b"]"):
                continue

            try:
                entity = orjson.loads(clean_line)
            except Exception:
                continue

            author_data = _extract_author_data(
                entity,
                require_authority_id=require_auth_id,
                authority_properties=auth_props,
            )
            if author_data:
                author_batch.append(author_data)
                if len(author_batch) >= 500:
                    if store is not None:
                        store.insert_wikidata_authors(author_batch)
                    total_authors += len(author_batch)
                    author_batch.clear()

            if tracker and hasattr(f, "fileobj") and f.fileobj and hasattr(f.fileobj, "tell"):
                curr_bytes = int(getattr(f.fileobj, "tell")())
                tracker.update_progress(bytes_processed=curr_bytes, items_increment=0)

    # Flush remaining batch
    if author_batch and store is not None:
        store.insert_wikidata_authors(author_batch)
        total_authors += len(author_batch)
        author_batch.clear()

    logger.info("Pass 1 complete: %d author entities persisted to SQLite", total_authors)
    return total_authors


def _stream_bibliographic_entities(
    path: Path,
    store: Optional[WEMIRepository],
    tracker: Optional[ProgressTracker],
) -> Iterator[dict[str, Any]]:
    """Pass 2: Stream the Wikidata dump and yield bibliographic entities.

    Author names are hydrated from the SQLite agents/identifiers tables (populated in Pass 1),
    achieving true O(1) memory consumption.
    """
    with gzip.open(path, "rb") as f:
        for line in f:
            if not _has_bib_indicator(line):
                continue

            clean_line = line.strip().rstrip(b",")
            if not clean_line or clean_line in (b"[", b"]"):
                continue

            try:
                entity = orjson.loads(clean_line)
            except Exception:
                continue

            parsed = parse_wikidata_entity(entity, store=store)
            if parsed:
                yield parsed

            if tracker and hasattr(f, "fileobj") and f.fileobj and hasattr(f.fileobj, "tell"):
                curr_bytes = int(getattr(f.fileobj, "tell")())
                tracker.update_progress(
                    bytes_processed=curr_bytes,
                    items_increment=1 if parsed else 0,
                )


def stream_wikidata_dump(
    dump_path: Path | str,
    store: Optional[WEMIRepository] = None,
    tracker: Optional[ProgressTracker] = None,
    config: Optional[WikidataIngestConfig] = None,
) -> Iterator[dict[str, Any]]:
    """Two-pass Wikidata streaming: pre-scan authors into SQLite, then extract books.

    Pass 1: Scans the entire dump to collect all human/organization/place entities
            and persists them to the SQLite wikidata_authors table.
    Pass 2: Scans the dump again to process bibliographic entities, hydrating
            author/publisher/subject names from SQLite instead of in-memory state.

    This achieves true O(1) memory consumption regardless of dump size.
    """
    path = Path(dump_path)
    if not path.exists():
        raise FileNotFoundError(f"Wikidata dump not found: {path}")

    total_bytes = os.path.getsize(path)

    # Pass 1: Pre-scan all human/org entities into wikidata_authors
    logger.info("Pass 1/2: Pre-scanning author entities into SQLite...")
    if tracker:
        tracker.start_task(stage="wikidata_prescan", bytes_total=total_bytes)
    _prescan_authors(path, store, tracker, config=config)
    if tracker:
        tracker.complete_task(metadata={"stage": "prescan"})

    # Pass 2: Stream bibliographic entities, hydrating authors from SQLite
    logger.info("Pass 2/2: Streaming bibliographic entities...")
    if tracker:
        tracker.start_task(stage="streaming_wikidata", bytes_total=total_bytes)
    yield from _stream_bibliographic_entities(path, store, tracker)


def decompose_wikidata_entity(
    entity: dict[str, Any],
    store: Optional[WEMIRepository] = None,
    serialization_engine: Optional[WEMISerializationEngine] = None,
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
        list[MediumOfPerformance],
    ]
]:
    """Decompose a raw Wikidata entity dictionary into normalized WEMI domain entities.

    Distinguishes Abstract Works from Editions/Manifestations (P629).
    Hydrates author/creator names from SQLite for O(1) memory consumption.
    """
    qid = entity.get("id", "")
    claims = entity.get("claims", {})
    labels = entity.get("labels", {})

    # Check if this is a human/org/place entity — skip bibliographic processing
    instance_of = get_claim_qids(claims, "P31")
    is_human = "Q5" in instance_of
    is_org_or_place = any(k in instance_of for k in _HUMAN_AND_ENTITY_TYPES - {"Q5"})
    if is_human or is_org_or_place:
        return None

    # Check bibliographic / creative work criteria
    is_book = any(k in instance_of for k in _BOOK_TYPES)
    has_bib_ids = "P212" in claims or "P957" in claims or "P3595" in claims
    has_p629 = bool(get_claim_qids(claims, "P629"))
    is_edition = has_p629 or "Q3331189" in instance_of
    is_creative_work = is_book or any(
        k in instance_of
        for k in {
            "Q7725634",
            "Q571",
            "Q47461344",
            "Q2188189",
            "Q11424",
            "Q40063",
            "Q1004",
            "Q7889",
            "Q131436",
        }
    )

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
            publisher = _lookup_entity_name(pub_qids[0], store)
    if publisher and re.match(r"^Q\d+$", publisher.strip()):
        publisher = None

    # Publication Place
    publication_place = extract_claim_qualifier_string(claims, "P291")
    if not publication_place:
        place_qids = get_claim_qids(claims, "P291")
        if place_qids:
            publication_place = _lookup_entity_name(place_qids[0], store)
    if publication_place and re.match(r"^Q\d+$", publication_place.strip()):
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

    # Work type
    work_type: WorkType = "text"
    if any(k in instance_of for k in {"Q2188189", "Q7366", "Q105543609", "Q134556"}):
        work_type = "music"
    elif any(k in instance_of for k in {"Q11424", "Q5398426"}):
        work_type = "moving_image"
    elif "Q40063" in instance_of:
        work_type = "drama"
    elif any(k in instance_of for k in {"Q1004", "Q8274"}):
        work_type = "comic"
    elif any(k in instance_of for k in {"Q131569", "Q3284440"}):
        work_type = "statute"
    elif "Q7889" in instance_of:
        work_type = "game"
    elif "Q131436" in instance_of:
        work_type = "board_game"

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
            c_name = _lookup_entity_name(c_qid, store) or c_qid
            if not primary_creator_name and not re.match(r"^Q\d+$", c_name):
                primary_creator_name = c_name
            author_data = store.get_wikidata_author(c_qid) if store else None
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
            r_name = _lookup_entity_name(r_qid, store) or r_qid
            author_data = store.get_wikidata_author(r_qid) if store else None
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
        [],
        contributions,
    )


def ingest_wikidata_wemi_pipeline(
    config: WikidataIngestConfig,
    wemi_repo: WEMIRepository,
    tracker: Optional[ProgressTracker] = None,
) -> int:
    """Run two-pass streaming Wikidata ingestion landing into WEMIRepository."""
    path = Path(config.dump_path)
    if not path.exists():
        raise FileNotFoundError(f"Wikidata dump not found: {path}")

    total_bytes = os.path.getsize(path)

    # Pass 1: Scan author entities into SQLite wikidata_authors & agents
    logger.info("Pass 1/2: Pre-scanning author entities into SQLite...")
    if tracker:
        tracker.start_task(stage="wikidata_prescan", bytes_total=total_bytes)
    _prescan_authors(path, wemi_repo, tracker, config=config)
    if tracker:
        tracker.complete_task(metadata={"stage": "prescan"})

    if config.pass1_only:
        return 0

    # Pass 2: Stream bibliographic entities and insert into WEMIRepository
    logger.info("Pass 2/2: Decomposing bibliographic entities into WEMI...")
    if tracker:
        tracker.start_task(stage="wikidata_wemi", bytes_total=total_bytes)

    total_ingested = 0
    engine = WEMISerializationEngine()
    works_batch = []
    expressions_batch = []
    manifestations_batch = []
    items_batch = []
    agents_batch = []
    relationships_batch = []
    identifiers_batch = []
    associations_batch = []
    mediums_batch = []
    contributions_batch = []

    def _flush_batch() -> None:
        nonlocal total_ingested
        if works_batch:
            wemi_repo.insert_wemi_batch(
                works=works_batch,
                expressions=expressions_batch,
                manifestations=manifestations_batch,
                items=items_batch,
                agents=agents_batch,
                relationships=relationships_batch,
                identifiers=identifiers_batch,
                manifestation_expressions=associations_batch,
                mediums_of_performance=mediums_batch,
                contributions=contributions_batch,
            )
            total_ingested += len(works_batch)
            works_batch.clear()
            expressions_batch.clear()
            manifestations_batch.clear()
            items_batch.clear()
            agents_batch.clear()
            relationships_batch.clear()
            identifiers_batch.clear()
            associations_batch.clear()
            mediums_batch.clear()
            contributions_batch.clear()
            if tracker:
                tracker.update_progress(stage="wikidata_wemi", items_processed=total_ingested)

    with gzip.open(path, "rb") as f:
        for line in f:
            if not _has_bib_indicator(line):
                continue
            clean_line = line.strip().rstrip(b",")
            if not clean_line or clean_line in (b"[", b"]"):
                continue
            try:
                entity = orjson.loads(clean_line)
            except Exception:
                continue

            decomp = decompose_wikidata_entity(entity, store=wemi_repo, serialization_engine=engine)
            if decomp:
                w, e, m, itms, agts, rels, idents, assoc, mops, contribs = decomp
                works_batch.append(w)
                expressions_batch.append(e)
                manifestations_batch.append(m)
                items_batch.extend(itms)
                agents_batch.extend(agts)
                relationships_batch.extend(rels)
                identifiers_batch.extend(idents)
                associations_batch.extend(assoc)
                mediums_batch.extend(mops)
                contributions_batch.extend(contribs)

                if len(works_batch) >= config.batch_size:
                    _flush_batch()

    _flush_batch()

    if tracker:
        tracker.complete_task(metadata={"total_ingested": total_ingested})

    return total_ingested


def ingest_wikidata_pipeline(
    config: WikidataIngestConfig,
    store: WEMIRepository,
    tracker: Optional[ProgressTracker] = None,
) -> int:
    """Run streaming Wikidata dump ingestion into WEMI repository."""
    return ingest_wikidata_wemi_pipeline(config=config, wemi_repo=store, tracker=tracker)


def extract_wikidata_yso_aliases(entity: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract alias records from a Wikidata entity with property P2347 (YSO ID)."""
    claims = entity.get("claims", {})
    if "P2347" not in claims:
        return []

    qid = entity.get("id", "")
    yso_id = get_claim_snak_value(claims, "P2347")
    if not yso_id:
        return []

    yso_str = str(yso_id).strip()
    yso_clean = yso_str.split("/")[-1] if "/" in yso_str else yso_str
    if yso_clean and not yso_clean.startswith("p"):
        yso_clean = "p" + yso_clean
    concept_id = yso_clean or qid
    concept_vocab = "yso" if yso_clean else "wikidata"

    instance_of = get_claim_qids(claims, "P31")
    is_place = any(
        k in instance_of for k in ("Q515", "Q6256", "Q532", "Q15284", "Q486970", "Q1048835")
    )
    cat_defaults = get_category_default_field_map()
    target_field = cat_defaults["geographic"] if is_place else cat_defaults["topical"]

    aliases: list[dict[str, Any]] = []
    labels = entity.get("labels", {})
    for lang, l_data in labels.items():
        val = l_data.get("value", "") if isinstance(l_data, dict) else str(l_data)
        if val and val.strip():
            folded = fold_label(val)
            if folded:
                aliases.append(
                    {
                        "folded_label": folded,
                        "raw_label": val.strip(),
                        "lang": lang,
                        "concept_id": concept_id,
                        "vocabulary": concept_vocab,
                        "target_field": target_field,
                        "is_preferred": 1,
                        "source": f"wikidata:{qid}" if qid else "wikidata",
                    }
                )

    all_aliases = entity.get("aliases", {})
    for lang, a_list in all_aliases.items():
        if isinstance(a_list, list):
            for a_item in a_list:
                val = a_item.get("value", "") if isinstance(a_item, dict) else str(a_item)
                if val and val.strip():
                    folded = fold_label(val)
                    if folded:
                        aliases.append(
                            {
                                "folded_label": folded,
                                "raw_label": val.strip(),
                                "lang": lang,
                                "concept_id": concept_id,
                                "vocabulary": concept_vocab,
                                "target_field": target_field,
                                "is_preferred": 0,
                                "source": f"wikidata:{qid}" if qid else "wikidata",
                            }
                        )

    return aliases
