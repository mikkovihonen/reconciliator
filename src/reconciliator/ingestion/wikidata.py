"""Zero-RAM streaming ingestion pipeline for gzipped Wikidata dump latest-all.json.gz."""

from __future__ import annotations

import gzip
import os
import re
from pathlib import Path
from typing import Any, Iterator, Optional

import orjson

from reconciliator.config import WikidataIngestConfig
from reconciliator.embedder import SentenceEmbedder
from reconciliator.models import BibliographicRecord
from reconciliator.normalizers.linguistic import fold_label
from reconciliator.normalizers.record import RecordNormalizer
from reconciliator.progress import ProgressTracker
from reconciliator.repository import AuthorityStore

WIKIDATA_LANG_MAP = {
    "Q1412": "fin",
    "Q9027": "swe",
    "Q1860": "eng",
    "Q188": "ger",
    "Q150": "fre",
    "Q7737": "rus",
    "Q25167": "sme",
    "Q33454": "smn",
    "Q33987": "sms",
    "Q9056": "est",
    "Q25258": "dan",
    "Q9043": "nor",
}

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


def parse_wikidata_entity(
    entity: dict[str, Any], author_cache: dict[str, dict[str, Any]]
) -> Optional[dict[str, Any]]:
    """Parse a Wikidata entity into raw bibliographic dictionary format."""
    qid = entity.get("id", "")
    claims = entity.get("claims", {})
    labels = entity.get("labels", {})

    # Check if author or entity (organization, publisher, place): cache if contains identifiers or relevant type
    instance_of = get_claim_qids(claims, "P31")
    is_human = "Q5" in instance_of
    is_org_or_place = any(
        k in instance_of for k in ("Q2085381", "Q4830453", "Q43229", "Q515", "Q6256")
    )
    has_auth_ids = "P3595" in claims or "P213" in claims or "P214" in claims

    if is_human or is_org_or_place or has_auth_ids:
        name_val = ""
        for lang in ("fi", "sv", "en"):
            if lang in labels:
                name_val = labels[lang].get("value", "")
                if name_val:
                    break
        fam_name = ""
        fam_qids = get_claim_qids(claims, "P734")
        if fam_qids:
            fam_name = fam_qids[0]

        asteri_id = get_claim_snak_value(claims, "P3595")
        isni = get_claim_snak_value(claims, "P213")
        viaf = get_claim_snak_value(claims, "P214")

        if name_val or asteri_id or isni or viaf:
            author_cache[qid] = {
                "qid": qid,
                "name": name_val,
                "family_name": fam_name,
                "asteri_id": asteri_id,
                "isni": isni,
                "viaf": viaf,
            }

    # Human authors and organizations/places are never bibliographic book records
    if is_human or is_org_or_place:
        return None

    # Check if bibliographic work / book
    is_book = any(k in instance_of for k in ("Q571", "Q7725634", "Q47461344", "Q3331189"))
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
        for lang in ("fi", "sv", "en"):
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
    for lang in ("fi", "sv", "en"):
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

    # Publisher: first check qualifier (P1810 / P1932 'stated as'), then cached entity name
    publisher = extract_claim_qualifier_string(claims, "P123")
    if not publisher:
        pub_qids = get_claim_qids(claims, "P123")
        if pub_qids and pub_qids[0] in author_cache:
            p_name = author_cache[pub_qids[0]].get("name")
            if p_name and not re.match(r"^Q\d+$", p_name):
                publisher = p_name
    if publisher and re.match(r"^Q\d+$", publisher.strip()):
        publisher = None

    # Publication Place: first check qualifier, then cached entity name
    publication_place = extract_claim_qualifier_string(claims, "P291")
    if not publication_place:
        place_qids = get_claim_qids(claims, "P291")
        if place_qids and place_qids[0] in author_cache:
            pl_name = author_cache[place_qids[0]].get("name")
            if pl_name and not re.match(r"^Q\d+$", pl_name):
                publication_place = pl_name
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
        if series_qids and series_qids[0] in author_cache:
            s_name = author_cache[series_qids[0]].get("name")
            if s_name and not re.match(r"^Q\d+$", s_name):
                series_title = s_name
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
    isbns: list[str] = []
    for stmt in claims.get("P212", []):
        val = stmt.get("mainsnak", {}).get("datavalue", {}).get("value")
        if val:
            isbns.append(str(val))
    for stmt in claims.get("P957", []):
        val = stmt.get("mainsnak", {}).get("datavalue", {}).get("value")
        if val:
            isbns.append(str(val))

    issns: list[str] = []
    for stmt in claims.get("P236", []):
        val = stmt.get("mainsnak", {}).get("datavalue", {}).get("value")
        if val:
            issns.append(str(val))

    urns: list[str] = []
    p1184 = get_claim_snak_value(claims, "P1184")
    if p1184:
        urns.append(str(p1184))

    other_ids: list[str] = [qid]
    p356 = get_claim_snak_value(claims, "P356")
    if p356:
        other_ids.append(f"doi:{p356}")

    # Agents
    agents: list[dict[str, Any]] = []
    for prop, role in (
        ("P50", "aut"),
        ("P98", "edt"),
        ("P655", "trl"),
        ("P110", "ill"),
        ("P86", "cmp"),
        ("P767", "ctb"),
    ):
        for a_qid in get_claim_qids(claims, prop):
            author_info = author_cache.get(a_qid, {})
            qual_name = extract_claim_qualifier_string(claims, prop)
            name = author_info.get("name") or qual_name or a_qid
            u0: list[str] = []
            if author_info.get("asteri_id"):
                u0.append(f"https://urn.fi/URN:NBN:fi:au:pn:{author_info['asteri_id']}")
            if author_info.get("isni"):
                u0.append(f"http://isni.org/isni/{author_info['isni']}")
            if author_info.get("viaf"):
                u0.append(f"http://viaf.org/viaf/{author_info['viaf']}")

            u1 = [f"http://www.wikidata.org/entity/{a_qid}"]

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

    # Subjects
    subjects: dict[str, list[dict[str, Any]]] = {
        "topical": [],
        "genre_form": [],
        "geographic": [],
        "medium_of_performance": [],
    }
    for s_qid in get_claim_qids(claims, "P921"):
        s_heading = author_cache.get(s_qid, {}).get("name") or s_qid
        subjects["topical"].append(
            {"heading": s_heading, "field": "650", "vocabulary": "yso", "uri": None}
        )
    for g_qid in get_claim_qids(claims, "P136"):
        g_heading = author_cache.get(g_qid, {}).get("name") or g_qid
        subjects["genre_form"].append(
            {"heading": g_heading, "field": "655", "vocabulary": "slm", "uri": None}
        )
    for geo_qid in get_claim_qids(claims, "P840"):
        geo_heading = author_cache.get(geo_qid, {}).get("name") or geo_qid
        subjects["geographic"].append(
            {"heading": geo_heading, "field": "651", "vocabulary": "yso", "uri": None}
        )
    for mus_qid in get_claim_qids(claims, "P870"):
        mus_heading = author_cache.get(mus_qid, {}).get("name") or mus_qid
        subjects["medium_of_performance"].append(
            {"heading": mus_heading, "field": "382", "vocabulary": "seko", "uri": None}
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
            "other_ids": other_ids,
        },
        "agents": agents,
        "subjects": subjects,
    }


