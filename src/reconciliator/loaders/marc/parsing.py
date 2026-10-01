"""MARCXML bibliographic record parsing, element streaming, and items extraction."""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any, Optional

from lxml import etree
from reconciliator._core.parsers import (
    MarcRecordData,
    py_parse_marc_file,
    py_parse_marc_xml_str,
)

from reconciliator.config import (
    DEFAULT_FICTIONAL_QUALIFIERS,
    get_bundled_vocabularies_defaults,
    get_subject_field_category_map,
    get_work_authority_sources,
)
from reconciliator.constituents import get_constituent_profile
from reconciliator.loaders.base import DEFAULT_ISBD_PUNCTUATION, strip_isbd
from reconciliator.loaders.marc.authority import extract_agent_qualifiers
from reconciliator.loaders.marc.constituents import extract_marc_constituents
from reconciliator.loaders.marc.xml_utils import (
    _get_controlfield,
    _get_datafields,
    _get_subfield,
    _get_subfields,
)
from reconciliator.models import ItemEntity, RecordOrigin
from reconciliator.normalizers.agent import (
    parse_agent_dates,
)
from reconciliator.normalizers.relators import normalize_role
from reconciliator.utils.curie import (
    parse_curie,
)
from reconciliator.vocab import (
    fold_subjects,
    subject_footprint,
)

MAIN_ENTRY_TAGS = frozenset({"100", "110", "111"})
IMPLIED_MAIN_ENTRY_ROLE = "aut"

