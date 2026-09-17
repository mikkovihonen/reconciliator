"""Loader, converter, and serializer for NatLibFi/BIBRA publication metadata."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterator, Optional

from lxml import etree
from pydantic import BaseModel, ConfigDict, Field

from reconciliator.curie import parse_curie
from reconciliator.matcher import ReconciliationResult
from reconciliator.models import BibliographicRecord


class BibraPublicationMetadata(BaseModel):
    """Pydantic model representing NatLibFi/BIBRA PublicationMetadata extraction schema."""

    model_config = ConfigDict(populate_by_name=True)

    language: Optional[str] = None
    title: Optional[str] = None
    alt_title: Optional[str] = None
    creator: list[str] = Field(default_factory=list)
    year: Optional[str] = None
    publisher: list[str] = Field(default_factory=list)
    doi: Optional[str] = None
    e_isbn: list[str] = Field(default_factory=list, alias="e-isbn")
    p_isbn: list[str] = Field(default_factory=list, alias="p-isbn")
    e_issn: Optional[str] = Field(default=None, alias="e-issn")
    p_issn: Optional[str] = Field(default=None, alias="p-issn")
    type_coar: Optional[str] = None


def bibra_to_raw_record(
    data: dict[str, Any] | BibraPublicationMetadata,
    record_id: str = "",
) -> dict[str, Any]:
    """Convert a BIBRA PublicationMetadata dictionary or model into Reconciliator's raw record format.

    The returned dictionary matches the structure produced by parse_marc_record_elem
    and can be fed directly into RecordNormalizer.normalize().
    """
    if isinstance(data, BibraPublicationMetadata):
        data = data.model_dump(by_alias=True)

    # Resolve identifier
    source_id = record_id
    if not source_id:
        if data.get("doi"):
            source_id = str(data["doi"]).strip()
        elif data.get("e-isbn") and data["e-isbn"]:
            source_id = str(data["e-isbn"][0]).strip()
        elif data.get("p-isbn") and data["p-isbn"]:
            source_id = str(data["p-isbn"][0]).strip()
        else:
            title_part = (data.get("title") or "record")[:20].strip()
            source_id = f"bibra-{title_part}"

    # Agents: map each creator to an author agent dictionary
    creators = data.get("creator") or []
    agents: list[dict[str, Any]] = []
    for c in creators:
        if isinstance(c, str) and c.strip():
            agents.append(
                {
                    "name": c.strip(),
                    "role": "aut",
                    "field_tag": "100" if len(agents) == 0 else "700",
                    "is_personal": True,
                    "uri_subfield_0": [],
                    "uri_subfield_1": [],
                }
            )

    # Identifiers
    isbns: list[str] = []
    for isbn in (data.get("e-isbn") or []) + (data.get("p-isbn") or []):
        if isinstance(isbn, str) and isbn.strip():
            isbns.append(isbn.strip())

    issns: list[str] = []
    for issn_field in ("e-issn", "p-issn"):
        val = data.get(issn_field)
        if val and isinstance(val, str) and val.strip():
            issns.append(val.strip())

    other_ids: list[str] = []
    doi = data.get("doi")
    if doi and isinstance(doi, str) and doi.strip():
        other_ids.append(f"doi:{doi.strip()}")

    # Publisher
    publishers = data.get("publisher") or []
    publisher_str: Optional[str] = None
    if publishers and isinstance(publishers, list) and isinstance(publishers[0], str):
        publisher_str = publishers[0].strip()
    elif isinstance(publishers, str):
        publisher_str = publishers.strip()

    # Titles
    title = str(data.get("title") or "").strip()
    alt_title = data.get("alt_title")
    variant_titles: list[str] = []
    if alt_title and isinstance(alt_title, str) and alt_title.strip():
        variant_titles.append(alt_title.strip())

    # Subjects / Genres
    subjects: dict[str, list[dict[str, Any]]] = {
        "topical": [],
        "genre_form": [],
        "geographic": [],
        "medium_of_performance": [],
    }
    type_coar = data.get("type_coar")
    if type_coar and isinstance(type_coar, str) and type_coar.strip():
        subjects["genre_form"].append(
            {
                "heading": type_coar.strip(),
                "field": "655",
                "vocabulary": "coar",
                "uri": None,
            }
        )

    return {
        "source_id": source_id,
        "source": "target",
        "title": title,
        "subtitle": None,
        "uniform_title": None,
        "variant_titles": variant_titles,
        "publication_year": str(data.get("year") or "").strip() or None,
        "publisher": publisher_str,
        "publication_place": None,
        "language": str(data.get("language") or "").strip() or None,
        "original_language": None,
        "edition": None,
        "extent": None,
        "series_title": None,
        "series_numbering": None,
        "volume_designation": None,
        "classifications": [],
        "identifiers": {
            "isbn": isbns,
            "issn": issns,
            "urn": [],
            "other_ids": other_ids,
        },
        "agents": agents,
        "statement_of_responsibility": None,
        "subjects": subjects,
    }


def iter_bibra_records(
    file_path: Path | str,
) -> Iterator[tuple[dict[str, Any], int, dict[str, Any]]]:
    """Stream BIBRA records from a JSON or JSONL/NDJSON file in O(1) memory.

    Yields tuples of:
        (raw_record_dict, file_byte_offset, original_bibra_dict)
    """
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"BIBRA file not found: {path}")

    file_size = path.stat().st_size

    # Check first non-whitespace character
    with open(path, "rb") as f:
        first_char = b""
        while True:
            ch = f.read(1)
            if not ch:
                break
            if not ch.isspace():
                first_char = ch
                break

    if first_char == b"[":
        # JSON array of objects
        with open(path, "r", encoding="utf-8") as f:
            data_list = json.load(f)
            if isinstance(data_list, list):
                for idx, item in enumerate(data_list):
                    if isinstance(item, dict):
                        raw_rec = bibra_to_raw_record(item, record_id=f"bibra-{idx + 1}")
                        yield raw_rec, int(file_size * (idx + 1) / max(len(data_list), 1)), item
    else:
        # Check if entire file is a single JSON object
        is_single_json = False
        try:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
                data = json.loads(content)
                if isinstance(data, dict):
                    is_single_json = True
                    raw_rec = bibra_to_raw_record(data, record_id="bibra-1")
                    yield raw_rec, file_size, data
        except Exception:
            is_single_json = False

        if not is_single_json:
            # Line by line in binary mode to track byte offsets without OSError
            with open(path, "rb") as f:
                current_offset = 0
                idx = 0
                for line in f:
                    current_offset += len(line)
                    line_str = line.decode("utf-8", errors="replace").strip()
                    if not line_str:
                        continue
                    item = json.loads(line_str)
                    if isinstance(item, dict):
                        idx += 1
                        raw_rec = bibra_to_raw_record(item, record_id=f"bibra-{idx}")
                        yield raw_rec, current_offset, item


def bibra_to_marcxml_elem(
    bibra_data: dict[str, Any],
    result: ReconciliationResult,
    target_record: Optional[BibliographicRecord] = None,
) -> etree._Element:
    """Synthesize a catalog-ready MARCXML <record> element from BIBRA metadata and reconciliation result."""
    rec = target_record or result.target_record
    ns = "http://www.loc.gov/MARC21/slim"
    record_elem = etree.Element(f"{{{ns}}}record")

    # Leader
    leader = etree.SubElement(record_elem, f"{{{ns}}}leader")
    leader.text = "00000nam a2200000 i 4500"

    # 001 Controlfield
    f001 = etree.SubElement(record_elem, f"{{{ns}}}controlfield")
    f001.set("tag", "001")
    f001.text = rec.source_id

    # 041 Language
    if rec.language:
        f041 = etree.SubElement(record_elem, f"{{{ns}}}datafield")
        f041.set("tag", "041")
        f041.set("ind1", "0")
        f041.set("ind2", " ")
        sub_a = etree.SubElement(f041, f"{{{ns}}}subfield")
        sub_a.set("code", "a")
        sub_a.text = rec.language

    # 020 ISBNs
    isbns = list(rec.identifiers.isbn)
    for k in ("e-isbn", "p-isbn"):
        for isbn_val in bibra_data.get(k) or []:
            if isbn_val and isbn_val not in isbns:
                isbns.append(isbn_val)

    for isbn in isbns:
        f020 = etree.SubElement(record_elem, f"{{{ns}}}datafield")
        f020.set("tag", "020")
        f020.set("ind1", " ")
        f020.set("ind2", " ")
        sub_a = etree.SubElement(f020, f"{{{ns}}}subfield")
        sub_a.set("code", "a")
        sub_a.text = isbn

    # 022 ISSNs
    for issn in rec.identifiers.issn:
        f022 = etree.SubElement(record_elem, f"{{{ns}}}datafield")
        f022.set("tag", "022")
        f022.set("ind1", " ")
        f022.set("ind2", " ")
        sub_a = etree.SubElement(f022, f"{{{ns}}}subfield")
        sub_a.set("code", "a")
        sub_a.text = issn

    # 024 Other identifiers (e.g. DOI)
    other_ids = list(rec.identifiers.other_ids)
    bib_doi = bibra_data.get("doi")
    if bib_doi and f"doi:{bib_doi}" not in other_ids and f"doi:{bib_doi.strip()}" not in other_ids:
        other_ids.append(f"doi:{bib_doi.strip()}")

    for other_id in other_ids:
        f024 = etree.SubElement(record_elem, f"{{{ns}}}datafield")
        f024.set("tag", "024")
        f024.set("ind1", "7")
        f024.set("ind2", " ")
        if other_id.startswith("doi:"):
            sub_a = etree.SubElement(f024, f"{{{ns}}}subfield")
            sub_a.set("code", "a")
            sub_a.text = other_id[4:]
            sub_2 = etree.SubElement(f024, f"{{{ns}}}subfield")
            sub_2.set("code", "2")
            sub_2.text = "doi"
        else:
            sub_a = etree.SubElement(f024, f"{{{ns}}}subfield")
            sub_a.set("code", "a")
            sub_a.text = other_id

    # Agents (100 for primary author, 700 for additional authors)
    for idx, agent in enumerate(rec.agents):
        tag = "100" if idx == 0 else "700"
        df = etree.SubElement(record_elem, f"{{{ns}}}datafield")
        df.set("tag", tag)
        df.set("ind1", "1" if agent.is_personal else "2")
        df.set("ind2", " ")

        sub_a = etree.SubElement(df, f"{{{ns}}}subfield")
        sub_a.set("code", "a")
        sub_a.text = agent.name

        role_val = next(iter(agent.role_counts.keys())) if agent.role_counts else None
        if role_val:
            sub_e = etree.SubElement(df, f"{{{ns}}}subfield")
            sub_e.set("code", "e")
            sub_e.text = "author" if role_val == "aut" else role_val

        for q_val in agent.qualifier_counts:
            sub_c = etree.SubElement(df, f"{{{ns}}}subfield")
            sub_c.set("code", "c")
            sub_c.text = q_val

        # Inject reconciled URIs: $0 for standard authorities, $1 for Wikidata
        selected_uris = result.selected_agent_uris.get(idx, [])
        for uri in selected_uris:
            code = "1" if "wikidata.org" in uri.lower() else "0"
            sub_uri = etree.SubElement(df, f"{{{ns}}}subfield")
            sub_uri.set("code", code)
            sub_uri.text = uri

    # 245 Title Statement
    f245 = etree.SubElement(record_elem, f"{{{ns}}}datafield")
    f245.set("tag", "245")
    f245.set("ind1", "1" if rec.agents else "0")
    f245.set("ind2", "0")
    sub_title = etree.SubElement(f245, f"{{{ns}}}subfield")
    sub_title.set("code", "a")
    sub_title.text = rec.title

    if rec.subtitle:
        sub_b = etree.SubElement(f245, f"{{{ns}}}subfield")
        sub_b.set("code", "b")
        sub_b.text = rec.subtitle

    # 246 Alternative Title
    for var_title in rec.variant_titles:
        f246 = etree.SubElement(record_elem, f"{{{ns}}}datafield")
        f246.set("tag", "246")
        f246.set("ind1", "1")
        f246.set("ind2", "3")
        sub_a = etree.SubElement(f246, f"{{{ns}}}subfield")
        sub_a.set("code", "a")
        sub_a.text = var_title

    # 260 / 264 Publication info
    if rec.publisher or rec.publication_year or rec.publication_place:
        f260 = etree.SubElement(record_elem, f"{{{ns}}}datafield")
        f260.set("tag", "260")
        f260.set("ind1", " ")
        f260.set("ind2", " ")
        if rec.publication_place:
            sub_a = etree.SubElement(f260, f"{{{ns}}}subfield")
            sub_a.set("code", "a")
            sub_a.text = rec.publication_place
        if rec.publisher:
            sub_b = etree.SubElement(f260, f"{{{ns}}}subfield")
            sub_b.set("code", "b")
            sub_b.text = rec.publisher
        if rec.publication_year:
            sub_c = etree.SubElement(f260, f"{{{ns}}}subfield")
            sub_c.set("code", "c")
            sub_c.text = str(rec.publication_year)

    # 655 Genre / Form (e.g. COAR resource type)
    for g in rec.subjects.genre_form:
        f655 = etree.SubElement(record_elem, f"{{{ns}}}datafield")
        f655.set("tag", "655")
        f655.set("ind1", " ")
        f655.set("ind2", "7")
        sub_a = etree.SubElement(f655, f"{{{ns}}}subfield")
        sub_a.set("code", "a")
        sub_a.text = g.pref_label
        if g.vocabulary:
            sub_2 = etree.SubElement(f655, f"{{{ns}}}subfield")
            sub_2.set("code", "2")
            sub_2.text = g.vocabulary
        if g.uri:
            sub_0 = etree.SubElement(f655, f"{{{ns}}}subfield")
            sub_0.set("code", "0")
            sub_0.text = g.uri

    return record_elem


def enrich_bibra_json(
    bibra_data: dict[str, Any],
    result: ReconciliationResult,
    target_record: Optional[BibliographicRecord] = None,
) -> dict[str, Any]:
    """Enrich the original BIBRA dictionary with resolved authority reconciliation metadata."""
    rec = target_record or result.target_record
    enriched = dict(bibra_data)

    best_match = result.matches[0] if result.matches else None
    authorities: list[dict[str, Any]] = []

    for idx, agent in enumerate(rec.agents):
        selected = result.selected_agent_uris.get(idx, [])
        discovered = {u.uri: u for u in result.discovered_agent_uris.get(idx, [])}

        source_uris: dict[str, str] = {}
        for u in selected:
            src, _ = parse_curie(u)
            if src and src != "unknown":
                source_uris[f"{src}_uri"] = u

        author_entry = {
            "name": agent.name,
            "role": next(iter(agent.role_counts.keys())) if agent.role_counts else "aut",
            "selected_uris": selected,
            "source_uris": source_uris,
            "candidates_count": len(discovered),
        }
        author_entry.update(source_uris)
        authorities.append(author_entry)

    enriched["reconciliation"] = {
        "status": "matched" if result.accepted_candidates else "unmatched",
        "match_tier": best_match.match_tier if best_match else "unmatched",
        "match_score": best_match.match_score if best_match else 0.0,
        "matched_authority_id": result.accepted_candidates[0].source_id
        if result.accepted_candidates
        else None,
        "matched_authority_source": result.accepted_candidates[0].source
        if result.accepted_candidates
        else None,
        "agents": authorities,
    }

    return enriched
