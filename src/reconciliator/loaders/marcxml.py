"""Unified high-fidelity MARCXML parser extracting rich bibliographic data structures."""

from __future__ import annotations

import logging
import re
import uuid
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional

from lxml import etree

from reconciliator.config import (
    DEFAULT_FICTIONAL_QUALIFIERS,
    get_category_default_field_map,
    get_subject_field_category_map,
)
from reconciliator.normalizers.relators import normalize_role
from reconciliator.vocabularies import (
    extract_concept_id,
    normalize_subject_vocabulary,
    resolve_subject_concept_id,
)
from reconciliator.models import (
    AgentEntity,
    CarrierType,
    ContributionEntity,
    ExpressionContentType,
    ExpressionEntity,
    IdentifierEntity,
    ItemEntity,
    ManifestationEntity,
    ManifestationExpressionAssociation,
    MediumOfPerformance,
    TargetRecord,
    WorkEntity,
    WorkRelationship,
    WorkType,
)
from reconciliator.normalizers.blocking import (
    compute_author_key,
    compute_host_citation_key,
    compute_music_work_key,
    compute_series_key,
    compute_title_stem,
    compute_work_blocking_key,
)
from reconciliator.template import WEMISerializationEngine

logger = logging.getLogger(__name__)

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


_YEAR_RANGE_RE = re.compile(
    r"(?:ca\.?|noin|n\.?)?\s*(\d{4})\s*[-–—]\s*(?:ca\.?|noin|n\.?)?\s*(\d{4})"
)
_BIRTH_ONLY_RE = re.compile(r"(?:ca\.?|noin|n\.?)?\s*(\d{4})\s*[-–—]\s*$")
_DEATH_ONLY_RE = re.compile(r"^[-–—]\s*(?:ca\.?|noin|n\.?)?\s*(\d{4})")
_PREFIX_BIRTH_RE = re.compile(r"(?:s\.|f\.|b\.|synt\.)\s*(\d{4})", re.IGNORECASE)
_PREFIX_DEATH_RE = re.compile(r"(?:k\.|d\.|kuol\.)\s*(\d{4})", re.IGNORECASE)


def parse_marc_dates(dates_str: Optional[str]) -> tuple[Optional[int], Optional[int]]:
    """Extract 4-digit birth and death years from MARC subfield $d string."""
    if not dates_str:
        return None, None
    clean = dates_str.strip().rstrip(".,;")
    # 1. Full range: e.g. "1908-1979", "1850?-1910?" or "ca. 1850 - 1910"
    m = _YEAR_RANGE_RE.search(clean)
    if m:
        return int(m.group(1)), int(m.group(2))
    # 2. Birth with hyphen: "1948-"
    m = _BIRTH_ONLY_RE.search(clean)
    if m:
        return int(m.group(1)), None
    # 3. Death with hyphen: "-1920"
    m = _DEATH_ONLY_RE.search(clean)
    if m:
        return None, int(m.group(1))
    # 4. Prefix birth: "s. 1950"
    m = _PREFIX_BIRTH_RE.search(clean)
    if m:
        return int(m.group(1)), None
    # 5. Prefix death: "k. 2012"
    m = _PREFIX_DEATH_RE.search(clean)
    if m:
        return None, int(m.group(1))
    # 6. Fallback finding 4-digit years
    if not any(w in clean.lower() for w in ("luku", "century", "tal")):
        years = re.findall(r"\b(\d{4})\b", clean)
        if len(years) >= 2:
            return int(years[0]), int(years[1])
        elif len(years) == 1 and any(sep in clean for sep in ("-", "–", "—")):
            y = int(years[0])
            hyphen_idx = min(
                (clean.find(sep) for sep in ("-", "–", "—") if sep in clean),
                default=-1,
            )
            idx = clean.find(years[0])
            if idx < hyphen_idx:
                return y, None
            else:
                return None, y
    return None, None


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


