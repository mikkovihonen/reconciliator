"""Full record normalization, cleaning, volume discrimination, and classifications extraction."""

from __future__ import annotations

import re
from typing import Any, Optional

from reconciliator.finto_folding import FintoFolder
from reconciliator.models import (
    Agent,
    BibliographicRecord,
    ClassificationItem,
    RecordIdentifiers,
    SubjectItem,
    SubjectsContainer,
)
from reconciliator.normalizers.agent import extract_surname, normalize_agent_name, parse_245c_agents
from reconciliator.normalizers.identifiers import (
    normalize_isbn,
    normalize_issn,
    normalize_urn,
)
from reconciliator.template import RecordTemplateEngine

ROMAN_NUMERALS = {
    "i": 1,
    "ii": 2,
    "iii": 3,
    "iv": 4,
    "v": 5,
    "vi": 6,
    "vii": 7,
    "viii": 8,
    "ix": 9,
    "x": 10,
    "xi": 11,
    "xii": 12,
}

NON_FILING_ARTICLES = {
    "the",
    "a",
    "an",
    "en",
    "ett",
    "den",
    "det",
    "de",
    "der",
    "die",
    "das",
    "le",
    "la",
    "les",
}


def clean_title(title: str, strip_articles: bool = True) -> str:
    """Clean and standardize title string, stripping ISBD punctuation and non-filing articles."""
    if not title:
        return ""
    # Strip ISBD punctuation delimiters: ' /', ' :', ' =', ' ;', '.'
    t = re.sub(r"\s+[/:=;]\s*$", "", title.strip())
    t = re.sub(r"[\s.]+$", "", t)

    if strip_articles:
        tokens = t.split()
        if len(tokens) > 1 and tokens[0].lower() in NON_FILING_ARTICLES:
            t = " ".join(tokens[1:])

    return re.sub(r"\s+", " ", t).strip()


def extract_year(text: Optional[str]) -> Optional[int]:
    """Extract clean 4-digit Gregorian year."""
    if not text:
        return None
    match = re.search(r"\b(1[5-9]\d{2}|20\d{2})\b", text)
    if match:
        return int(match.group(1))
    return None


def clean_publisher(publisher: Optional[str]) -> Optional[str]:
    """Remove legal suffixes and clean publisher name."""
    if not publisher:
        return None
    pub = re.sub(
        r"\b(Oy|Ab|Kustannusosakeyhtiö|Förlag|Kustannus|Publishing|Ltd|GmbH)\b",
        "",
        publisher,
        flags=re.IGNORECASE,
    )
    pub = re.sub(r"[.,;:/\[\]]+", " ", pub)
    pub = re.sub(r"\s+", " ", pub).strip()
    return pub or None


def roman_to_int(s: str) -> Optional[int]:
    """Convert Roman numeral string to integer."""
    clean = s.lower().strip()
    return ROMAN_NUMERALS.get(clean)


def normalize_volume(text: Optional[str]) -> Optional[str]:
    """
    Extract volume / part designation and normalize to canonical 'osa <int>'.
    Matches Arabic and Roman numerals (e.g. 'osa 2', 'Vol. II', 'del 1' -> 'osa <int>').
    """
    if not text:
        return None

    # 1. Check keyword followed by numeral (e.g. 'osa 2', 'Vol. II', 'del 1')
    pattern_prefix = r"\b(osa|nide|vol|nro|del|band|tome|v)\.?\s*(\d+|[ivxlcdm]+)\b"
    match = re.search(pattern_prefix, text, re.IGNORECASE)
    if match:
        num_part = match.group(2).lower()
        if num_part.isdigit():
            return f"osa {int(num_part)}"
        val = roman_to_int(num_part)
        if val is not None:
            return f"osa {val}"

    # 2. Check numeral followed by keyword (e.g. '1. osa', '2 osa', 'II del')
    pattern_suffix = r"\b(\d+|[ivxlcdm]+)\.?\s*(osa|nide|del|band)\b"
    match_suffix = re.search(pattern_suffix, text, re.IGNORECASE)
    if match_suffix:
        num_part = match_suffix.group(1).lower()
        if num_part.isdigit():
            return f"osa {int(num_part)}"
        val = roman_to_int(num_part)
        if val is not None:
            return f"osa {val}"

    return None


def extract_edition(edition_str: Optional[str]) -> Optional[int]:
    """Extract edition integer ordinal (e.g. '2. uud. p.' -> 2)."""
    if not edition_str:
        return None
    match = re.search(r"(\d+)", edition_str)
    if match:
        return int(match.group(1))
    return None


def extract_extent(extent_str: Optional[str]) -> Optional[int]:
    """Extract page count integer heuristic (e.g. '245 s.' -> 245)."""
    if not extent_str:
        return None
    match = re.search(r"(\d+)\s*(?:s\.|p\.|pages)", extent_str, re.IGNORECASE)
    if match:
        return int(match.group(1))
    match = re.search(r"\b(\d+)\b", extent_str)
    if match:
        return int(match.group(1))
    return None


