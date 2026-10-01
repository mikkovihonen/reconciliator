"""Full record normalization, cleaning, volume discrimination, and classifications extraction."""

from __future__ import annotations

import re
from typing import Any, Optional

from reconciliator.config import (
    DEFAULT_CLASSIFICATION_PRIORITY,
    DEFAULT_ISO_639_2_TO_1,
    DEFAULT_NON_FILING_ARTICLES,
    DEFAULT_PUBLISHER_SUFFIXES,
    DEFAULT_ROMAN_NUMERALS,
    DEFAULT_VOLUME_PREFIXES,
    DEFAULT_VOLUME_SUFFIXES,
    RecordNormalizationConfig,
    get_subject_field_category_map,
)
from reconciliator.embedding.template import RecordTemplateEngine
from reconciliator.models import (
    Agent,
    BibliographicRecord,
    ClassificationItem,
    IdentifierEntity,
    RecordIdentifiers,
    SubjectItem,
    SubjectsContainer,
)
from reconciliator.normalizers.agent import extract_surname, normalize_agent_name, parse_245c_agents
from reconciliator.normalizers.identifiers import (
    normalize_doi,
    normalize_isbn,
    normalize_issn,
    normalize_source_id,
    normalize_urn,
)
from reconciliator.normalizers.linguistic import build_biographical_date_pattern
from reconciliator.normalizers.relators import RelatorRegistry
from reconciliator.vocab import VocabFolder, extract_concept_id, normalize_subject_vocabulary


_ISO_639_2_TO_1: dict[str, str] = DEFAULT_ISO_639_2_TO_1


_PRECOMPUTED_SORTED_DEFAULT_ARTICLES: list[str] = sorted(
    [a.lower() for arts in DEFAULT_NON_FILING_ARTICLES.values() for a in arts],
    key=len,
    reverse=True,
)
_PRECOMPUTED_STRIPPED_DEFAULT_ARTICLES: set[str] = {
    a.strip().lower() for arts in DEFAULT_NON_FILING_ARTICLES.values() for a in arts
}

_PRECOMPUTED_ARTICLES_BY_LANG: dict[str, tuple[list[str], set[str]]] = {}
for _k, _arts in DEFAULT_NON_FILING_ARTICLES.items():
    _sorted = sorted([a.lower() for a in _arts], key=len, reverse=True)
    _stripped = {a.strip().lower() for a in _arts}
    _PRECOMPUTED_ARTICLES_BY_LANG[_k] = (_sorted, _stripped)