def parse_marc_record_elem(
    record_elem: etree._Element,
    source: str = "target",
    fallback_id: Optional[str] = None,
    id_source: str = "marc_001",
    supported_subject_vocabularies: Optional[Iterable[str]] = None,
    legacy_to_modern: Optional[dict[str, str]] = None,
    relator_mappings: Optional[dict[str, str]] = None,
    vocab_store: Optional[Any] = None,
) -> dict[str, Any]:
    """Extract raw bibliographic fields from an lxml MARCXML <record> element."""
    # Control numbers
    f001 = _get_controlfield(record_elem, "001")
    f003 = _get_controlfield(record_elem, "003")
    f008 = _get_controlfield(record_elem, "008")
    oai_id = record_elem.get("_oai_identifier") or fallback_id

    if id_source == "oai_identifier" and oai_id:
        source_id = oai_id
    elif f001:
        source_id = f001
        if f003 and not source_id.startswith("("):
            source_id = f"({f003}){source_id}"
    else:
        source_id = oai_id or ""

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
    for f in _get_datafields(record_elem, "246"):
        vt_a = _get_subfield(f, "a")
        if vt_a:
            vt_b = _get_subfield(f, "b")
            combined = vt_a.strip()
            if vt_b:
                combined = f"{combined} {vt_b.strip()}"
            variant_titles.append(combined)
    for f in _get_datafields(record_elem, "740"):
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

    original_title: Optional[str] = None
    f765 = _get_datafields(record_elem, "765")
    if f765:
        original_title = _get_subfield(f765[0], "t")
    if not original_title:
        f242 = _get_datafields(record_elem, "242")
        if f242:
            original_title = _get_subfield(f242[0], "a")
    if not original_title and orig_language and orig_language != language:
        f240 = _get_datafields(record_elem, "240")
        if f240:
            original_title = _get_subfield(f240[0], "a")

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
    ismns: list[str] = []
    isrcs: list[str] = []
    eans: list[str] = []
    dois: list[str] = []
    other_ids: list[str] = []
    for f in _get_datafields(record_elem, "024"):
        ind1 = f.get("ind1", " ")
        sub2 = (_get_subfield(f, "2") or "").lower()
        val_024 = _get_subfield(f, "a")
        if not val_024:
            continue
        if ind1 == "2" or sub2 == "ismn":
            ismns.append(val_024)
        elif ind1 == "0" or sub2 == "isrc":
            isrcs.append(val_024)
        elif ind1 in ("1", "3") or sub2 in ("upc", "ean"):
            eans.append(val_024)
        elif sub2 == "doi" or val_024.startswith("10."):
            dois.append(val_024)
        elif sub2 == "urn" or val_024.lower().startswith("urn:"):
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

    # Early Printed Books Fingerprints (026)
    fingerprints: list[str] = []
    for f in _get_datafields(record_elem, "026"):
        fp_e = _get_subfield(f, "e")
        if fp_e:
            fingerprints.append(fp_e)
        else:
            groups = [v for code in ("a", "b", "c", "d") for v in _get_subfields(f, code)]
            if groups:
                fingerprints.append(" ".join(groups))

    # Patent Numbers (013)
    patent_numbers: list[str] = []
    for f in _get_datafields(record_elem, "013"):
        pat = _get_subfield(f, "a")
        if pat:
            patent_numbers.append(pat)

    # Cartographic Coordinates (034)
    cartographic_coordinates: list[str] = []
    for f in _get_datafields(record_elem, "034"):
        coords = [f"{code}:{v}" for code in ("d", "e", "f", "g") for v in _get_subfields(f, code)]
        if coords:
            cartographic_coordinates.append(" ".join(coords))

    # 3D Digital File Formats (347)
    digital_file_formats: list[str] = []
    for f in _get_datafields(record_elem, "347"):
        fmt = _get_subfield(f, "b") or _get_subfield(f, "a")
        if fmt:
            digital_file_formats.append(fmt)

    # Target Audience / PEGI Age Ratings (521)
    target_audience: list[str] = []
    for f in _get_datafields(record_elem, "521"):
        aud = _get_subfield(f, "a")
        if aud:
            target_audience.append(aud)

    # System Requirements Note (538)
    system_requirements: list[str] = []
    for f in _get_datafields(record_elem, "538"):
        req = _get_subfield(f, "a")
        if req:
            system_requirements.append(req)

    # Record languages
    rec_languages: dict[str, int] = {}
    if language:
        rec_languages[language.strip().lower()] = 1
    if orig_language and orig_language.strip().lower() not in rec_languages:
        rec_languages[orig_language.strip().lower()] = 1

    # Agents
    agents: list[dict[str, Any]] = []
    for tag in ("100", "700"):
        for f in _get_datafields(record_elem, tag):
            name = _get_subfield(f, "a")
            if name:
                sub_cs = [c.strip(" ,;.") for c in _get_subfields(f, "c") if c.strip(" ,;.")]
                sub_d = _get_subfield(f, "d")
                sub_q = _get_subfield(f, "q")
                sub_us = [u.strip(" ,;.") for u in _get_subfields(f, "u") if u.strip(" ,;.")]
                birth_yr, death_yr = parse_marc_dates(sub_d)
                full_literal = f"{name} {' '.join(sub_cs)}".strip()
                is_fictional = any(q in full_literal.lower() for q in DEFAULT_FICTIONAL_QUALIFIERS)
                raw_roles = [r.strip(" ,;.") for r in (_get_subfields(f, "4") + _get_subfields(f, "e")) if r.strip(" ,;.")]
                norm_roles = [normalize_role(r, relator_mappings) for r in raw_roles]
                norm_roles = [r for r in norm_roles if r]
                u0 = _get_subfields(f, "0")
                u1 = _get_subfields(f, "1")
                orcid_val: Optional[str] = None
                for u in (u1 + u0):
                    if "orcid.org" in u.lower():
                        orcid_val = u
                        break
                agents.append(
                    {
                        "field_tag": tag,
                        "name": name,
                        "dates": sub_d,
                        "fuller_name": sub_q,
                        "birth_year": birth_yr,
                        "death_year": death_yr,
                        "is_personal": True,
                        "is_fictional": is_fictional,
                        "uri_subfield_0": u0,
                        "uri_subfield_1": u1,
                        "orcid": orcid_val,
                        "qualifier_counts": dict(Counter(sub_cs)),
                        "affiliation_counts": dict(Counter(sub_us)),
                        "role_counts": dict(Counter(norm_roles)),
                        "language_counts": dict(rec_languages),
                        "subject_counts": {},
                    }
                )

    for tag in ("110", "710", "111", "711"):
        for f in _get_datafields(record_elem, tag):
            name = _get_subfield(f, "a")
            if name:
                sub_cs = [c.strip(" ,;.") for c in _get_subfields(f, "c") if c.strip(" ,;.")]
                sub_d = _get_subfield(f, "d")
                sub_us = [u.strip(" ,;.") for u in _get_subfields(f, "u") if u.strip(" ,;.")]
                full_literal = f"{name} {' '.join(sub_cs)}".strip()
                is_fictional = any(q in full_literal.lower() for q in DEFAULT_FICTIONAL_QUALIFIERS)
                raw_roles = [r.strip(" ,;.") for r in (_get_subfields(f, "4") + _get_subfields(f, "e")) if r.strip(" ,;.")]
                norm_roles = [normalize_role(r, relator_mappings) for r in raw_roles]
                norm_roles = [r for r in norm_roles if r]
                u0 = _get_subfields(f, "0")
                u1 = _get_subfields(f, "1")
                orcid_val = None
                for u in (u1 + u0):
                    if "orcid.org" in u.lower():
                        orcid_val = u
                        break
                agents.append(
                    {
                        "field_tag": tag,
                        "name": name,
                        "dates": sub_d,
                        "fuller_name": None,
                        "birth_year": None,
                        "death_year": None,
                        "is_personal": False,
                        "is_fictional": is_fictional,
                        "uri_subfield_0": u0,
                        "uri_subfield_1": u1,
                        "orcid": orcid_val,
                        "qualifier_counts": dict(Counter(sub_cs)),
                        "affiliation_counts": dict(Counter(sub_us)),
                        "role_counts": dict(Counter(norm_roles)),
                        "language_counts": dict(rec_languages),
                        "subject_counts": {},
                    }
                )

    # Subjects
    subject_field_category_map = get_subject_field_category_map()
    subjects: dict[str, list[dict[str, Any]]] = {
        category: [] for category in subject_field_category_map.values()
    }

    for tag, category in subject_field_category_map.items():
        for f in _get_datafields(record_elem, tag):
            heading = _get_subfield(f, "a")
            if heading:
                vocab = _get_subfield(f, "2")
                uri = _get_subfield(f, "0")
                subjects[category].append(
                    {
                        "heading": heading,
                        "field": tag,
                        "vocabulary": vocab,
                        "uri": uri,
                    }
                )

    # Harvest supported subject headings for the agent footprint
    valid_subject_keys: list[str] = []
    subject_tags = set(subject_field_category_map.keys()) | {"600", "610", "611", "630"}
    for tag in subject_tags:
        for f in _get_datafields(record_elem, tag):
            heading = _get_subfield(f, "a")
            vocab = _get_subfield(f, "2")
            uris = _get_subfields(f, "0") + _get_subfields(f, "1")
            norm_v = None
            matched_uri = None
            if uris:
                for u in uris:
                    v = normalize_subject_vocabulary(
                        vocab,
                        uri=u,
                        supported_vocabularies=supported_subject_vocabularies,
                        legacy_to_modern=legacy_to_modern,
                    )
                    if v:
                        norm_v = v
                        matched_uri = u
                        break
            if not norm_v and vocab:
                norm_v = normalize_subject_vocabulary(
                    vocab,
                    uri=None,
                    supported_vocabularies=supported_subject_vocabularies,
                    legacy_to_modern=legacy_to_modern,
                )
            if not norm_v:
                continue

            concept_id = None
            if matched_uri:
                concept_id = extract_concept_id(matched_uri)
            elif uris:
                concept_id = extract_concept_id(uris[0])

            if not concept_id and heading:
                concept_id = resolve_subject_concept_id(
                    heading,
                    vocab=norm_v,
                    vocab_store=vocab_store,
                    language=language,
                )

            if concept_id:
                valid_subject_keys.append(f"{norm_v}:{concept_id.lower()}")

    rec_subject_counts = dict(Counter(valid_subject_keys))
    for a in agents:
        a["subject_counts"] = dict(rec_subject_counts)

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
        "original_title": original_title,
        "edition": edition_str,
        "extent": extent_str,
        "series_title": series_title,
        "series_numbering": series_numbering,
        "volume_designation": vol_desig,
        "classifications": classifications,
        "identifiers": {
            "isbn": isbns,
            "issn": issns,
            "ismn": ismns,
            "isrc": isrcs,
            "ean": eans,
            "doi": dois,
            "urn": urns,
            "other_ids": other_ids,
            "fingerprint": fingerprints,
            "patent_number": patent_numbers,
        },
        "fingerprints": fingerprints,
        "patent_numbers": patent_numbers,
        "cartographic_coordinates": cartographic_coordinates,
        "digital_file_formats": digital_file_formats,
        "target_audience": target_audience,
        "system_requirements": system_requirements,
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