_LANG_CODE_MAP: dict[str, str] = dict(
    get_bundled_vocabularies_defaults().get("language_code_map") or {}
)



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
    work_authority_sources: Optional[Iterable[str]] = None,
    legacy_to_modern: Optional[dict[str, str]] = None,
    vocab_store: Optional[Any] = None,
    strip_isbd_punctuation: bool = True,
    isbd_punctuation: str = DEFAULT_ISBD_PUNCTUATION,
    default_classification_scheme: Optional[str] = None,
    digital_repository_patterns: Optional[list[str]] = None,
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
    work_authority_candidates: list[str] = []
    work_identifiers: dict[str, list[str]] = {}

    work_auth_set = {
        s.lower()
        for s in (
            work_authority_sources
            if work_authority_sources is not None
            else get_work_authority_sources()
        )
    }

    f130 = _get_datafields(record_elem, "130")
    if f130:
        uniform_title = _isbd(_get_subfield(f130[0], "a"))
        for f in f130:
            for u in _get_subfields(f, "0") + _get_subfields(f, "1"):
                if u:
                    work_authority_candidates.append(u.strip())
    if not uniform_title:
        f240 = _get_datafields(record_elem, "240")
        if f240:
            uniform_title = _isbd(_get_subfield(f240[0], "a"))
    for f in _get_datafields(record_elem, "240"):
        for u in _get_subfields(f, "0") + _get_subfields(f, "1"):
            if u:
                work_authority_candidates.append(u.strip())

    for tag in ("100", "110", "111", "700", "710", "711"):
        for f in _get_datafields(record_elem, tag):
            if _get_subfield(f, "t"):
                for u in _get_subfields(f, "0") + _get_subfields(f, "1"):
                    if u:
                        work_authority_candidates.append(u.strip())

    for tag in ("758",):
        for f in _get_datafields(record_elem, tag):
            for u in _get_subfields(f, "0") + _get_subfields(f, "1"):
                if u:
                    work_authority_candidates.append(u.strip())

    for raw_wid in work_authority_candidates:
        src_w, sid_w = parse_curie(raw_wid)
        if src_w in work_auth_set and sid_w:
            bucket = work_identifiers.setdefault(src_w, [])
            if sid_w not in bucket:
                bucket.append(sid_w)

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

    # 377 - Associated Language: RDA fallback for records with neither a
    # readable 008 language code nor a 041. In bibliographic records, 377 $a
    # carries the language of the content (same semantics as 008/35-37).
    if not language:
        for f in _get_datafields(record_elem, "377"):
            lang_377 = _get_subfield(f, "a")
            if lang_377 and len(lang_377.strip()) == 3:
                language = lang_377.strip()
                break

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
    if default_classification_scheme is None:
        from reconciliator.config import get_bundled_record_defaults

        default_scheme = str(
            get_bundled_record_defaults().get("default_classification_scheme") or "ykl"
        )
    else:
        default_scheme = str(default_classification_scheme)
    for f in _get_datafields(record_elem, "084"):
        code = _get_subfield(f, "a")
        scheme = _get_subfield(f, "2") or default_scheme
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

    if digital_repository_patterns is None:
        from reconciliator.config import get_bundled_vocabularies_defaults

        raw_patterns = get_bundled_vocabularies_defaults().get("digital_repository_patterns") or []
    else:
        raw_patterns = digital_repository_patterns
    repo_patterns = [re.compile(p, re.I) for p in raw_patterns]
    for f in _get_datafields(record_elem, "856"):
        u = _get_subfield(f, "u")
        if u and any(p.search(u) for p in repo_patterns):
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
                name, parsed_qualifiers = extract_agent_qualifiers(name, sub_cs)
                sub_d = _get_subfield(f, "d")
                sub_q = _get_subfield(f, "q")
                birth_yr, death_yr = parse_agent_dates(sub_d)
                full_literal = f"{name} {' '.join(parsed_qualifiers)}".strip()
                is_fictional = any(q in full_literal.lower() for q in DEFAULT_FICTIONAL_QUALIFIERS)
                raw_roles = [
                    r.strip(" ,;.")
                    for r in (_get_subfields(f, "4") + _get_subfields(f, "e"))
                    if r.strip(" ,;.")
                ]
                norm_roles = [normalize_role(r) for r in raw_roles]
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
                agent_identifiers: list[dict[str, Any]] = []
                seen_agent_idents: set[tuple[str, str]] = set()
                for sub_code, u_list in (("0", u0), ("1", u1)):
                    for u in u_list:
                        if not u or not u.strip():
                            continue
                        src, val = parse_curie(
                            u.strip(), default_source="wikidata" if sub_code == "1" else "uri"
                        )
                        if not val:
                            continue
                        key = (src, val)
                        if key in seen_agent_idents:
                            continue
                        seen_agent_idents.add(key)
                        agent_identifiers.append(
                            {
                                "entity_type": "agent",
                                "source": src,
                                "source_id": val,
                                "source_id_normalized": val.replace("-", "").strip().lower(),
                            }
                        )
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
                        "identifiers": agent_identifiers,
                        "qualifiers": parsed_qualifiers,
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
                name, parsed_qualifiers = extract_agent_qualifiers(name, sub_cs)
                sub_d = _get_subfield(f, "d")
                full_literal = f"{name} {' '.join(parsed_qualifiers)}".strip()
                is_fictional = any(q in full_literal.lower() for q in DEFAULT_FICTIONAL_QUALIFIERS)
                raw_roles = [
                    r.strip(" ,;.")
                    for r in (_get_subfields(f, "4") + _get_subfields(f, "e"))
                    if r.strip(" ,;.")
                ]
                norm_roles = [normalize_role(r) for r in raw_roles]
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
                agent_identifiers: list[dict[str, Any]] = []
                seen_agent_idents: set[tuple[str, str]] = set()
                for sub_code, u_list in (("0", u0), ("1", u1)):
                    for u in u_list:
                        if not u or not u.strip():
                            continue
                        src, val = parse_curie(
                            u.strip(), default_source="wikidata" if sub_code == "1" else "uri"
                        )
                        if not val:
                            continue
                        key = (src, val)
                        if key in seen_agent_idents:
                            continue
                        seen_agent_idents.add(key)
                        agent_identifiers.append(
                            {
                                "entity_type": "agent",
                                "source": src,
                                "source_id": val,
                                "source_id_normalized": val.replace("-", "").strip().lower(),
                            }
                        )
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
                        "identifiers": agent_identifiers,
                        "qualifiers": parsed_qualifiers,
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
            **{scheme: list(w_ids) for scheme, w_ids in work_identifiers.items()},
        },
        "work_identifiers": work_identifiers,
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


def parse_marc_file_fast(
    file_path: Path | str,
    use_pigz: bool = True,
    pigz_threads: int = 4,
    pigz_path: Optional[str] = None,
) -> list[MarcRecordData]:
    """Parse MARCXML records from file using native Rust SAX parser (with optional pigz)."""
    return py_parse_marc_file(
        str(file_path),
        use_pigz=use_pigz,
        pigz_threads=pigz_threads,
        pigz_path=pigz_path,
    )


def parse_marc_xml_str_fast(xml: str) -> list[MarcRecordData]:
    """Parse MARCXML records from an XML string using native Rust SAX parser."""
    return py_parse_marc_xml_str(xml)


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




def marc_main_entry_name(record_elem: etree._Element) -> Optional[str]:
    """The 1XX main entry's name, or None when the record has no main entry.

    A contents note listed under a single main entry is that main entry's work:
    a track on a composer's album is by that composer whether or not the note
    repeats the name. Constituent identity needs this, because a track named with
    its composer in one release and without in another would otherwise be two
    works.

    None for a record with no 1XX - a various-artists compilation, catalogued
    under title - which is exactly the case where inheriting would be wrong.
    """
    for tag in ("100", "110", "111"):
        for df in _get_datafields(record_elem, tag):
            name = strip_isbd(_get_subfield(df, "a"))
            if name:
                return name
    return None


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