def clean_title(
    title: str,
    strip_articles: bool = True,
    articles: Optional[dict[str, list[str]] | set[str] | list[str]] = None,
    lang: Optional[str] = None,
    iso_639_map: Optional[dict[str, str]] = None,
) -> str:
    """Clean and standardize title string, stripping ISBD punctuation and non-filing articles."""
    if not title:
        return ""
    # Strip ISBD punctuation delimiters: ' /', ' :', ' =', ' ;', '.'
    t = re.sub(r"\s+[/:=;]\s*$", "", title.strip())
    t = re.sub(r"[\s.]+$", "", t)

    active_iso_map = iso_639_map if iso_639_map is not None else DEFAULT_ISO_639_2_TO_1

    if strip_articles:
        if articles is None:
            if lang:
                norm_lang = lang.strip().lower()
                norm_lang_2 = active_iso_map.get(norm_lang, norm_lang)
                cached = _PRECOMPUTED_ARTICLES_BY_LANG.get(norm_lang) or _PRECOMPUTED_ARTICLES_BY_LANG.get(norm_lang_2)
                if cached is not None:
                    target_sorted, stripped_arts = cached
                else:
                    target_sorted, stripped_arts = [], set()
            else:
                target_sorted = _PRECOMPUTED_SORTED_DEFAULT_ARTICLES
                stripped_arts = _PRECOMPUTED_STRIPPED_DEFAULT_ARTICLES

            t_lower = t.lower()
            matched = False
            for art_norm in target_sorted:
                if t_lower.startswith(art_norm):
                    t = t[len(art_norm) :].strip()
                    matched = True
                    break
            if not matched:
                tokens = t.split()
                if len(tokens) > 1 and tokens[0].lower() in stripped_arts:
                    t = " ".join(tokens[1:])
        else:
            active_articles = articles
            if isinstance(active_articles, dict):
                target_articles: list[str] = []
                if lang:
                    norm_lang = lang.strip().lower()
                    norm_lang_2 = active_iso_map.get(norm_lang, norm_lang)
                    if norm_lang in active_articles:
                        target_articles = list(active_articles[norm_lang])
                    elif norm_lang_2 in active_articles:
                        target_articles = list(active_articles[norm_lang_2])
                    else:
                        target_articles = []
                else:
                    for arts in active_articles.values():
                        target_articles.extend(arts)
                t_lower = t.lower()
                matched = False
                for art in sorted(target_articles, key=len, reverse=True):
                    art_norm = art.lower()
                    if t_lower.startswith(art_norm):
                        t = t[len(art_norm) :].strip()
                        matched = True
                        break
                if not matched:
                    tokens = t.split()
                    stripped_arts_custom = {a.strip().lower() for a in target_articles}
                    if len(tokens) > 1 and tokens[0].lower() in stripped_arts_custom:
                        t = " ".join(tokens[1:])
            else:
                tokens = t.split()
                stripped_arts_custom = {a.strip().lower() for a in active_articles}
                if len(tokens) > 1 and tokens[0].lower() in stripped_arts_custom:
                    t = " ".join(tokens[1:])

    return re.sub(r"\s+", " ", t).strip()


def extract_year(text: Optional[str]) -> Optional[int]:
    """
    Extract clean 4-digit Gregorian year.
    Supports standard cataloging copyright/phonogram notations: 'c2001', 'cop. 1986', 'p2010', '[1995]'.
    """
    if not text:
        return None
    match = re.search(r"(?<!\d)(1[5-9]\d{2}|20\d{2})(?!\d)", text)
    if match:
        return int(match.group(1))
    return None


def clean_publisher(
    publisher: Optional[str],
    suffixes: Optional[list[str]] = None,
) -> Optional[str]:
    """Remove legal suffixes and clean publisher name."""
    if not publisher:
        return None
    suffix_list = suffixes if suffixes is not None else DEFAULT_PUBLISHER_SUFFIXES
    escaped = [
        re.escape(s.strip()) for s in sorted(suffix_list, key=len, reverse=True) if s.strip()
    ]
    pattern = (
        r"(?:(?<=\b)|(?<=\s))(?:" + "|".join(escaped) + r")(?=[\s.,;:/\[\]]|$)" if escaped else ""
    )

    pub = publisher
    if pattern:
        pub = re.sub(pattern, "", pub, flags=re.IGNORECASE)
    pub = re.sub(r"[.,;:/\[\]]+", " ", pub)
    pub = re.sub(r"\s+", " ", pub).strip()
    return pub or None


def roman_to_int(s: str, roman_numerals: Optional[dict[str, int]] = None) -> Optional[int]:
    """Convert Roman numeral string to integer."""
    clean = s.lower().strip()
    mapping = roman_numerals if roman_numerals is not None else DEFAULT_ROMAN_NUMERALS
    return mapping.get(clean)