# ---------------------------------------------------------------------------
# WEMI Entity Decomposition & Staging Helpers
# ---------------------------------------------------------------------------


def determine_work_type(record_elem: etree._Element) -> WorkType:
    """Determine WEMI WorkType from Leader/06-07, 007, 008, 336, 502, and 655."""
    leader_elem = record_elem.find("marc:leader", MARC_NS)
    if leader_elem is None:
        leader_elem = record_elem.find(".//leader")
    ldr = (leader_elem.text or "") if leader_elem is not None else ""
    l06 = ldr[6:7] if len(ldr) > 6 else ""
    l07 = ldr[7:8] if len(ldr) > 7 else ""

    f008 = _get_controlfield(record_elem, "008") or ""

    # Check 502 (Academic Thesis)
    if _get_datafields(record_elem, "502"):
        return "thesis"

    # Check genre/form headings
    genre_field = get_category_default_field_map()["genre_form"]
    genre_headings = [
        (_get_subfield(f, "a") or "").lower() for f in _get_datafields(record_elem, genre_field)
    ]
    for gh in genre_headings:
        if "sarjakuv" in gh or "manga" in gh:
            return "comic"
        if "lautapel" in gh:
            return "board_game"
        if "videopel" in gh or "tietokonepel" in gh:
            return "interactive_game"
        if "pienpainat" in gh or "tuoteluettel" in gh:
            return "ephemera"
        if "näytelm" in gh:
            return "drama"
        if "koreograf" in gh:
            return "choreography"
        if "standardi" in gh or "standardit" in gh:
            return "standard"
        if "patentt" in gh:
            return "patent"
        if "podcast" in gh:
            return "podcast"
        if "haastattelu" in gh:
            return "oral_history"
        if "näyttelyluettel" in gh:
            return "exhibition"
        if "kriittinen laitos" in gh:
            return "critical_edition"
        if "rahat" in gh or "mitalit" in gh:
            return "numismatic"

    # Music
    if l06 in ("c", "d", "j"):
        return "music"
    if l06 == "i":
        return "audio_nonmusic"

    # Cartography
    if l06 in ("e", "f"):
        return "cartography"

    # Visual / Moving Image
    if l06 in ("g", "v"):
        return "moving_image"
    if l06 == "k":
        return "still_image"

    # Computer files / Software / Datasets
    if l06 == "m":
        if len(f008) > 26 and f008[26] in ("a", "b", "c", "h"):
            return "dataset"
        return "software"

    # Manuscripts
    if l06 == "t":
        return "manuscript"
    if l06 == "p":
        return "archive_fonds"
    if l06 == "r":
        return "realia"
    if l06 == "o":
        return "multimedia"

    # Early printed books (Date1 < 1800)
    date1 = f008[7:11] if len(f008) >= 11 and f008[7:11].isdigit() else None
    if date1 and int(date1) < 1800:
        return "early_print"

    # Serials / Integrating
    if l07 in ("s", "b"):
        return "serial"
    if l07 == "i" or (len(f008) > 21 and f008[21] in ("d", "w")):
        return "integrating"

    return "text"


