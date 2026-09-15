"""Unified high-fidelity MARCXML parser extracting rich bibliographic data structures."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional

from lxml import etree

from reconciliator.config import (
    DEFAULT_FICTIONAL_QUALIFIERS,
    get_category_default_field_map,
    get_subject_field_category_map,
)
from reconciliator.constituents import ConstituentProfile, get_constituent_profile
from reconciliator.constituents.record import Constituent
from reconciliator.constituents.record import finalise as finalise_constituents

# ISBD presentation punctuation and the stripper live with the loader contract:
# the charset had a second declaration here, and two copies of a normalisation
# rule are a rule that will eventually disagree with itself. The decomposers
# still strip as they parse, because the cleaned title feeds key derivation
# before `finalise` ever sees the record.
from reconciliator.loaders.base import DEFAULT_ISBD_PUNCTUATION, strip_isbd
from reconciliator.loaders.subjects import fold_subjects, subject_footprint
from reconciliator.models import (
    AgentEntity,
    CarrierType,
    ContributionEntity,
    EntityKey,
    ExpressionContentType,
    ExpressionEntity,
    IdentifierEntity,
    ItemEntity,
    ManifestationEntity,
    ManifestationExpressionAssociation,
    MatchRecord,
    MediumOfPerformance,
    RecordOrigin,
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
from reconciliator.normalizers.relators import normalize_role
from reconciliator.template import WEMISerializationEngine
from reconciliator.vocabularies import (
    detect_vocabulary,
)

logger = logging.getLogger(__name__)

#: 1XX main entry: the record's principal creator, whose relator MARC leaves
#: implicit. 7XX added entries carry a relator or none at all by design.
MAIN_ENTRY_TAGS = frozenset({"100", "110", "111"})
IMPLIED_MAIN_ENTRY_ROLE = "aut"

MARC_NS = {"marc": "http://www.loc.gov/MARC21/slim"}


def derive_agent_id(
    name: Optional[str],
    dates: Optional[str] = None,
    fuller_name: Optional[str] = None,
) -> str:
    """Derive a stable agent id from the agent's own content.

    The three decomposition paths used to mint three different fallback URIs, and
    the bibliographic one embedded the agent's ordinal position among the
    record's 7XX fields - so reordering those fields silently changed agent
    identity. This is the same defect Phase 13.1 removed from records via
    `derive_content_id`, and the same fix: identity from content, not position.

    Deliberately scoped to one record: callers combine this digest with the
    record's own source_id. Dropping source_id would additionally collapse one
    person appearing in several records into a single agent row - which the
    `agents` table would accept, since it upserts on `agent_uri` - but it would
    also merge two *different* people who share a name and carry no dates, and
    MARC 7XX fields frequently carry no $d. Agent deduplication is a separate
    decision from agent determinism, and this stage only fixes determinism.

    Only used as a fallback: an agent carrying a $0 or $1 authority URI keeps it,
    since a real authority identifier beats any derived one.
    """
    parts = [
        (name or "").strip().lower(),
        (dates or "").strip().lower(),
        (fuller_name or "").strip().lower(),
    ]
    digest = hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:16]
    return digest


def derive_content_id(record_elem: etree._Element, prefix: str = "sha1") -> str:
    """Derive a stable identifier from a record's own content.

    Records lacking a control number previously got a uuid4, which changes on
    every ingest: the same record re-harvested became a different work, agent,
    expression and manifestation, and no index key built from its URI could
    survive a rebuild. Digesting the record's canonical XML instead makes the
    identifier a function of the data, so re-ingesting is idempotent.

    Two genuinely identical records collapse onto one id, which is the correct
    outcome for a record that carries no identifier of its own.

    The digest is taken over the record's fields rather than its serialization,
    so it does not shift with whitespace, attribute order, or whether a source
    writes the MARC namespace with a prefix.
    """
    parts: list[str] = []
    for child in record_elem:
        tag = etree.QName(child).localname if isinstance(child.tag, str) else str(child.tag)
        if tag == "controlfield":
            parts.append(f"C|{child.get('tag')}|{(child.text or '').strip()}")
        elif tag == "datafield":
            subs = "|".join(
                f"{s.get('code')}={(s.text or '').strip()}"
                for s in child
                if isinstance(s.tag, str)
            )
            parts.append(
                f"D|{child.get('tag')}|{child.get('ind1') or ' '}{child.get('ind2') or ' '}|{subs}"
            )
        elif tag == "leader":
            parts.append(f"L|{(child.text or '').strip()}")
    digest = hashlib.sha1("\n".join(parts).encode("utf-8")).hexdigest()
    return f"{prefix}:{digest}"


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


def extract_marc_source_id(
    record_elem: etree._Element,
    id_source: str = "marc_001",
) -> Optional[str]:
    """Resolve the source_id of a MARCXML record without parsing the whole record.

    Mirrors the resolution in parse_marc_record_elem so skip-existing filtering
    compares the same identifiers that decomposition would later persist.
    """
    oai_id = record_elem.get("_oai_identifier")
    if id_source == "oai_identifier" and oai_id:
        return oai_id
    if id_source == "marc_024":
        for f in _get_datafields(record_elem, "024"):
            v = _get_subfield(f, "a")
            if v:
                return v
        return oai_id
    f001 = _get_controlfield(record_elem, "001")
    if f001:
        f003 = _get_controlfield(record_elem, "003")
        if f003 and not f001.startswith("("):
            return f"({f003}){f001}"
        return f001
    return oai_id


def parse_marc_record_elem(
    record_elem: etree._Element,
    source: str = "target",
    fallback_id: Optional[str] = None,
    id_source: str = "marc_001",
    supported_subject_vocabularies: Optional[Iterable[str]] = None,
    legacy_to_modern: Optional[dict[str, str]] = None,
    relator_mappings: Optional[dict[str, str]] = None,
    vocab_store: Optional[Any] = None,
    strip_isbd_punctuation: bool = True,
    isbd_punctuation: str = DEFAULT_ISBD_PUNCTUATION,
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

    def _isbd(value: Optional[str]) -> Optional[str]:
        """Strip trailing ISBD punctuation, keeping a value that is only that.

        Titles are stripped here, at the single extraction point, because both
        the WEMI decomposers and the target-side MatchRecord read this dict. Only
        agent names were stripped before, so a 245 $a reading "Testikirja :" -
        the colon introduces $b - reached the datastore with the colon, while the
        same title from an authority heading did not. `RecordLoader.finalise`
        catches what any format leaves behind; this keeps the target path, which
        builds no entities and so never passes through a loader, in step.
        """
        stripped = strip_isbd(value, strip_isbd_punctuation, isbd_punctuation)
        return stripped or value

    f245 = _get_datafields(record_elem, "245")
    if f245:
        f = f245[0]
        title_proper = _isbd(_get_subfield(f, "a")) or ""
        subtitle = _isbd(_get_subfield(f, "b"))
        statement_of_resp = _get_subfield(f, "c")
        parts = _get_subfields(f, "n") + _get_subfields(f, "p")
        vol_designations.extend(parts)

    uniform_title = None
    f130 = _get_datafields(record_elem, "130")
    if f130:
        uniform_title = _isbd(_get_subfield(f130[0], "a"))
    if not uniform_title:
        f240 = _get_datafields(record_elem, "240")
        if f240:
            uniform_title = _isbd(_get_subfield(f240[0], "a"))

    variant_titles: list[str] = []
    for f in _get_datafields(record_elem, "246"):
        vt_a = _get_subfield(f, "a")
        if vt_a:
            vt_b = _get_subfield(f, "b")
            combined = vt_a.strip()
            if vt_b:
                combined = f"{combined} {vt_b.strip()}"
            variant_titles.append(_isbd(combined) or combined)
    for f in _get_datafields(record_elem, "740"):
        vt = _isbd(_get_subfield(f, "a"))
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
        original_title = _isbd(_get_subfield(f765[0], "t"))
    if not original_title:
        f242 = _get_datafields(record_elem, "242")
        if f242:
            original_title = _isbd(_get_subfield(f242[0], "a"))
    if not original_title and orig_language and orig_language != language:
        f240 = _get_datafields(record_elem, "240")
        if f240:
            original_title = _isbd(_get_subfield(f240[0], "a"))

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
                series_title = _isbd(_get_subfield(sf, "a"))
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
    issn_ls: list[str] = []
    for f in _get_datafields(record_elem, "022"):
        for sub_code in ("a", "l", "m", "y", "z"):
            for val in _get_subfields(f, sub_code):
                issns.append(val)
        # $l is the linking ISSN, the one that identifies a serial across its
        # medium changes, so funnel H needs it as its own scheme and not only
        # merged into the ISSN bag.
        for val in _get_subfields(f, "l"):
            issn_ls.append(val)

    # 028 publisher/plate numbers. These are the primary tier-1 schemes for
    # recorded sound and notated music, and were previously extracted nowhere -
    # so neither the identifiers table nor the matcher ever saw them.
    publisher_numbers: list[str] = []
    plate_numbers: list[str] = []
    for f in _get_datafields(record_elem, "028"):
        val = _get_subfield(f, "a")
        if not val:
            continue
        # ind1: 0 issue number, 2 plate number, 3 other music publisher number
        if f.get("ind1", " ") == "2":
            plate_numbers.append(val)
        else:
            publisher_numbers.append(val)

    # 088 report/standard technical report number, the tier-1 scheme for funnel O.
    strns: list[str] = []
    for f in _get_datafields(record_elem, "088"):
        for val in _get_subfields(f, "a"):
            strns.append(val)

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
            name = strip_isbd(_get_subfield(f, "a"), strip_isbd_punctuation, isbd_punctuation)
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
                if not norm_roles and tag in MAIN_ENTRY_TAGS:
                    # A main entry with no $e or $4 is the creator; MARC leaves
                    # the relator implicit. Without this the agent's role
                    # histogram was empty for most records - the BibFrame path
                    # records "aut" from the primary contribution, and the target
                    # path already assumes it for a 245 $c name - so the same
                    # person read from two formats had two different footprints.
                    norm_roles = [IMPLIED_MAIN_ENTRY_ROLE]
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
            name = strip_isbd(_get_subfield(f, "a"), strip_isbd_punctuation, isbd_punctuation)
            if name:
                sub_cs = [c.strip(" ,;.") for c in _get_subfields(f, "c") if c.strip(" ,;.")]
                sub_d = _get_subfield(f, "d")
                sub_us = [u.strip(" ,;.") for u in _get_subfields(f, "u") if u.strip(" ,;.")]
                full_literal = f"{name} {' '.join(sub_cs)}".strip()
                is_fictional = any(q in full_literal.lower() for q in DEFAULT_FICTIONAL_QUALIFIERS)
                raw_roles = [r.strip(" ,;.") for r in (_get_subfields(f, "4") + _get_subfields(f, "e")) if r.strip(" ,;.")]
                norm_roles = [normalize_role(r, relator_mappings) for r in raw_roles]
                norm_roles = [r for r in norm_roles if r]
                if not norm_roles and tag in MAIN_ENTRY_TAGS:
                    # A main entry with no $e or $4 is the creator; MARC leaves
                    # the relator implicit. Without this the agent's role
                    # histogram was empty for most records - the BibFrame path
                    # records "aut" from the primary contribution, and the target
                    # path already assumes it for a 245 $c name - so the same
                    # person read from two formats had two different footprints.
                    norm_roles = [IMPLIED_MAIN_ENTRY_ROLE]
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

    # Fold every subject heading to vocabulary + concept id, through the shared
    # folder. The 6XX name-subject tags are included alongside the configured
    # topical and genre tags: a record *about* a person is evidence about that
    # person even though the heading is a name.
    subject_tags = set(subject_field_category_map.keys()) | {"600", "610", "611", "630"}
    raw_for_folding = [
        {
            "heading": _get_subfield(f, "a"),
            "vocabulary": _get_subfield(f, "2"),
            "uris": _get_subfields(f, "0") + _get_subfields(f, "1"),
            "field": tag,
        }
        for tag in subject_tags
        for f in _get_datafields(record_elem, tag)
    ]
    folded_subjects = fold_subjects(
        raw_for_folding,
        supported_vocabularies=supported_subject_vocabularies,
        legacy_to_modern=legacy_to_modern,
        vocab_store=vocab_store,
        language=language,
    )
    rec_subject_counts = subject_footprint(folded_subjects)
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
        "subjects_folded": [s.as_dict() for s in folded_subjects],
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
            "issn_l": issn_ls,
            "publisher_number": publisher_numbers,
            "plate_number": plate_numbers,
            "strn": strns,
            "control_number": [source_id] if source_id else [],
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


# SLM (Suomalainen lajityyppi- ja muotosanasto) genre/form terms -> WorkType.
#
# Recorded in 655 with `$2 slm/fin` per the Sisällönkuvailuopas. Every term below
# was confirmed to exist in SLM through the Finto API, with its URI noted, rather
# than invented from a plausible-looking Finnish word - the failure mode this
# table exists to avoid.
#
# Broadcast: SLM has no single "broadcast programme" concept, so the two parent
# terms and their narrower concepts are listed. The four terms shared between
# television and radio (dokumenttiohjelmat, lastenohjelmat, musiikkiohjelmat,
# luonto-ohjelmat) are included because in SLM they sit only under those two
# parents, so a record carrying one is a broadcast programme either way.
#
# Legal: the whole "juridiset teokset" (s1507) branch.
SLM_WORK_TYPES: dict[str, str] = {
    # televisio-ohjelmat s1209 and its narrower concepts
    "televisio-ohjelmat": "broadcast_program",
    "televisionäytelmät": "broadcast_program",
    "televisiosarjat": "broadcast_program",
    "televisioelokuvat": "broadcast_program",
    "televisio-oopperat": "broadcast_program",
    # radio-ohjelmat s1416 and its narrower concepts
    "radio-ohjelmat": "broadcast_program",
    "radiosarjat": "broadcast_program",
    "radiosovitukset": "broadcast_program",
    "kuunnelmat": "broadcast_program",
    # shared narrower concepts of the two above
    "dokumenttiohjelmat": "broadcast_program",
    "lastenohjelmat": "broadcast_program",
    "musiikkiohjelmat": "broadcast_program",
    "luonto-ohjelmat": "broadcast_program",
    # juridiset teokset s1507 and its narrower concepts
    "juridiset teokset": "legal",
    "lait": "legal",
    "asetukset": "legal",
    "säädöskokoelmat": "legal",
    "direktiivit": "legal",
    "valtiosopimukset": "legal",
    "oikeustapauskokoelmat": "legal",
    "peruskirjat (juridiset teokset)": "legal",
    # antologiat s289, whose broader concept is kokoomateokset s963
    "antologiat": "anthology",
    "kokoomateokset": "anthology",
}


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
    # Terms taken from SLM (Suomalainen lajityyppi- ja muotosanasto), which the
    # subject-analysis guidance names as the 655 vocabulary (`$2 slm/fin`), and
    # verified against Finto rather than guessed. Exact-match first, because
    # substring matching across this vocabulary collides - "dokumenttiohjelmat"
    # (broadcast programmes) and "dokumenttielokuvat" (documentary films) share a
    # stem but are different materials.
    for gh in genre_headings:
        term = gh.strip()
        resolved = SLM_WORK_TYPES.get(term)
        if resolved:
            return resolved  # type: ignore[return-value]

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

    # Tactile resources, from either the machine-readable accessibility field or
    # a tactile RDA content type. The Finnish accessibility guidance directs
    # cataloguers to 341 (`341 0# $a tactile $e braille $2 sapdv` for a braille
    # book), and 336 $b carries the tactile content codes independently.
    #
    # Placed after the genre headings but before the leader checks: a braille
    # book has Leader/06 = "a" like any text, so a leader-first order would never
    # see it. Whether a braille *score* should be "tactile" or "music" is a
    # genuine cataloguing question and this order answers "tactile" - flagged for
    # the National Library rather than settled here.
    for f in _get_datafields(record_elem, "341"):
        for val in _get_subfields(f, "a"):
            if val.strip().lower() == "tactile":
                return "tactile"
    for f in _get_datafields(record_elem, "336"):
        for sub in _get_subfields(f, "b"):
            if sub.strip().lower() in TACTILE_CONTENT_CODES:
                return "tactile"

    # A 111 meeting main entry is a conference. Without this no MARC record ever
    # became work_type "conference" or "event", so funnel J was unreachable
    # despite 111 already being parsed as a corporate agent.
    if _get_datafields(record_elem, "111"):
        return "conference"

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

    # Web archive and digital surrogate, from 857 "elektronisen arkiston sijainti
    # ja käyttö" - the field that exists for material "saatavissa verkkoarkistossa
    # tai digitaalisessa arkistossa", as distinct from 856's ordinary electronic
    # location. Its second indicator says what the archived copy IS:
    #
    #   # or 0  the described resource itself, held in the archive -> web harvest
    #   1 or 4  an electronic version of a physical resource       -> surrogate
    #
    # 533 "jäljennehuomautus" says the same as indicator 1 in note form.
    #
    # Checked after the leader and genre tests on purpose: a digitised map is
    # still a map, so surrogate-ness only classifies a record that would
    # otherwise be generic text. That this is a work type at all is arguable -
    # being a surrogate is a property of the manifestation, not of the work - and
    # is one of the questions to put to the National Library.
    for f in _get_datafields(record_elem, "857"):
        relationship = (f.get("ind2") or " ").strip()
        if relationship in ("1", "4"):
            return "preservation_surrogate"
        return "web_harvest"
    if _get_datafields(record_elem, "533"):
        return "preservation_surrogate"

    # RDA content type as a work-type signal, before the serial/default fall
    # through. Leader/06 wins where it is specific, but an RDA record is required
    # to carry 336 and many electronic resources carry a generic leader with a
    # precise content type - so a dataset coded `336 $b cod` under Leader/06 "a"
    # was arriving as plain text.
    for f in _get_datafields(record_elem, "336"):
        for sub in _get_subfields(f, "b"):
            implied = CONTENT_TYPE_WORK_TYPES.get(sub.strip().lower())
            if implied:
                return implied  # type: ignore[return-value]

    # Serials / Integrating
    if l07 in ("s", "b"):
        return "serial"
    if l07 == "i" or (len(f008) > 21 and f008[21] in ("d", "w")):
        return "integrating"

    return "text"


# MARC 336 $b (rdacontent) -> ExpressionContentType.
#
# The full LoC "Term and Code List for RDA Content Types", mapped onto the
# internal vocabulary. Where RDA is finer-grained than ExpressionContentType the
# nearest internal value is used and the RDA term is named in the comment, so a
# future widening of the internal type has the original distinction to hand.
RDA_CONTENT_TYPES: dict[str, ExpressionContentType] = {
    "txt": "text",
    "ntm": "notated_music",
    "prm": "audio_music",              # performed music
    "spw": "spoken_word",
    "snd": "sounds",
    "sti": "still_image",
    "tdi": "moving_image",             # two-dimensional moving image: film, video
    "tdm": "three_dimensional_moving_image",
    "tdf": "three_dimensional",        # three-dimensional form
    "cri": "cartographic_image",
    "crd": "cartographic_dataset",
    "crm": "moving_image",             # cartographic moving image
    "crf": "cartographic_image",       # cartographic three-dimensional form
    "crt": "tactile_cartographic",     # cartographic tactile image
    "crn": "tactile_cartographic",     # cartographic tactile 3-D form
    "cod": "dataset",                  # computer dataset
    "cop": "computer_program",
    "ntv": "choreographic_movement",   # notated movement
    "tci": "tactile_graphic",          # tactile image
    "tcm": "tactile_notated_music",
    "tcn": "tactile_notated_music",    # tactile notated movement
    "tct": "tactile_text",
    "tcf": "three_dimensional",        # tactile three-dimensional form
}

# RDA content type -> WorkType, used only when Leader/06 was not specific.
# Deliberately partial: codes whose work type the leader already handles well, or
# which imply nothing about the work type, are left out.
CONTENT_TYPE_WORK_TYPES: dict[str, str] = {
    "cri": "cartography",
    "crd": "cartography",
    "crm": "cartography",
    "crf": "cartography",
    "cod": "dataset",              # computer dataset
    "cop": "software",             # computer program
    "ntm": "music",                # notated music
    "prm": "music",                # performed music
    "spw": "audio_nonmusic",       # spoken word
    "sti": "still_image",
    "tdi": "moving_image",         # two-dimensional moving image
    "tdm": "moving_image",         # three-dimensional moving image
    "tdf": "realia",               # three-dimensional form
    "ntv": "choreography",         # notated movement
}

# The tactile subset, which is what makes a resource tactile regardless of what
# else it is. 341 $a "tactile" (‡2 sapdv) says the same thing in machine-readable
# form and is the field the accessibility guidance directs cataloguers to.
TACTILE_CONTENT_CODES = frozenset({"tci", "tcm", "tcn", "tct", "tcf", "crt", "crn"})


def determine_content_type(
    record_elem: etree._Element, work_type: WorkType = "text"
) -> ExpressionContentType:
    """Determine WEMI ExpressionContentType from 336 $b, Leader/06, or work_type."""
    # RDA content types (336 $b, $2 rdacontent). Codes verified against the LoC
    # "Term and Code List for RDA Content Types" rather than transcribed from
    # memory: the previous mapping had `tdi` (two-dimensional moving image - the
    # code ordinary film and video carry) pointing at three-dimensional moving
    # image, `tdm` at a static three-dimensional form, `tcf` and `tcm` swapped
    # with each other's meanings, and three codes that do not exist in the
    # vocabulary at all (`dta`, `dcr`, `tcg`).
    for f in _get_datafields(record_elem, "336"):
        for sub in _get_subfields(f, "b"):
            resolved = RDA_CONTENT_TYPES.get(sub.lower().strip())
            if resolved:
                return resolved

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
    record_origin: RecordOrigin = "authority",
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
                record_origin=record_origin,
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
                    record_origin=record_origin,
                    holding_organization=org,
                    shelf_mark=call_no,
                    barcode=barcode,
                    item_condition=cond,
                )
            )

    return items


def is_marc_authority_record(record_elem: etree._Element) -> bool:
    """Determine whether a MARCXML element represents a MARC 21 Authority record.

    A MARC 21 Authority record has Leader/06 == 'z'. If leader is missing or short,
    it is considered an authority record if it has an established 1xx heading
    (100, 110, 111, 130, 147, 148, 150, 151, 155) and NO 245 title statement
    (which is mandatory in MARC 21 bibliographic records).
    """
    leader_elem = record_elem.find("marc:leader", MARC_NS)
    if leader_elem is None:
        leader_elem = record_elem.find(".//leader")
    ldr = (leader_elem.text or "") if leader_elem is not None else ""
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


def decompose_marc_authority(
    record_elem: etree._Element,
    source: str = "authority",
    id_source: str = "marc_001",
    record_origin: RecordOrigin = "authority",
    serialization_engine: Optional[WEMISerializationEngine] = None,
    strip_isbd_punctuation: bool = True,
    isbd_punctuation: str = DEFAULT_ISBD_PUNCTUATION,
) -> tuple[
    list[WorkEntity],
    list[ExpressionEntity],
    list[ManifestationEntity],
    list[ItemEntity],
    list[AgentEntity],
    list[WorkRelationship],
    list[IdentifierEntity],
    list[ManifestationExpressionAssociation],
    list[MediumOfPerformance],
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
                agent_name_clean = strip_isbd(agent_name, strip_isbd_punctuation, isbd_punctuation) or agent_name
                agent_dates = _get_subfield(agent_field, "d")
                b_yr, d_yr = parse_marc_dates(agent_dates)
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
                    surname=agent_name_clean.split(",")[0].strip() if "," in agent_name_clean else None,
                    dates=agent_dates,
                    birth_year=b_yr,
                    death_year=d_yr,
                    fuller_name=_get_subfield(agent_field, "q"),
                    is_personal=bool(f100),
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
        mops: list[MediumOfPerformance] = []
        for f in _get_datafields(record_elem, "382"):
            h = _get_subfield(f, "a")
            if h:
                uri = _get_subfield(f, "0") or f"urn:mop:{h}"
                mops.append(MediumOfPerformance(work_id=0, medium_uri=uri, label=h))

        # Variant titles from 4xx
        variant_titles: list[str] = []
        for f in _get_datafields(record_elem, "430"):
            vt = _get_subfield(f, "a")
            if vt:
                variant_titles.append(strip_isbd(vt, strip_isbd_punctuation, isbd_punctuation) or vt)
        for tag in ("400", "410", "411"):
            for f in _get_datafields(record_elem, tag):
                vt = _get_subfield(f, "t")
                if vt:
                    variant_titles.append(strip_isbd(vt, strip_isbd_punctuation, isbd_punctuation) or vt)

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
            if not work_uri and (scheme in ("uri", "urn", "asteri") or val.startswith(("http://", "https://", "urn:"))):
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
            # A work authority's 4XX variant titles, which have nowhere else to
            # live: WorkEntity has one title field, and the serialised text -
            # what BM25 and the dense index read - is built from this list, so
            # putting them here is what makes "Vanha Kalevala" findable. Tagged
            # so a reader can tell them from actual subjects; see
            # docs/pipeline_overview.md for the open question of a proper
            # variant-title column.
            subjects_json=[
                {"heading": vt, "pref_label": vt, "kind": "variant_title"}
                for vt in variant_titles
            ],
        )

        # Build canonical ExpressionEntity
        expr_lang = orig_lang or "und"
        expression = ExpressionEntity(
            work_id=0,
            expression_uri=f"urn:exp:{source}:{source_id}:{expr_lang}",
            source=source,
            source_id=source_id,
            record_origin=record_origin,
            language=expr_lang,
            content_type="text",
            title=title,
        )

        engine = serialization_engine or WEMISerializationEngine()
        work.serialized_text = engine.render_work(work, authors=[{"name": a.name} for a in agents_list])
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
            mops,
            contributions,
        )

    elif f100 or f110 or f111:
        # -------------------------------------------------------------
        # Agent Authority (100 Personal, 110 Corporate, 111 Meeting)
        # -------------------------------------------------------------
        agent_field = (f100 or f110 or f111)[0]
        agent_name = _get_subfield(agent_field, "a") or "Unknown Agent"
        agent_name_clean = strip_isbd(agent_name, strip_isbd_punctuation, isbd_punctuation) or agent_name
        agent_dates = _get_subfield(agent_field, "d")
        birth_yr, death_yr = parse_marc_dates(agent_dates)

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
                    variant_names.append(strip_isbd(v, strip_isbd_punctuation, isbd_punctuation) or v)

        # Affiliations from 373
        affiliations: dict[str, int] = {}
        for f in _get_datafields(record_elem, "373"):
            aff = _get_subfield(f, "a")
            if aff:
                affiliations[aff] = affiliations.get(aff, 0) + 1

        # Identifiers & Agent URI
        agent_uri: Optional[str] = None
        orcid: Optional[str] = None
        identifiers: list[IdentifierEntity] = []
        for scheme, val in raw_identifiers:
            norm = val.replace("-", "").strip().lower()
            if scheme == "orcid" or "orcid.org" in val:
                orcid = val
            if not agent_uri and (scheme in ("isni", "viaf", "uri", "urn", "asteri") or val.startswith(("http://", "https://", "urn:"))):
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
                if not uri or any(i.value == uri for i in identifiers):
                    continue
                if not agent_uri:
                    agent_uri = uri
                scheme = (
                    detect_vocabulary(uri) or ("wikidata" if sub_code == "1" else "uri")
                ).lower()
                identifiers.append(
                    IdentifierEntity(
                        entity_type="agent",
                        entity_id=0,
                        scheme=scheme,
                        value=uri,
                        value_normalized=uri.replace("-", "").strip().lower(),
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
            orcid=orcid,
            variant_names=variant_names,
            affiliation_counts=affiliations,
        )

        return ([], [], [], [], [agent], [], identifiers, [], [], [])

    # Subject/topical/form authority without work or agent heading
    return ([], [], [], [], [], [], [], [], [], [])


def decompose_marc_wemi(
    record_elem: etree._Element,
    source: str = "melinda",
    id_source: str = "marc_001",
    include_items: bool = False,
    record_origin: Optional[RecordOrigin | str] = None,
    serialization_engine: Optional[WEMISerializationEngine] = None,
    supported_subject_vocabularies: Optional[Iterable[str]] = None,
    legacy_to_modern: Optional[dict[str, str]] = None,
    relator_mappings: Optional[dict[str, str]] = None,
    vocab_store: Optional[Any] = None,
    key_sink: Optional[list[EntityKey]] = None,
    strip_isbd_punctuation: bool = True,
    isbd_punctuation: str = DEFAULT_ISBD_PUNCTUATION,
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
    """Decompose a single MARCXML record into the WEMI entity core hierarchy.

    `key_sink`, when given, collects the record's derived domain blocking keys as
    `EntityKey` rows bound to the manifestation URI. It is a sink rather than an
    eleventh tuple element so that adding it does not break the ten-way unpacking
    at every existing call site; the tuple is slated to become a result object.
    """
    # record_origin names the ingest source since Phase 13. An explicit value
    # wins; otherwise the source name is the origin.
    effective_origin: RecordOrigin = record_origin or source or "unknown"
    parsed = parse_marc_record_elem(
        record_elem,
        source=source,
        id_source=id_source,
        supported_subject_vocabularies=supported_subject_vocabularies,
        legacy_to_modern=legacy_to_modern,
        relator_mappings=relator_mappings,
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

    # Subjects as folded by parse_marc_record_elem: heading, vocabulary, concept
    # id. The work used to store bare heading strings, which the serialisation
    # template could not render and the classification-evidence reader could not
    # read.
    flat_subjects = list(parsed.get("subjects_folded") or [])

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
        record_origin=effective_origin,
        title=preferred_title,
        # Must use the same stem normalization as the BIBFRAME and Wikidata
        # decomposers, otherwise stem-based work lookup cannot find MARC records.
        title_normalized=compute_title_stem(preferred_title, lang=orig_lang),
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
        is_primary=True,
    )

    # 5. Items
    items = (
        parse_marc_items(record_elem, manifestation_id=0, source=source, record_origin=effective_origin)
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
                orcid=a.get("orcid"),
                variant_names=list(a.get("variant_names", [])),
                qualifier_counts=dict(a.get("qualifier_counts", {})),
                affiliation_counts=dict(a.get("affiliation_counts", {})),
                role_counts=dict(a.get("role_counts", {})),
                language_counts=dict(a.get("language_counts", {})),
                subject_counts=dict(a.get("subject_counts", {})),
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

        # Persist every authority URI carried on the agent. a_uri only holds the
        # first one, so without these rows the remaining $0/$1 links are lost at
        # ingest and can never be transferred during reconciliation.
        for sub_code, uri_list in (("0", u0), ("1", u1)):
            for uri in uri_list:
                if not uri:
                    continue
                scheme = (detect_vocabulary(uri) or ("wikidata" if sub_code == "1" else "uri")).lower()
                identifiers.append(
                    IdentifierEntity(
                        entity_type="agent",
                        entity_id=0,
                        entity_uri=a_uri,
                        scheme=scheme,
                        value=uri,
                        value_normalized=uri.replace("-", "").strip().lower(),
                    )
                )
        if a.get("orcid"):
            orcid_val = str(a["orcid"])
            identifiers.append(
                IdentifierEntity(
                    entity_type="agent",
                    entity_id=0,
                    entity_uri=a_uri,
                    scheme="orcid",
                    value=orcid_val,
                    value_normalized=orcid_val.replace("-", "").strip().lower(),
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

    if key_sink is not None:
        _agents = parsed.get("agents") or []
        _author_key = compute_author_key(
            _agents[0]["name"] if _agents else None,
            _agents[0].get("surname") if _agents else None,
        )
        _title_stem = compute_title_stem(
            parsed.get("uniform_title") or parsed.get("title"), lang=parsed.get("language")
        )
        for key_type, values in derive_domain_keys(
            record_elem, parsed, work_type, _author_key, _title_stem
        ).items():
            for value in values:
                key_sink.append(
                    EntityKey(
                        entity_type="manifestation",
                        entity_id=0,
                        key_type=key_type,
                        key_value=value,
                        entity_uri=manifestation.manifestation_uri,
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


# Analytics are (title, creator) pairs but entity_keys stores one string per row,
# so the pair is JSON-encoded. A control-character separator was tried first and
# rejected: Python counts U+001C-U+001F as whitespace, so `.strip()` silently ate
# it. JSON survives stripping, stays readable in the database, and cannot be
# confused with subfield text whatever punctuation the title carries.
def encode_analytic(title: str, creator: Optional[str]) -> str:
    """Encode a constituent-work (title, creator) pair as one key value."""
    return json.dumps([title, creator], ensure_ascii=False)


def decode_analytic(value: str) -> tuple[str, Optional[str]]:
    """Decode a key value written by `encode_analytic`."""
    try:
        title, creator = json.loads(value)
    except (ValueError, TypeError):
        return value, None
    return title, creator


def _subfield_groups(
    datafield: etree._Element, title_code: str
) -> list[tuple[dict[str, str], dict[str, str]]]:
    """Split a datafield into one group per title subfield, in document order.

    An enhanced 505 repeats its subfields in groups - `$t` title `$r`
    responsibility `$g` duration, again and again - so a constituent's attributes
    are the ones that follow *its* `$t` and precede the next. Reading each code
    with `findall` instead would give every track the first track's duration.

    Each group is returned with the subfields that appear *before the first*
    title as a fallback, because a 700 puts `$a` ahead of `$t`: the creator is
    outside the group it belongs to. The fallback stops there deliberately. Using
    the datafield's first occurrence of each code instead gave every track on an
    album the first track's duration and the first track's composer, since a
    group that states neither would inherit both.
    """
    ordered: list[tuple[str, str]] = []
    for sub in datafield:
        tag = etree.QName(sub).localname if isinstance(sub.tag, str) else ""
        if tag != "subfield":
            continue
        code = sub.get("code") or ""
        text = (sub.text or "").strip()
        if code and text:
            ordered.append((code, text))

    preamble: dict[str, str] = {}
    for code, text in ordered:
        if code == title_code:
            break
        preamble.setdefault(code, text)

    groups: list[tuple[dict[str, str], dict[str, str]]] = []
    current: Optional[dict[str, str]] = None
    for code, text in ordered:
        if code == title_code:
            current = {code: text}
            groups.append((current, preamble))
        elif current is not None:
            current.setdefault(code, text)
    return groups


def extract_marc_constituents(
    record_elem: etree._Element,
    profile: ConstituentProfile,
    *,
    record_id: str = "<unknown>",
) -> list[Constituent]:
    """Read the constituents a profile says this source records.

    The profile decides which tags to look at, which indicator values qualify,
    which subfield carries what, and how the results are deduplicated and
    numbered - because two sources in the same format do not agree about any of
    that. See plans/aggregates.md.
    """
    found: list[Constituent] = []

    for precedence, field in enumerate(profile.fields):
        if not field.tag:
            continue  # an RDF entry; not this format's business
        for datafield in _get_datafields(record_elem, field.tag):
            if not _indicators_match(datafield, field.indicators):
                continue
            for group, field_level in _subfield_groups(datafield, field.title or "t"):

                def value(code: Optional[str]) -> Optional[str]:
                    if not code:
                        return None
                    return group.get(code) or field_level.get(code)

                title = value(field.title)
                if not title:
                    continue
                found.append(
                    Constituent(
                        title=title,
                        creator=value(field.creator),
                        ordinal=_stated_ordinal(field.ordinal, group, field_level),
                        disc=_as_int(value(field.disc)),
                        duration=_clean_duration(value(field.duration)),
                        pagination=value(field.pagination),
                        role=value(field.role),
                        shape=profile.shape_of(field),
                        component_type=field.component_type,
                        container_key=field.container_key,
                        contributes_to_container=field.contributes_to_container,
                        source_field=field.tag,
                        precedence=precedence,
                        creator_controlled=field.creator_controlled,
                    )
                )

    return finalise_constituents(found, profile, record_id=record_id)


def _indicators_match(datafield: etree._Element, required: dict[str, str]) -> bool:
    """Whether a datafield's indicators satisfy the profile.

    An entry with no indicators claims every instance of the tag, which is what
    the old unfiltered reading did; `marc_enhanced` names them, which is how a
    basic 505's single `$a` blob is declined rather than mistaken for a title.
    """
    for name, expected in required.items():
        actual = datafield.get(name)
        # MARC writes a blank indicator as a space, and exporters disagree about
        # whether it is a space or absent.
        if (actual or " ").strip() != (expected or " ").strip():
            return False
    return True


def _stated_ordinal(
    ordinal: str, group: dict[str, str], field_level: dict[str, str]
) -> Optional[int]:
    """A number the record states, for `ordinal: subfield:<code>`.

    `position` and `none` are resolved after deduplication, in `finalise`.
    """
    if not ordinal.startswith("subfield:"):
        return None
    code = ordinal.split(":", 1)[1]
    return _as_int(group.get(code) or field_level.get(code))


def _as_int(value: Optional[str]) -> Optional[int]:
    if not value:
        return None
    digits = re.sub(r"[^0-9]", "", value)
    return int(digits) if digits else None


def _clean_duration(value: Optional[str]) -> Optional[str]:
    """"(4:32)" is one duration written for a card catalogue; store "4:32"."""
    if not value:
        return None
    cleaned = value.strip().strip("()[]").strip()
    return cleaned or None


def extract_marc_analytics(record_elem: etree._Element) -> list[tuple[str, Optional[str]]]:
    """Extract constituent works (title, creator) from 700 $t / 710 $t / 505 $t.

    Lives here rather than in the matcher so the record is parsed once, at
    ingest, instead of once per record per match attempt.

    The reading it performs is now the `marc_legacy` profile - every 505 and
    every 700/710 `$t`, unfiltered and undeduplicated - so the blocking keys a
    source has always derived stay byte-identical while a source that wants the
    stricter reading names `marc_enhanced` instead. One implementation, two
    described conventions, rather than one convention hard-coded here.
    """
    profile = get_constituent_profile("marc_legacy")
    return [(c.title, c.creator) for c in extract_marc_constituents(record_elem, profile)]


def derive_domain_keys(
    record_elem: etree._Element,
    parsed: dict[str, Any],
    work_type: str,
    author_key: Optional[str],
    title_stem: Optional[str],
) -> dict[str, list[str]]:
    """Derive every domain blocking key for one MARC record.

    Single definition of the key bag, shared by the staged `MatchRecord` and by
    the `entity_keys` rows written at ingest, so the two cannot drift apart.
    """
    keys: dict[str, list[str]] = {}

    def _add(key_type: str, val: Optional[str]) -> None:
        if val and str(val).strip():
            clean = str(val).strip()
            bucket = keys.setdefault(key_type, [])
            if clean not in bucket:
                bucket.append(clean)

    idents = parsed.get("identifiers") or {}
    first_issn = (idents.get("issn") or [None])[0]
    first_isbn = (idents.get("isbn") or [None])[0]

    uniform_title = parsed.get("uniform_title")
    title = parsed.get("title")
    agents = parsed.get("agents") or []
    author_surname = agents[0].get("surname") if agents else None

    # Persisted so a MatchRecord can be projected back out of WEMI rows exactly,
    # rather than re-derived from stored titles and hoped to agree.
    _add("author", author_key)
    _add("title_stem", title_stem)
    # Only when the record carries a 240/130. The work row stores the preferred
    # title, which falls back to the 245, so projecting uniform_title from it
    # would invent one for every record that has none.
    _add("uniform_title", parsed.get("uniform_title"))

    _add("work", compute_work_blocking_key(author_key, title_stem))

    if work_type == "music":
        _add("music_work", compute_music_work_key(author_key, uniform_title or title))

    _add("series", compute_series_key(parsed.get("series_title"), first_issn))
    _add("standard", first_isbn or first_issn)

    pub_year = None
    py_raw = parsed.get("publication_year") or ""
    if str(py_raw).isdigit():
        pub_year = int(py_raw)

    start_page = None
    for f in _get_datafields(record_elem, "773"):
        q = _get_subfield(f, "q")
        if q:
            start_page = q
            break
    _add("citation", compute_host_citation_key(first_issn, pub_year, start_page, author_surname))

    # Translators, from the same relator test the serialization engine already
    # uses. The key type was declared on MatchRecord and consumed by the
    # clusterer's pairwise scoring and funnel HH's dispatch, but nothing ever
    # emitted it - so both read an always-empty value.
    translator_names = [
        a.get("name")
        for a in agents
        if "trl" in (a.get("role_counts") or {}) or a.get("role") == "trl"
    ]
    if translator_names:
        _add(
            "translators_signature",
            " ".join(
                sorted(compute_author_key(n, None) for n in translator_names if n)
            ).strip(),
        )

    # Bound-with, from 501 "Huomautus julkaisemisesta jonkin kanssa". The RDA
    # guidance records this as a local field carrying $5 with the holding
    # library's ISIL code, so it appears in institutional records rather than in
    # shared Melinda ones - the key is read here because Melinda is not the only
    # source a corpus comes from. A holdings-level fallback is still needed for
    # sammelbands described only at item level.
    for f in _get_datafields(record_elem, "501"):
        note = _get_subfield(f, "a")
        if note:
            _add("sammelband", note)
        # 014 links the bound-with relationship to the other record.
        for linked in _get_subfields(f, "5"):
            _add("sammelband_holder", linked)
    for f in _get_datafields(record_elem, "014"):
        for val in _get_subfields(f, "a"):
            _add("sammelband", val)

    _add("patent", (idents.get("patent_number") or [None])[0])
    _add("fingerprint", (idents.get("fingerprint") or [None])[0])
    _add("geodata_spatial", (parsed.get("cartographic_coordinates") or [None])[0])
    _add("heritage_model", (parsed.get("digital_file_formats") or [None])[0])

    for a_title, a_creator in extract_marc_analytics(record_elem):
        _add("analytic", encode_analytic(a_title, a_creator))
        # The title alone as well, so records sharing a track can be found by an
        # exact key lookup. The paired form cannot serve that: two catalogues
        # often disagree about a track's creator while agreeing on its title.
        _add("analytic_title", a_title)

    return keys


def build_target_record_from_marc(
    record_elem: etree._Element,
    source: str = "target",
    id_source: str = "marc_001",
    serialization_engine: Optional[WEMISerializationEngine] = None,
    supported_subject_vocabularies: Optional[Iterable[str]] = None,
    legacy_to_modern: Optional[dict[str, str]] = None,
    relator_mappings: Optional[dict[str, str]] = None,
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
        legacy_to_modern=legacy_to_modern,
        relator_mappings=relator_mappings,
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
    work, expression, manifestation, *rest = decompose_marc_wemi(
        record_elem,
        source=source,
        id_source=id_source,
        serialization_engine=engine,
        supported_subject_vocabularies=supported_subject_vocabularies,
        legacy_to_modern=legacy_to_modern,
    )

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