def normalize_classifications(
    raw_classifications: list[tuple[str, str]],
) -> list[ClassificationItem]:
    """
    Normalize classifications and flag the primary classification based on scheme priority:
    Priority hierarchy: ykl -> udc -> ddc -> lcc.
    """
    items: list[ClassificationItem] = []
    for code_str, scheme_str in raw_classifications:
        clean_code = re.sub(r"[\[\]]", "", code_str).strip()
        clean_scheme = scheme_str.lower().strip() or "ykl"
        if clean_code:
            items.append(ClassificationItem(code=clean_code, scheme=clean_scheme, is_primary=False))

    if not items:
        return []

    # Flag primary according to hierarchy: ykl -> udc -> ddc -> lcc -> first
    schemes = [c.scheme for c in items]
    primary_idx = -1
    for pref in ("ykl", "udc", "ddc", "lcc"):
        if pref in schemes:
            primary_idx = schemes.index(pref)
            break
    if primary_idx == -1:
        primary_idx = 0

    result: list[ClassificationItem] = []
    for i, item in enumerate(items):
        result.append(
            ClassificationItem(code=item.code, scheme=item.scheme, is_primary=(i == primary_idx))
        )
    return result


class RecordNormalizer:
    """Symmetrically normalizes authority and target bibliographic records."""

    def __init__(
        self,
        finto_folder: Optional[FintoFolder] = None,
        template_engine: Optional[RecordTemplateEngine] = None,
    ) -> None:
        self.finto_folder = finto_folder or FintoFolder()
        self.template_engine = template_engine

    def normalize(self, raw_data: dict[str, Any]) -> BibliographicRecord:
        """Normalize raw extracted record dictionary into unified BibliographicRecord."""
        source_id = str(raw_data.get("source_id") or raw_data.get("external_id") or "")
        source = raw_data.get("source", "target")

        title_proper = clean_title(str(raw_data.get("title") or ""))
        subtitle = raw_data.get("subtitle")
        if subtitle:
            subtitle = clean_title(str(subtitle), strip_articles=False)
            full_title = f"{title_proper} : {subtitle}" if title_proper else subtitle
        else:
            full_title = title_proper

        uniform_title = raw_data.get("uniform_title")
        if uniform_title:
            uniform_title = re.sub(r"[\[\]]", "", str(uniform_title)).strip()

        variant_titles = [
            clean_title(vt, strip_articles=False) for vt in raw_data.get("variant_titles", []) if vt
        ]

        year = raw_data.get("publication_year")
        if isinstance(year, str):
            year = extract_year(year)

        publisher = clean_publisher(raw_data.get("publisher"))
        pub_place = raw_data.get("publication_place")
        if pub_place:
            pub_place = re.sub(r"[\[\]:,.]+", " ", str(pub_place)).strip() or None

        language = raw_data.get("language")
        if language:
            language = str(language).strip().lower()[:3]

        orig_language = raw_data.get("original_language")
        if orig_language:
            orig_language = str(orig_language).strip().lower()[:3]

        edition = raw_data.get("edition")
        if isinstance(edition, str):
            edition = extract_edition(edition)

        extent = raw_data.get("extent")
        if isinstance(extent, str):
            extent = extract_extent(extent)

        series_title = raw_data.get("series_title")
        if series_title:
            series_title = clean_title(str(series_title), strip_articles=False)

        series_numbering = raw_data.get("series_numbering")
        if series_numbering:
            series_numbering = str(series_numbering).strip()

        # Volume / Part discrimination
        vol = raw_data.get("volume_designation")
        if vol:
            volume_designation = normalize_volume(str(vol))
        else:
            volume_designation = (
                normalize_volume(full_title)
                or normalize_volume(raw_data.get("edition"))
                or normalize_volume(series_numbering)
            )

        # Classifications
        raw_classifications = raw_data.get("classifications", [])
        classifications = normalize_classifications(raw_classifications)

        # Identifiers
        raw_ids = raw_data.get("identifiers", {})
        isbns: list[str] = []
        for raw_isbn in raw_ids.get("isbn", []):
            norm_isbn = normalize_isbn(raw_isbn)
            if norm_isbn and norm_isbn not in isbns:
                isbns.append(norm_isbn)

        issns: list[str] = []
        for raw_issn in raw_ids.get("issn", []):
            norm_issn = normalize_issn(raw_issn)
            if norm_issn and norm_issn not in issns:
                issns.append(norm_issn)

        urns: list[str] = []
        for raw_urn in raw_ids.get("urn", []):
            norm_urn = normalize_urn(raw_urn)
            if norm_urn and norm_urn not in urns:
                urns.append(norm_urn)

        other_ids = [str(oid).strip() for oid in raw_ids.get("other_ids", []) if oid]

        identifiers = RecordIdentifiers(isbn=isbns, issn=issns, urn=urns, other_ids=other_ids)

        # Agents
        agents: list[Agent] = []
        for raw_agent in raw_data.get("agents", []):
            name_raw = raw_agent.get("name", "")
            is_personal = raw_agent.get("is_personal", True)
            norm_name, surname = normalize_agent_name(name_raw, is_personal=is_personal)
            if norm_name:
                agents.append(
                    Agent(
                        name=norm_name,
                        surname=surname,
                        role=raw_agent.get("role"),
                        field_tag=raw_agent.get("field_tag"),
                        is_personal=is_personal,
                        uri_subfield_0=list(raw_agent.get("uri_subfield_0", [])),
                        uri_subfield_1=list(raw_agent.get("uri_subfield_1", [])),
                    )
                )

        # Fallback agents from 245$c if no agents present
        if not agents and raw_data.get("statement_of_responsibility"):
            names_245c = parse_245c_agents(raw_data["statement_of_responsibility"])
            for n in names_245c:
                s_name = extract_surname(n)
                agents.append(Agent(name=n, surname=s_name, role="aut", field_tag="245"))

        # Subjects folding via FintoFolder
        raw_subjects = raw_data.get("subjects", {})
        topical: list[SubjectItem] = []
        genre_form: list[SubjectItem] = []
        geographic: list[SubjectItem] = []
        medium_of_perf: list[SubjectItem] = []

        for item in raw_subjects.get("topical", []):
            heading = item.get("heading") or item.get("pref_label", "")
            field = item.get("field", "650")
            vocab = item.get("vocabulary")
            uri = item.get("uri")
            t_field, t_vocab, t_uri, t_label, m_type = self.finto_folder.fold_subject_heading(
                heading=heading, source_vocab=vocab, source_field=field, source_uri=uri
            )
            sub_item = SubjectItem(
                uri=t_uri,
                pref_label=t_label,
                vocabulary=t_vocab,
                source_field=field,
                target_field=t_field,
                source_vocab=vocab,
                source_uri=uri,
                source_heading=heading,
                mapping_type=m_type,
            )
            if t_field == "655":
                genre_form.append(sub_item)
            else:
                topical.append(sub_item)

        for item in raw_subjects.get("genre_form", []):
            heading = item.get("heading") or item.get("pref_label", "")
            field = item.get("field", "655")
            vocab = item.get("vocabulary")
            uri = item.get("uri")
            t_field, t_vocab, t_uri, t_label, m_type = self.finto_folder.fold_subject_heading(
                heading=heading, source_vocab=vocab, source_field=field, source_uri=uri
            )
            genre_form.append(
                SubjectItem(
                    uri=t_uri,
                    pref_label=t_label,
                    vocabulary=t_vocab,
                    source_field=field,
                    target_field=t_field,
                    source_vocab=vocab,
                    source_uri=uri,
                    source_heading=heading,
                    mapping_type=m_type,
                )
            )

        for item in raw_subjects.get("geographic", []):
            heading = item.get("heading") or item.get("pref_label", "")
            field = item.get("field", "651")
            vocab = item.get("vocabulary")
            uri = item.get("uri")
            t_field, t_vocab, t_uri, t_label, m_type = self.finto_folder.fold_subject_heading(
                heading=heading, source_vocab=vocab, source_field=field, source_uri=uri
            )
            geographic.append(
                SubjectItem(
                    uri=t_uri,
                    pref_label=t_label,
                    vocabulary=t_vocab,
                    source_field=field,
                    target_field=t_field,
                    source_vocab=vocab,
                    source_uri=uri,
                    source_heading=heading,
                    mapping_type=m_type,
                )
            )

        for item in raw_subjects.get("medium_of_performance", []):
            heading = item.get("heading") or item.get("pref_label", "")
            field = item.get("field", "382")
            vocab = item.get("vocabulary")
            uri = item.get("uri")
            t_field, t_vocab, t_uri, t_label, m_type = self.finto_folder.fold_subject_heading(
                heading=heading, source_vocab=vocab, source_field=field, source_uri=uri
            )
            medium_of_perf.append(
                SubjectItem(
                    uri=t_uri,
                    pref_label=t_label,
                    vocabulary=t_vocab,
                    source_field=field,
                    target_field=t_field,
                    source_vocab=vocab,
                    source_uri=uri,
                    source_heading=heading,
                    mapping_type=m_type,
                )
            )

        subjects = SubjectsContainer(
            topical=topical,
            genre_form=genre_form,
            geographic=geographic,
            medium_of_performance=medium_of_perf,
        )

        record = BibliographicRecord(
            source_id=source_id,
            source=source,
            title=full_title,
            subtitle=subtitle,
            uniform_title=uniform_title,
            variant_titles=variant_titles,
            publication_year=year,
            publisher=publisher,
            publication_place=pub_place,
            language=language,
            original_language=orig_language,
            edition=edition,
            extent=extent,
            series_title=series_title,
            series_numbering=series_numbering,
            volume_designation=volume_designation,
            classifications=classifications,
            identifiers=identifiers,
            agents=agents,
            subjects=subjects,
            serialization="",
        )

        if self.template_engine:
            record.serialization = self.template_engine.render(record)

        return record