def determine_content_type(
    record_elem: etree._Element, work_type: WorkType = "text"
) -> ExpressionContentType:
    """Determine WEMI ExpressionContentType from 336 $b, Leader/06, or work_type."""
    for f in _get_datafields(record_elem, "336"):
        for sub in _get_subfields(f, "b"):
            code = sub.lower()
            if code == "txt":
                return "text"
            if code == "ntm":
                return "notated_music"
            if code == "prm":
                return "audio_music"
            if code == "spw":
                return "spoken_word"
            if code == "snd":
                return "sounds"
            if code == "tdi":
                return "three_dimensional_moving_image"
            if code == "tdm":
                return "three_dimensional"
            if code == "tcf":
                return "tactile_notated_music"
            if code == "tcm":
                return "tactile_cartographic"
            if code == "tct":
                return "tactile_text"
            if code == "tcg":
                return "tactile_graphic"
            if code == "cop":
                return "computer_program"
            if code == "dcr":
                return "choreographic_movement"
            if code == "crd":
                return "cartographic_dataset"
            if code == "cri":
                return "cartographic_image"
            if code == "sti":
                return "still_image"
            if code == "dta":
                return "dataset"

    # Fallbacks based on work_type
    if work_type == "music":
        leader_elem = record_elem.find("marc:leader", MARC_NS)
        if leader_elem is None:
            leader_elem = record_elem.find(".//leader")
        ldr = (leader_elem.text or "") if leader_elem is not None else ""
        l06 = ldr[6:7] if len(ldr) > 6 else ""
        return "audio_music" if l06 == "j" else "notated_music"
    if work_type == "audio_nonmusic":
        return "spoken_word"
    if work_type == "moving_image":
        return "moving_image"
    if work_type == "cartography":
        return "cartographic_image"
    if work_type == "still_image":
        return "still_image"
    if work_type in ("software", "ai_model"):
        return "software_code"
    if work_type == "dataset":
        return "dataset"
    if work_type == "tactile":
        return "tactile_text"

    return "text"


