"""Zero-RAM streaming ingestion pipeline for gzipped Wikidata dump latest-all.json.gz."""

from __future__ import annotations

import gzip
import os
from pathlib import Path
import re
from typing import Any, Iterator, Optional
import orjson

from reconciliator.config import WikidataIngestConfig
from reconciliator.embedder import SentenceEmbedder
from reconciliator.models import BibliographicRecord
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
    b'"Q571"',
    b'"Q7725634"',
    b'"Q47461344"',
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


def parse_wikidata_entity(
    entity: dict[str, Any], author_cache: dict[str, dict[str, Any]]
) -> Optional[dict[str, Any]]:
    """Parse a Wikidata entity into raw bibliographic dictionary format."""
    qid = entity.get("id", "")
    claims = entity.get("claims", {})
    labels = entity.get("labels", {})

    # Check if author entity: cache if contains identifier claims
    is_human = "Q5" in get_claim_qids(claims, "P31")
    if is_human or "P3595" in claims or "P213" in claims or "P214" in claims:
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

        author_cache[qid] = {
            "name": name_val,
            "family_name": fam_name,
            "asteri_id": asteri_id,
            "isni": isni,
            "viaf": viaf,
        }

    # Check if bibliographic work / book
    instance_of = get_claim_qids(claims, "P31")
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

    # Publisher
    publisher = None
    pub_qids = get_claim_qids(claims, "P123")
    if pub_qids:
        publisher = pub_qids[0]  # Or label if cached

    # Language
    language = None
    lang_qids = get_claim_qids(claims, "P407")
    if lang_qids and lang_qids[0] in WIKIDATA_LANG_MAP:
        language = WIKIDATA_LANG_MAP[lang_qids[0]]

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
    for prop, role in (("P50", "aut"), ("P98", "edt"), ("P655", "trl"), ("P110", "ill")):
        for a_qid in get_claim_qids(claims, prop):
            author_info = author_cache.get(a_qid, {})
            name = author_info.get("name") or a_qid
            u0: list[str] = []
            if author_info.get("asteri_id"):
                u0.append(f"https://urn.fi/URN:NBN:fi:au:pn:{author_info['asteri_id']}")
            if author_info.get("isni"):
                u0.append(f"http://isni.org/isni/{author_info['isni']}")
            if author_info.get("viaf"):
                u0.append(f"http://viaf.org/viaf/{author_info['viaf']}")

            u1 = [f"http://www.wikidata.org/entity/{a_qid}"]

            agents.append({
                "name": name,
                "is_personal": True,
                "role": role,
                "uri_subfield_0": u0,
                "uri_subfield_1": u1,
            })

    # Subjects
    subjects: dict[str, list[dict[str, Any]]] = {
        "topical": [],
        "genre_form": [],
        "geographic": [],
        "medium_of_performance": [],
    }
    for s_qid in get_claim_qids(claims, "P921"):
        subjects["topical"].append({"heading": s_qid, "field": "650", "vocabulary": "yso", "uri": None})
    for g_qid in get_claim_qids(claims, "P136"):
        subjects["genre_form"].append({"heading": g_qid, "field": "655", "vocabulary": "slm", "uri": None})

    return {
        "source_id": qid,
        "source": "wikidata",
        "title": title,
        "subtitle": subtitle,
        "publication_year": year_str,
        "publisher": publisher,
        "language": language,
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
    tracker: Optional[ProgressTracker] = None,
) -> Iterator[dict[str, Any]]:
    """Stream Wikidata gzipped JSON dump line by line in constant memory."""
    path = Path(dump_path)
    if not path.exists():
        raise FileNotFoundError(f"Wikidata dump not found: {path}")

    total_bytes = os.path.getsize(path)
    if tracker:
        tracker.start_task(stage="streaming_wikidata", bytes_total=total_bytes)

    author_cache: dict[str, dict[str, Any]] = {}

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

            parsed = parse_wikidata_entity(entity, author_cache)
            if parsed:
                yield parsed

            if tracker and hasattr(f, "fileobj") and f.fileobj:
                curr_bytes = f.fileobj.tell()
                tracker.update_progress(
                    bytes_processed=curr_bytes,
                    items_increment=1,
                )


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

    for raw_data in stream_wikidata_dump(config.dump_path, tracker=tracker):
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