def normalize_volume(
    text: Optional[str],
    prefixes: Optional[list[str]] = None,
    suffixes: Optional[list[str]] = None,
    format_template: str = "osa {num}",
    roman_numerals: Optional[dict[str, int]] = None,
) -> Optional[str]:
    """
    Extract volume / part designation and normalize using format_template (default 'osa {num}').
    Matches Arabic and Roman numerals (e.g. 'osa 2', 'Vol. II', 'del 1').
    """
    if not text:
        return None

    pref_list = prefixes if prefixes is not None else DEFAULT_VOLUME_PREFIXES
    suff_list = suffixes if suffixes is not None else DEFAULT_VOLUME_SUFFIXES

    pref_pattern = (
        r"\b(?:" + "|".join(re.escape(p) for p in pref_list if p) + r")\.?\s*(\d+|[ivxlcdm]+)\b"
    )
    match = re.search(pref_pattern, text, re.IGNORECASE)
    if match:
        num_part = match.group(1).lower()
        if num_part.isdigit():
            return format_template.format(num=int(num_part))
        val = roman_to_int(num_part, roman_numerals=roman_numerals)
        if val is not None:
            return format_template.format(num=val)

    suff_pattern = (
        r"\b(\d+|[ivxlcdm]+)\.?\s*(?:" + "|".join(re.escape(s) for s in suff_list if s) + r")\b"
    )
    match_suffix = re.search(suff_pattern, text, re.IGNORECASE)
    if match_suffix:
        num_part = match_suffix.group(1).lower()
        if num_part.isdigit():
            return format_template.format(num=int(num_part))
        val = roman_to_int(num_part, roman_numerals=roman_numerals)
        if val is not None:
            return format_template.format(num=val)

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
    raw_classifications: list[Any],
    priority: Optional[list[str]] = None,
    default_scheme: Optional[str] = None,
) -> list[ClassificationItem]:
    """
    Normalize classifications and flag the primary classification based on scheme priority:
    Accepts tuples (code, scheme), dicts {'code': ..., 'scheme': ..., 'is_primary': ...},
    or ClassificationItem objects.
    """
    if default_scheme is None:
        from reconciliator.config import get_bundled_record_defaults

        default_scheme = str(
            get_bundled_record_defaults().get("default_classification_scheme") or "ykl"
        )
    items: list[tuple[str, str, bool]] = []
    has_explicit_primary = False
    priority_order = tuple(p.lower() for p in (priority or DEFAULT_CLASSIFICATION_PRIORITY))

    for entry in raw_classifications:
        if isinstance(entry, ClassificationItem):
            clean_code = re.sub(r"[\[\]]", "", entry.code).strip()
            clean_scheme = entry.scheme.lower().strip() or default_scheme
            is_prim = entry.is_primary
        elif isinstance(entry, dict):
            code_val = str(entry.get("code") or "")
            scheme_val = str(entry.get("scheme") or default_scheme)
            clean_code = re.sub(r"[\[\]]", "", code_val).strip()
            clean_scheme = scheme_val.lower().strip() or default_scheme
            is_prim = bool(entry.get("is_primary", False))
        elif isinstance(entry, (list, tuple)) and len(entry) >= 2:
            code_val = str(entry[0] or "")
            scheme_val = str(entry[1] or default_scheme)
            clean_code = re.sub(r"[\[\]]", "", code_val).strip()
            clean_scheme = scheme_val.lower().strip() or default_scheme
            is_prim = bool(entry[2]) if len(entry) >= 3 else False
        else:
            continue

        if clean_code:
            if is_prim:
                has_explicit_primary = True
            items.append((clean_code, clean_scheme, is_prim))

    if not items:
        return []

    # If an explicit primary was specified (e.g. preferred rank from Wikidata), retain it
    if has_explicit_primary:
        seen_primary = False
        result: list[ClassificationItem] = []
        for code, scheme, prim in items:
            if prim and not seen_primary:
                result.append(ClassificationItem(code=code, scheme=scheme, is_primary=True))
                seen_primary = True
            else:
                result.append(ClassificationItem(code=code, scheme=scheme, is_primary=False))
        return result

    # Otherwise flag primary according to hierarchy
    schemes = [scheme for _, scheme, _ in items]
    primary_idx = -1
    for pref in priority_order:
        if pref in schemes:
            primary_idx = schemes.index(pref)
            break
    if primary_idx == -1:
        primary_idx = 0

    return [
        ClassificationItem(code=code, scheme=scheme, is_primary=(i == primary_idx))
        for i, (code, scheme, _) in enumerate(items)
    ]