def determine_carrier_type(record_elem: etree._Element) -> CarrierType:
    """Determine WEMI CarrierType from 338 $b or Leader/07/007."""
    for f in _get_datafields(record_elem, "338"):
        for sub in _get_subfields(f, "b"):
            code = sub.lower()
            if code == "nc":
                return "volume"
            if code == "nb":
                return "sheet"
            if code == "sd":
                return "audio_disc"
            if code == "ss":
                return "audiocassette"
            if code == "vd":
                return "videodisc"
            if code == "vf":
                return "videocassette"
            if code == "cd":
                return "computer_disc"
            if code == "cr":
                return "online_resource"
            if code == "cb":
                return "memory_card"
            if code == "hd":
                return "microfilm_reel"
            if code == "he":
                return "microfiche"
            if code == "tc":
                return "braille_volume"
            if code == "nr":
                return "object"

    # Fallback based on 007
    f007 = _get_controlfield(record_elem, "007") or ""
    if f007:
        c0 = f007[0].lower()
        if c0 == "c":
            return (
                "online_resource" if len(f007) > 1 and f007[1].lower() == "r" else "computer_disc"
            )
        if c0 == "s":
            return "audio_disc" if len(f007) > 1 and f007[1].lower() == "d" else "audiocassette"
        if c0 == "v":
            return "videodisc" if len(f007) > 1 and f007[1].lower() == "d" else "videocassette"
        if c0 == "h":
            return "microfilm_reel"
        if c0 == "f":
            return "braille_volume"

    return "volume"