def stream_wikidata_dump(
    dump_path: Path | str,
    store: Optional[AuthorityStore] = None,
    tracker: Optional[ProgressTracker] = None,
) -> Iterator[dict[str, Any]]:
    """Stream Wikidata gzipped JSON dump line by line in constant memory with SQLite author persistence."""
    path = Path(dump_path)
    if not path.exists():
        raise FileNotFoundError(f"Wikidata dump not found: {path}")

    total_bytes = os.path.getsize(path)
    if tracker:
        tracker.start_task(stage="streaming_wikidata", bytes_total=total_bytes)

    author_cache: dict[str, dict[str, Any]] = {}
    author_batch: list[dict[str, Any]] = []

    with gzip.open(path, "rb") as f:
        for line in f:
            # Fast line pre-filtering
            if not any(indicator in line for indicator in BIB_INDICATORS):
                continue

            clean_line = line.strip().rstrip(b",")
            if not clean_line or clean_line in (b"[", b"]"):
                continue

            try:
                entity = orjson.loads(clean_line)
            except Exception:
                continue

            qid = entity.get("id", "")
            prev_cache_len = len(author_cache)
            parsed = parse_wikidata_entity(entity, author_cache)

            # Check if an author was cached
            if len(author_cache) > prev_cache_len and qid in author_cache:
                author_batch.append(author_cache[qid])
                if len(author_batch) >= 500:
                    if store is not None:
                        store.insert_wikidata_authors(author_batch)
                    author_batch.clear()

            if parsed:
                yield parsed

            if tracker and hasattr(f, "fileobj") and f.fileobj and hasattr(f.fileobj, "tell"):
                curr_bytes = int(getattr(f.fileobj, "tell")())
                tracker.update_progress(
                    bytes_processed=curr_bytes,
                    items_increment=1,
                )

        if author_batch and store is not None:
            store.insert_wikidata_authors(author_batch)
            author_batch.clear()


def ingest_wikidata_pipeline(
    config: WikidataIngestConfig,
    store: AuthorityStore,
    normalizer: RecordNormalizer,
    embedder: SentenceEmbedder,
    tracker: Optional[ProgressTracker] = None,
) -> int:
    """Run streaming Wikidata dump ingestion with batch embedding and indexing."""
    batch: list[BibliographicRecord] = []
    total_ingested = 0

    for raw_data in stream_wikidata_dump(config.dump_path, store=store, tracker=tracker):
        record = normalizer.normalize(raw_data)
        batch.append(record)

        if len(batch) >= config.batch_size:
            vectors = embedder.embed_records(batch)
            store.insert_records_batch(batch, vectors=vectors)
            total_ingested += len(batch)
            batch.clear()

            if tracker:
                tracker.update_progress(stage="indexing", items_processed=total_ingested)

    if batch:
        vectors = embedder.embed_records(batch)
        store.insert_records_batch(batch, vectors=vectors)
        total_ingested += len(batch)
        batch.clear()

    store.save_index(
        metadata={
            "model_name": embedder.config.model_name,
            "dimension": embedder.dimension,
            "template_hash": embedder.template_hash,
        }
    )

    if tracker:
        tracker.complete_task(metadata={"records_ingested": total_ingested})

    return total_ingested


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
    if not yso_str.startswith("http"):
        yso_uri = (
            f"http://www.yso.fi/onto/yso/{yso_str if yso_str.startswith('p') else 'p' + yso_str}"
        )
    else:
        yso_uri = yso_str

    instance_of = get_claim_qids(claims, "P31")
    is_place = any(
        k in instance_of for k in ("Q515", "Q6256", "Q532", "Q15284", "Q486970", "Q1048835")
    )
    target_field = "651" if is_place else "650"

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
                        "concept_uri": yso_uri,
                        "wikidata_qid": qid,
                        "vocabulary": "yso",
                        "target_field": target_field,
                        "is_preferred": 1,
                        "source": "wikidata",
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
                                "concept_uri": yso_uri,
                                "wikidata_qid": qid,
                                "vocabulary": "yso",
                                "target_field": target_field,
                                "is_preferred": 0,
                                "source": "wikidata",
                            }
                        )

    return aliases