class RecordNormalizer:
    """Symmetrically normalizes authority and target bibliographic records."""

    def __init__(
        self,
        vocab_folder: Optional[VocabFolder] = None,
        template_engine: Optional[RecordTemplateEngine] = None,
        relator_registry: Optional[RelatorRegistry] = None,
        config: Optional[RecordNormalizationConfig] = None,
    ) -> None:
        self.config = config or RecordNormalizationConfig()
        self.vocab_folder = vocab_folder or VocabFolder(linguistic=self.config.linguistic)
        self.template_engine = template_engine
        self.relator_registry = relator_registry or RelatorRegistry(
            vocab_store=getattr(self.vocab_folder, "vocab_store", None),
        )
        self.date_pattern = build_biographical_date_pattern(
            prefixes=self.config.linguistic.biographical_dates.prefixes,
            era_bc=self.config.linguistic.biographical_dates.era_bc,
            era_ad=self.config.linguistic.biographical_dates.era_ad,
            century_suffixes=self.config.linguistic.biographical_dates.century_suffixes,
            custom_pattern=self.config.linguistic.biographical_dates.pattern,
        )
        if self.config.custom_relator_terms:
            for code, term in self.config.custom_relator_terms.items():
                self.relator_registry.register(code, term)

    def normalize_id(self, raw_id: Optional[str]) -> str:
        """Normalize raw source ID using configured control number rules."""
        if not raw_id:
            return ""
        cfg = self.config
        source_id = str(raw_id).strip()
        if (
            cfg.control_number_prefix
            or cfg.control_number_pattern
            or cfg.control_number_padding_digits
            or cfg.control_number_strip_prefixes
        ):
            norm_id = normalize_source_id(
                source_id,
                prefix=cfg.control_number_prefix,
                digits=cfg.control_number_padding_digits,
                pattern=cfg.control_number_pattern,
                strip_prefixes=cfg.control_number_strip_prefixes,
            )
            if norm_id:
                return norm_id
        return source_id

    def normalize(self, raw_data: dict[str, Any]) -> BibliographicRecord:
        """Normalize raw extracted record dictionary into unified BibliographicRecord."""
        cfg = self.config
        source_id = self.normalize_id(raw_data.get("source_id"))
        source = raw_data.get("source", "target")

        rec_lang = raw_data.get("language")
        if rec_lang:
            rec_lang = str(rec_lang).strip().lower()[:3]
        target_lang = rec_lang or cfg.linguistic.default_language

        title_proper = clean_title(
            str(raw_data.get("title") or ""),
            strip_articles=True,
            articles=cfg.non_filing_articles,
            lang=target_lang,
            iso_639_map=cfg.iso_639_2_to_1,
        )
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

        publisher = clean_publisher(raw_data.get("publisher"), suffixes=cfg.publisher_suffixes)
        pub_place = raw_data.get("publication_place")
        if pub_place:
            pub_place = re.sub(r"[\[\]:,.]+", " ", str(pub_place)).strip() or None

        language = rec_lang

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
            volume_designation = normalize_volume(
                str(vol),
                prefixes=cfg.volume_prefixes,
                suffixes=cfg.volume_suffixes,
                format_template=cfg.volume_format_template,
                roman_numerals=cfg.roman_numerals,
            )
        else:
            volume_designation = (
                normalize_volume(
                    full_title,
                    prefixes=cfg.volume_prefixes,
                    suffixes=cfg.volume_suffixes,
                    format_template=cfg.volume_format_template,
                    roman_numerals=cfg.roman_numerals,
                )
                or normalize_volume(
                    raw_data.get("edition"),
                    prefixes=cfg.volume_prefixes,
                    suffixes=cfg.volume_suffixes,
                    format_template=cfg.volume_format_template,
                    roman_numerals=cfg.roman_numerals,
                )
                or normalize_volume(
                    series_numbering,
                    prefixes=cfg.volume_prefixes,
                    suffixes=cfg.volume_suffixes,
                    format_template=cfg.volume_format_template,
                    roman_numerals=cfg.roman_numerals,
                )
            )

        # Classifications
        raw_classifications = raw_data.get("classifications", [])
        classifications = normalize_classifications(
            raw_classifications,
            priority=cfg.classification_priority,
            default_scheme=cfg.default_classification_scheme,
        )

        # Identifiers
        raw_ids = raw_data.get("identifiers", {})
        id_rules = cfg.standard_identifier_rules or {}

        urn_rule = id_rules.get("urn", {})
        urn_pats = urn_rule.get("patterns") or cfg.urn_pattern

        doi_rule = id_rules.get("doi", {})
        doi_prefixes = doi_rule.get("strip_resolver_prefixes")
        doi_pat = doi_rule.get("pattern")

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
            norm_urn = normalize_urn(raw_urn, pattern=urn_pats)
            if norm_urn and norm_urn not in urns:
                urns.append(norm_urn)

        dois: list[str] = []
        for raw_doi in raw_ids.get("doi", []):
            norm_doi = normalize_doi(
                raw_doi,
                strip_resolver_prefixes=doi_prefixes,
                pattern=doi_pat,
            )
            if norm_doi and norm_doi not in dois:
                dois.append(norm_doi)

        other_ids = [str(oid).strip() for oid in raw_ids.get("other_ids", []) if oid]
        work_identifiers: dict[str, list[str]] = {}
        raw_work_ids = raw_ids.get("work_identifiers", {})
        if isinstance(raw_work_ids, dict):
            for k, v in raw_work_ids.items():
                if isinstance(v, list):
                    work_identifiers[str(k)] = [str(x).strip() for x in v if str(x).strip()]
                elif isinstance(v, str) and v.strip():
                    work_identifiers[str(k)] = [v.strip()]

        identifiers = RecordIdentifiers(
            isbn=isbns,
            issn=issns,
            urn=urns,
            doi=dois,
            other_ids=other_ids,
            work_identifiers=work_identifiers,
        )

        # Agents
        agents: list[Agent] = []
        for raw_agent in raw_data.get("agents", []):
            name_raw = raw_agent.get("name", "")
            is_personal = raw_agent.get("is_personal", True)
            is_fictional = bool(raw_agent.get("is_fictional", False)) or any(
                q in name_raw.lower() for q in cfg.fictional_qualifiers
            )
            norm_name, surname = normalize_agent_name(
                name_raw,
                is_personal=is_personal,
                relator_registry=self.relator_registry,
                preserved_diacritics=cfg.linguistic.preserved_diacritics,
                date_pattern=self.date_pattern,
            )
            if norm_name:
                role_counts: dict[str, int] = {}
                raw_rc = dict(raw_agent.get("role_counts", {}))
                if not raw_rc and raw_agent.get("role"):
                    raw_rc[str(raw_agent["role"]).strip()] = 1
                for r_key, r_cnt in raw_rc.items():
                    norm_r = self.relator_registry.normalize_role(r_key) or r_key
                    role_counts[norm_r] = role_counts.get(norm_r, 0) + r_cnt

                quals: list[str] = []
                raw_quals = raw_agent.get("qualifiers")
                if isinstance(raw_quals, list):
                    quals = [str(q) for q in raw_quals if q]
                elif isinstance(raw_quals, dict):
                    quals = [str(q) for q in raw_quals.keys() if q]
                if not quals and raw_agent.get("qualifier"):
                    quals = [str(raw_agent["qualifier"]).strip()]

                # Build identifiers from agent dict
                idents = []
                for ident_data in raw_agent.get("identifiers", []):
                    idents.append(
                        IdentifierEntity(
                            entity_type="agent",
                            entity_id=0,
                            source=ident_data.get("source", "uri"),
                            source_id=ident_data.get("source_id", ""),
                            source_id_normalized=ident_data.get("source_id_normalized", ""),
                        )
                    )

                agents.append(
                    Agent(
                        name=norm_name,
                        surname=surname,
                        dates=raw_agent.get("dates"),
                        fuller_name=raw_agent.get("fuller_name"),
                        birth_year=raw_agent.get("birth_year"),
                        death_year=raw_agent.get("death_year"),
                        field_tag=raw_agent.get("field_tag"),
                        is_personal=is_personal,
                        is_fictional=is_fictional,
                        uri_subfield_0=list(raw_agent.get("uri_subfield_0", [])),
                        uri_subfield_1=list(raw_agent.get("uri_subfield_1", [])),
                        variant_names=list(raw_agent.get("variant_names", [])),
                        qualifiers=quals,
                        identifiers=idents,
                    )
                )

        # Fallback agents from 245$c if no agents present
        if not agents and raw_data.get("statement_of_responsibility"):
            names_245c = parse_245c_agents(
                raw_data["statement_of_responsibility"],
                prefixes=cfg.responsibility_prefixes,
                conjunctions=cfg.responsibility_conjunctions,
                relator_registry=self.relator_registry,
                preserved_diacritics=cfg.linguistic.preserved_diacritics,
                date_pattern=self.date_pattern,
            )
            for n in names_245c:
                s_name = extract_surname(n)
                fict = any(q in n.lower() for q in cfg.fictional_qualifiers)
                agents.append(
                    Agent(
                        name=n,
                        surname=s_name,
                        field_tag="245",
                        is_fictional=fict,
                    )
                )

        # Subjects folding via VocabFolder
        raw_subjects = raw_data.get("subjects", {})
        rec_lang_folding = language or cfg.linguistic.default_language

        folder_cfg = getattr(self.vocab_folder, "config", None)
        field_to_cat = (
            getattr(cfg, "subject_field_category_map", None)
            or getattr(folder_cfg, "subject_field_category_map", None)
            or get_subject_field_category_map()
        )
        cat_default_field = {cat: tag for tag, cat in field_to_cat.items()}
        folded_categories: dict[str, list[SubjectItem]] = {cat: [] for cat in field_to_cat.values()}

        for cat_name, items in raw_subjects.items():
            fallback_field = (
                cat_default_field.get(cat_name)
                or getattr(cfg, "default_subject_field", None)
                or getattr(folder_cfg, "default_vocab_target_field", None)
                or "650"
            )
            for item in items:
                heading = item.get("heading") or item.get("pref_label", "")
                field = item.get("field", fallback_field)
                vocab = item.get("vocabulary")
                uri = item.get("uri")
                t_field, t_vocab, t_uri, t_label, m_type, qid = (
                    self.vocab_folder.fold_subject_heading(
                        heading=heading,
                        source_vocab=vocab,
                        source_field=field,
                        source_uri=uri,
                        lang=rec_lang_folding,
                    )
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
                target_cat = field_to_cat.get(
                    t_field, cat_name if cat_name in folded_categories else "topical"
                )
                folded_categories[target_cat].append(sub_item)

        subjects = SubjectsContainer(
            topical=folded_categories["topical"],
            genre_form=folded_categories["genre_form"],
            geographic=folded_categories["geographic"],
            chronological=folded_categories["chronological"],
            medium_of_performance=folded_categories["medium_of_performance"],
        )

        # Enrich agent subject_counts with canonical concept IDs resolved by VocabFolder
        resolved_subject_keys: list[str] = []
        for s_item in subjects.all_subjects():
            if s_item.uri and s_item.vocabulary:
                norm_v = normalize_subject_vocabulary(
                    s_item.vocabulary,
                    uri=s_item.uri,
                )
                if not norm_v:
                    continue
                cid = extract_concept_id(s_item.uri)
                if cid:
                    resolved_subject_keys.append(f"{norm_v}:{cid.lower()}")

        # subject_counts is no longer on Agent model

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
            work_identifiers=work_identifiers
            or (
                raw_data.get("work_identifiers")
                if isinstance(raw_data.get("work_identifiers"), dict)
                else {}
            ),
            agents=agents,
            subjects=subjects,
            serialization="",
        )

        if self.template_engine:
            record.serialization = self.template_engine.render(record)

        return record