def parse_marc_items(
    record_elem: etree._Element,
    manifestation_id: int = 0,
    source: str = "target",
) -> list[ItemEntity]:
    """Extract Item entities from 852 and 952 fields (Koha / local holdings)."""
    items: list[ItemEntity] = []

    # 1. MARC 852 (Location)
    for idx, f in enumerate(_get_datafields(record_elem, "852")):
        org = _get_subfield(f, "a") or _get_subfield(f, "b") or "Unknown Institution"
        call_no = (
            " ".join(
                [
                    p
                    for p in (_get_subfield(f, "c"), _get_subfield(f, "h"), _get_subfield(f, "i"))
                    if p
                ]
            )
            or None
        )
        barcode = _get_subfield(f, "p")
        item_id = barcode or f"item_{manifestation_id}_{idx + 1}"
        notes = " ".join(_get_subfields(f, "z") + _get_subfields(f, "x")) or None

        items.append(
            ItemEntity(
                manifestation_id=manifestation_id,
                item_uri=f"urn:item:{source}:{item_id}",
                source=source,
                source_id=item_id,
                holding_organization=org,
                shelf_mark=call_no,
                barcode=barcode,
                custodial_history=notes,
            )
        )

    # 2. Koha 952 fields
    if not items:
        for idx, f in enumerate(_get_datafields(record_elem, "952")):
            org = _get_subfield(f, "b") or _get_subfield(f, "a") or "Koha Library"
            call_no = _get_subfield(f, "o")
            barcode = _get_subfield(f, "p")
            item_id = barcode or _get_subfield(f, "9") or f"item_{manifestation_id}_{idx + 1}"
            cond = _get_subfield(f, "r")

            items.append(
                ItemEntity(
                    manifestation_id=manifestation_id,
                    item_uri=f"urn:item:{source}:{item_id}",
                    source=source,
                    source_id=item_id,
                    holding_organization=org,
                    shelf_mark=call_no,
                    barcode=barcode,
                    item_condition=cond,
                )
            )

    return items


