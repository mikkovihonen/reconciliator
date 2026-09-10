"""Unified high-fidelity MARCXML parser extracting rich bibliographic data structures."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterator, Optional

from lxml import etree

from reconciliator.constants import FICTIONAL_QUALIFIERS

MARC_NS = {"marc": "http://www.loc.gov/MARC21/slim"}


def _get_subfields(elem: etree._Element, code: str) -> list[str]:
    """Get text values of all subfields matching given code."""
    res: list[str] = []
    for sub in elem.findall(f"marc:subfield[@code='{code}']", MARC_NS):
        if sub.text:
            res.append(sub.text.strip())
    # Also support element without namespace prefix if present
    if not res:
        for sub in elem.findall(f"subfield[@code='{code}']"):
            if sub.text:
                res.append(sub.text.strip())
    return res


def _get_subfield(elem: etree._Element, code: str) -> Optional[str]:
    """Get text value of first subfield matching code."""
    subs = _get_subfields(elem, code)
    return subs[0] if subs else None


def _get_datafields(record: etree._Element, tag: str) -> list[etree._Element]:
    """Get all datafield elements for a specific tag."""
    fields = record.findall(f"marc:datafield[@tag='{tag}']", MARC_NS)
    if not fields:
        fields = record.findall(f"datafield[@tag='{tag}']")
    return list(fields)


def _get_controlfield(record: etree._Element, tag: str) -> Optional[str]:
    """Get value of controlfield element."""
    cf = record.find(f"marc:controlfield[@tag='{tag}']", MARC_NS)
    if cf is None:
        cf = record.find(f"controlfield[@tag='{tag}']")
    return cf.text.strip() if cf is not None and cf.text else None


def parse_marc_record_elem(record_elem: etree._Element, source: str = "target") -> dict[str, Any]:
    """Extract raw bibliographic fields from an lxml MARCXML <record> element."""
    # Control numbers
    f001 = _get_controlfield(record_elem, "001")
    f003 = _get_controlfield(record_elem, "003")
    f008 = _get_controlfield(record_elem, "008")

    source_id = f001 or ""
    if f003 and source_id and not source_id.startswith("("):
        source_id = f"({f003}){source_id}"

    # Titles
    title_proper = ""
    subtitle: Optional[str] = None
    statement_of_resp: Optional[str] = None
    vol_designations: list[str] = []

    f245 = _get_datafields(record_elem, "245")
    if f245:
        f = f245[0]
        title_proper = _get_subfield(f, "a") or ""
        subtitle = _get_subfield(f, "b")
        statement_of_resp = _get_subfield(f, "c")
        parts = _get_subfields(f, "n") + _get_subfields(f, "p")
        vol_designations.extend(parts)

    uniform_title = None
    f130 = _get_datafields(record_elem, "130")
    if f130:
        uniform_title = _get_subfield(f130[0], "a")
    if not uniform_title:
        f240 = _get_datafields(record_elem, "240")
        if f240:
            uniform_title = _get_subfield(f240[0], "a")

    variant_titles: list[str] = []
    for f in _get_datafields(record_elem, "246") + _get_datafields(record_elem, "740"):
        vt = _get_subfield(f, "a")
        if vt:
            variant_titles.append(vt)

    # Imprint (260 or 264)
    publisher: Optional[str] = None
    pub_place: Optional[str] = None
    pub_year_str: Optional[str] = None

    for tag in ("264", "260"):
        imprints = _get_datafields(record_elem, tag)
        for imp in imprints:
            if tag == "264":
                ind2 = imp.get("ind2", "")
                if ind2 != "1" and ind2 != " " and publisher:
                    continue
            if not publisher:
                publisher = _get_subfield(imp, "b")
            if not pub_place:
                pub_place = _get_subfield(imp, "a")
            if not pub_year_str:
                pub_year_str = _get_subfield(imp, "c")

    if not pub_year_str and f008 and len(f008) >= 11:
        date1 = f008[7:11].strip()
        if date1.isdigit():
            pub_year_str = date1

    # Language
    language: Optional[str] = None
    if f008 and len(f008) >= 38:
        lang_cand = f008[35:38].strip()
        if len(lang_cand) == 3:
            language = lang_cand

    orig_language: Optional[str] = None
    f041 = _get_datafields(record_elem, "041")
    if f041:
        f = f041[0]
        if not language:
            language = _get_subfield(f, "a")
        orig_language = _get_subfield(f, "h")

    # Edition & Extent
    edition_str: Optional[str] = None
    f250 = _get_datafields(record_elem, "250")
    if f250:
        edition_str = _get_subfield(f250[0], "a")

    extent_str: Optional[str] = None
    f300 = _get_datafields(record_elem, "300")
    if f300:
        extent_str = _get_subfield(f300[0], "a")

    # Series
    series_title: Optional[str] = None
    series_numbering: Optional[str] = None
    for tag in ("490", "830"):
        series_fields = _get_datafields(record_elem, tag)
        if series_fields:
            sf = series_fields[0]
            if not series_title:
                series_title = _get_subfield(sf, "a")
            if not series_numbering:
                series_numbering = _get_subfield(sf, "v")

    # Classifications
    classifications: list[tuple[str, str]] = []
    for f in _get_datafields(record_elem, "084"):
        code = _get_subfield(f, "a")
        scheme = _get_subfield(f, "2") or "ykl"
        if code:
            classifications.append((code, scheme))

    for f in _get_datafields(record_elem, "080"):
        code = _get_subfield(f, "a")
        if code:
            classifications.append((code, "udc"))

    for f in _get_datafields(record_elem, "082"):
        code = _get_subfield(f, "a")
        if code:
            classifications.append((code, "ddc"))

    # Identifiers
    isbns: list[str] = []
    for f in _get_datafields(record_elem, "020"):
        for sub_code in ("a", "z"):
            for val in _get_subfields(f, sub_code):
                isbns.append(val)

    issns: list[str] = []
    for f in _get_datafields(record_elem, "022"):
        for sub_code in ("a", "l", "m", "y", "z"):
            for val in _get_subfields(f, sub_code):
                issns.append(val)

    urns: list[str] = []
    other_ids: list[str] = []
    for f in _get_datafields(record_elem, "024"):
        ind2 = _get_subfield(f, "2") or ""
        val_024 = _get_subfield(f, "a")
        if val_024:
            if ind2.lower() == "urn" or val_024.lower().startswith("urn:"):
                urns.append(val_024)
            else:
                other_ids.append(val_024)

    for f in _get_datafields(record_elem, "856"):
        u = _get_subfield(f, "u")
        if u and "urn.fi" in u.lower():
            urns.append(u)

    for f in _get_datafields(record_elem, "035"):
        val_035 = _get_subfield(f, "a")
        if val_035:
            other_ids.append(val_035)
            if not source_id:
                source_id = val_035

    # Agents
    agents: list[dict[str, Any]] = []
    for tag in ("100", "700"):
        for f in _get_datafields(record_elem, tag):
            name = _get_subfield(f, "a")
            if name:
                sub_c = _get_subfield(f, "c") or ""
                full_literal = f"{name} {sub_c}".strip() if sub_c else name
                is_fictional = any(q in full_literal.lower() for q in FICTIONAL_QUALIFIERS)
                role = _get_subfield(f, "4") or _get_subfield(f, "e")
                u0 = _get_subfields(f, "0")
                u1 = _get_subfields(f, "1")
                agents.append(
                    {
                        "field_tag": tag,
                        "name": name,
                        "is_personal": True,
                        "is_fictional": is_fictional,
                        "role": role,
                        "uri_subfield_0": u0,
                        "uri_subfield_1": u1,
                    }
                )

    for tag in ("110", "710", "111", "711"):
        for f in _get_datafields(record_elem, tag):
            name = _get_subfield(f, "a")
            if name:
                sub_c = _get_subfield(f, "c") or ""
                full_literal = f"{name} {sub_c}".strip() if sub_c else name
                is_fictional = any(q in full_literal.lower() for q in FICTIONAL_QUALIFIERS)
                role = _get_subfield(f, "4") or _get_subfield(f, "e")
                u0 = _get_subfields(f, "0")
                u1 = _get_subfields(f, "1")
                agents.append(
                    {
                        "field_tag": tag,
                        "name": name,
                        "is_personal": False,
                        "is_fictional": is_fictional,
                        "role": role,
                        "uri_subfield_0": u0,
                        "uri_subfield_1": u1,
                    }
                )

    # Subjects
    subjects: dict[str, list[dict[str, Any]]] = {
        "topical": [],
        "genre_form": [],
        "geographic": [],
        "medium_of_performance": [],
    }

    for f in _get_datafields(record_elem, "650"):
        heading = _get_subfield(f, "a")
        if heading:
            vocab = _get_subfield(f, "2")
            uri = _get_subfield(f, "0")
            subjects["topical"].append(
                {
                    "heading": heading,
                    "field": "650",
                    "vocabulary": vocab,
                    "uri": uri,
                }
            )

    for f in _get_datafields(record_elem, "655"):
        heading = _get_subfield(f, "a")
        if heading:
            vocab = _get_subfield(f, "2")
            uri = _get_subfield(f, "0")
            subjects["genre_form"].append(
                {
                    "heading": heading,
                    "field": "655",
                    "vocabulary": vocab,
                    "uri": uri,
                }
            )

    for f in _get_datafields(record_elem, "651"):
        heading = _get_subfield(f, "a")
        if heading:
            vocab = _get_subfield(f, "2")
            uri = _get_subfield(f, "0")
            subjects["geographic"].append(
                {
                    "heading": heading,
                    "field": "651",
                    "vocabulary": vocab,
                    "uri": uri,
                }
            )

    for f in _get_datafields(record_elem, "382"):
        heading = _get_subfield(f, "a")
        if heading:
            vocab = _get_subfield(f, "2")
            uri = _get_subfield(f, "0")
            subjects["medium_of_performance"].append(
                {
                    "heading": heading,
                    "field": "382",
                    "vocabulary": vocab,
                    "uri": uri,
                }
            )

    vol_desig = " ".join(vol_designations) if vol_designations else None

    return {
        "source_id": source_id,
        "source": source,
        "title": title_proper,
        "subtitle": subtitle,
        "uniform_title": uniform_title,
        "variant_titles": variant_titles,
        "publication_year": pub_year_str,
        "publisher": publisher,
        "publication_place": pub_place,
        "language": language,
        "original_language": orig_language,
        "edition": edition_str,
        "extent": extent_str,
        "series_title": series_title,
        "series_numbering": series_numbering,
        "volume_designation": vol_desig,
        "classifications": classifications,
        "identifiers": {
            "isbn": isbns,
            "issn": issns,
            "urn": urns,
            "other_ids": other_ids,
        },
        "agents": agents,
        "statement_of_responsibility": statement_of_resp,
        "subjects": subjects,
    }


def iter_marcxml_records(file_path: Path | str, source: str = "target") -> Iterator[dict[str, Any]]:
    """Stream MARCXML records from a file without loading entire document into memory."""
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"MARCXML file not found at: {path}")

    # Iterparse <record> or <marc:record>
    context = etree.iterparse(
        str(path),
        events=("end",),
        tag=(
            "{http://www.loc.gov/MARC21/slim}record",
            "record",
        ),
    )

    for _, elem in context:
        parsed = parse_marc_record_elem(elem, source=source)
        yield parsed
        elem.clear()
        parent = elem.getparent()
        if parent is not None:
            while elem.getprevious() is not None:
                del parent[0]
    del context


def iter_marc_record_elements(
    file_path: Path | str,
) -> Iterator[tuple[etree._Element, int]]:
    """Stream raw lxml MARCXML <record> elements and file byte offsets with O(1) memory cleanup."""
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"MARCXML file not found at: {path}")

    with open(path, "rb") as in_f:
        context = etree.iterparse(
            in_f,
            events=("end",),
            tag=(
                "{http://www.loc.gov/MARC21/slim}record",
                "record",
            ),
        )
        for _, elem in context:
            yield elem, in_f.tell()
            elem.clear()
            parent = elem.getparent()
            if parent is not None:
                while elem.getprevious() is not None:
                    del parent[0]
        del context