def decompose_marc_wemi(
    record_elem: etree._Element,
    source: str = "melinda",
    id_source: str = "marc_001",
    include_items: bool = False,
    serialization_engine: Optional[WEMISerializationEngine] = None,
    supported_subject_vocabularies: Optional[Iterable[str]] = None,
    legacy_to_modern: Optional[dict[str, str]] = None,
    relator_mappings: Optional[dict[str, str]] = None,
    vocab_store: Optional[Any] = None,
) -> tuple[
    WorkEntity,
    ExpressionEntity,
    ManifestationEntity,
    list[ItemEntity],
    list[AgentEntity],
    list[WorkRelationship],
    list[IdentifierEntity],
    list[ManifestationExpressionAssociation],
    list[MediumOfPerformance],
    list[ContributionEntity],
]:
    """Decompose a single MARCXML record into the WEMI entity core hierarchy."""
    parsed = parse_marc_record_elem(
        record_elem,
        source=source,
        id_source=id_source,
        supported_subject_vocabularies=supported_subject_vocabularies,
        legacy_to_modern=legacy_to_modern,
        relator_mappings=relator_mappings,
        vocab_store=vocab_store,
    )
    source_id = parsed["source_id"]
    if not source_id or source_id == "unknown_id":
        source_id = f"unknown_id_{uuid.uuid4()}"
        logger.warning(
            "MARCXML record in source '%s' is missing an identifier; assigned unique fallback '%s'",
            source,
            source_id,
        )

    engine = serialization_engine or WEMISerializationEngine()

    # Check 758 Work / Instance links if present
    work_uri = None
    for f in _get_datafields(record_elem, "758"):
        w_sub0 = _get_subfield(f, "0") or _get_subfield(f, "1")
        if w_sub0:
            work_uri = w_sub0
            break
    if not work_uri:
        work_uri = f"urn:work:{source}:{source_id}"

    work_type = determine_work_type(record_elem)
    content_type = determine_content_type(record_elem, work_type=work_type)
    carrier_type = determine_carrier_type(record_elem)

    # All subject strings
    flat_subjects = [
        s.get("heading") for cat in parsed["subjects"].values() for s in cat if s.get("heading")
    ]

    # Primary classification
    ykl_class = None
    for c in parsed["classifications"]:
        if isinstance(c, tuple) and len(c) >= 2:
            code, scheme = c[0], c[1]
            if scheme.lower() == "ykl":
                ykl_class = code
                break
        elif isinstance(c, dict):
            if c.get("is_primary") or c.get("scheme", "").lower() == "ykl":
                ykl_class = c.get("code")
                break

    # 1. Work Entity
    preferred_title = parsed["uniform_title"] or parsed["title"]
    orig_lang = parsed.get("original_language") or parsed.get("language")
    work = WorkEntity(
        work_uri=work_uri,
        source=source,
        source_id=source_id,
        title=preferred_title,
        title_normalized=preferred_title.lower(),
        original_title=parsed.get("original_title"),
        original_language=orig_lang,
        work_type=work_type,
        subjects_json=flat_subjects,
        ykl_class=ykl_class,
    )

    # 2. Expression Entity
    lang = parsed.get("language") or "und"
    expression_uri = f"urn:exp:{source}:{source_id}:{lang}"
    expression = ExpressionEntity(
        work_id=0,  # Assigned upon DB insertion
        expression_uri=expression_uri,
        source=source,
        source_id=source_id,
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
        work_id=0,  # Assigned upon DB insertion
        manifestation_uri=manifestation_uri,
        source=source,
        source_id=source_id,
        title=parsed["title"],
        title_normalized=parsed["title"].lower(),
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
        is_primary=True,
    )

    # 5. Items
    items = (
        parse_marc_items(record_elem, manifestation_id=0, source=source) if include_items else []
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

    # 7. Contributions
    contributions: list[ContributionEntity] = []
    work_roles = {"aut", "cmp", "drt", "cre", "dsr", "prg", "arc"}
    for a in parsed["agents"]:
        role = a.get("role") or "aut"
        e_type = "work" if role in work_roles else "expression"
        contributions.append(
            ContributionEntity(
                entity_type=e_type,
                entity_id=0,
                agent_id=0,
                role=role,
                is_primary=(role in ("aut", "cmp")),
            )
        )

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

    # 9. Medium of Performance
    mop_field = get_category_default_field_map()["medium_of_performance"]
    mops: list[MediumOfPerformance] = []
    for f in _get_datafields(record_elem, mop_field):
        heading = _get_subfield(f, "a")
        if heading:
            uri = _get_subfield(f, "0") or f"urn:mop:{heading}"
            mops.append(
                MediumOfPerformance(
                    work_id=0,
                    medium_uri=uri,
                    label=heading,
                )
            )

    # Pre-render serializations using the WEMI Triad
    translators = [
        a for a in parsed["agents"]
        if "trl" in a.get("role_counts", {}) or a.get("role") == "trl"
    ]
    narrators = [
        a
        for a in parsed["agents"]
        if any(r in a.get("role_counts", {}) for r in ("nrt", "lbr", "narrator", "lukija"))
        or (a.get("role") and a.get("role").strip().lower() in ("nrt", "lbr", "narrator", "lukija"))
    ]
    work.serialized_text = engine.render_work(work, authors=parsed["agents"])
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
        a_uri = u0[0] if u0 else (u1[0] if u1 else f"urn:agent:{source}:{source_id}:{idx + 1}")
        norm_name = a_name.strip().lower()
        agents_list.append(
            AgentEntity(
                agent_uri=a_uri,
                source=source,
                name=a_name,
                name_normalized=norm_name,
                surname=a.get("surname"),
                dates=a.get("dates"),
                birth_year=a.get("birth_year"),
                death_year=a.get("death_year"),
                fuller_name=a.get("fuller_name"),
                is_personal=a.get("is_personal", True),
                is_fictional=a.get("is_fictional", False),
                orcid=a.get("orcid"),
                variant_names=list(a.get("variant_names", [])),
                qualifier_counts=dict(a.get("qualifier_counts", {})),
                affiliation_counts=dict(a.get("affiliation_counts", {})),
                role_counts=dict(a.get("role_counts", {})),
                language_counts=dict(a.get("language_counts", {})),
                subject_counts=dict(a.get("subject_counts", {})),
            )
        )

    return (
        work,
        expression,
        manifestation,
        items,
        agents_list,
        relationships,
        identifiers,
        [assoc],
        mops,
        contributions,
    )


def build_target_record_from_marc(
    record_elem: etree._Element,
    source: str = "target",
    id_source: str = "marc_001",
    serialization_engine: Optional[WEMISerializationEngine] = None,
    supported_subject_vocabularies: Optional[Iterable[str]] = None,
    legacy_to_modern: Optional[dict[str, str]] = None,
    relator_mappings: Optional[dict[str, str]] = None,
    vocab_store: Optional[Any] = None,
) -> TargetRecord:
    """Extract and stage a TargetRecord with all 42 domain blocking keys and pre-rendered serializations."""
    parsed = parse_marc_record_elem(
        record_elem,
        source=source,
        id_source=id_source,
        supported_subject_vocabularies=supported_subject_vocabularies,
        legacy_to_modern=legacy_to_modern,
        relator_mappings=relator_mappings,
        vocab_store=vocab_store,
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
    work_blocking_key = compute_work_blocking_key(author_key, title_stem)

    # Music work key
    music_key = None
    if work_type == "music":
        music_key = compute_music_work_key(author_key, uniform_title or title)

    # Series key
    first_issn = (
        parsed["identifiers"].get("issn", [None])[0] if parsed["identifiers"].get("issn") else None
    )
    series_key = compute_series_key(parsed.get("series_title"), first_issn)

    # Standard key
    first_isbn = (
        parsed["identifiers"].get("isbn", [None])[0] if parsed["identifiers"].get("isbn") else None
    )
    standard_key = first_isbn or first_issn

    # Publication year
    pub_year = None
    py_raw = parsed.get("publication_year") or ""
    if py_raw.isdigit():
        pub_year = int(py_raw)

    # Host citation key
    is_component = bool(_get_datafields(record_elem, "773"))
    start_page = None
    for f in _get_datafields(record_elem, "773"):
        q = _get_subfield(f, "q")
        if q:
            start_page = q
            break
    host_citation = compute_host_citation_key(first_issn, pub_year, start_page, author_surname)

    # Domain-specific keys
    first_fp = (
        parsed["identifiers"].get("fingerprint", [None])[0]
        if parsed["identifiers"].get("fingerprint")
        else None
    )
    first_pat = (
        parsed["identifiers"].get("patent_number", [None])[0]
        if parsed["identifiers"].get("patent_number")
        else None
    )
    first_coords = (
        parsed.get("cartographic_coordinates", [None])[0]
        if parsed.get("cartographic_coordinates")
        else None
    )
    first_3d = (
        parsed.get("digital_file_formats", [None])[0]
        if parsed.get("digital_file_formats")
        else None
    )

    # Pre-render serializations
    engine = serialization_engine or WEMISerializationEngine()
    work, expression, manifestation, *rest = decompose_marc_wemi(
        record_elem,
        source=source,
        id_source=id_source,
        serialization_engine=engine,
        supported_subject_vocabularies=supported_subject_vocabularies,
        legacy_to_modern=legacy_to_modern,
    )

    raw_payload = etree.tostring(record_elem, encoding="utf-8")

    return TargetRecord(
        record_id=source_id,
        format="marcxml",
        work_type=work_type,
        title=title,
        uniform_title=uniform_title,
        language=lang,
        author_key=author_key,
        title_stem=title_stem,
        work_blocking_key=work_blocking_key,
        music_work_key=music_key,
        series_key=series_key,
        standard_key=standard_key,
        patent_key=first_pat,
        fingerprint_key=first_fp,
        geodata_spatial_key=first_coords,
        heritage_model_key=first_3d,
        year=pub_year,
        volume_designation=parsed.get("volume_designation"),
        is_component=is_component,
        host_citation_key=host_citation,
        serialized_work=work.serialized_text,
        serialized_expression=expression.serialized_text,
        serialized_manifestation=manifestation.serialized_text,
        raw_payload=raw_payload,
    )
