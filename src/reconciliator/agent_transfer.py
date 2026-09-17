"""Fuzzy agent name alignment and multi-candidate authority URI aggregation."""

from __future__ import annotations

from typing import Any, Optional

from rapidfuzz import fuzz

from reconciliator.curie import parse_curie
from reconciliator.models import Agent, AgentURI, BibliographicRecord
from reconciliator.vocabularies import detect_vocabulary, extract_concept_id


class AgentAligner:
    """Aligns target agents with candidate records and aggregates discovered authority URIs."""

    def __init__(self, similarity_threshold: float = 85.0) -> None:
        self.similarity_threshold = similarity_threshold

    def align_names(self, name1: str, name2: str) -> float:
        """Calculate normalized similarity [0.0, 1.0] between two agent names."""
        if not name1 or not name2:
            return 0.0
        return fuzz.token_sort_ratio(name1.lower(), name2.lower()) / 100.0

    def align_target_agents(
        self,
        target_agents: list[Agent],
        candidate_records: list[BibliographicRecord],
    ) -> dict[int, list[AgentURI]]:
        """
        Align each target agent against agents across all accepted candidate records.
        Returns a mapping of target_agent_index -> list of discovered AgentURIs.
        """
        discovered: dict[int, list[AgentURI]] = {i: [] for i in range(len(target_agents))}

        for idx, target_agent in enumerate(target_agents):
            if target_agent.is_fictional:
                # Fictional characters (e.g. James Bond, Muumipeikko) must not be linked
                # to real human authority records in KANTO / Asteri. Skipped by design.
                continue

            target_name = target_agent.name.lower()
            seen_uris: set[str] = set()

            for cand_rec in candidate_records:
                for cand_agent in cand_rec.agents:
                    cand_name = cand_agent.name.lower()
                    score = fuzz.token_sort_ratio(target_name, cand_name)

                    if score >= self.similarity_threshold:
                        # Extract $0 URIs (Asteri, ISNI, VIAF)
                        for u0 in cand_agent.uri_subfield_0:
                            if u0 and u0 not in seen_uris:
                                seen_uris.add(u0)
                                vocab = detect_vocabulary(u0) or "OTHER"
                                discovered[idx].append(
                                    AgentURI(
                                        uri=u0,
                                        vocabulary=vocab,
                                        subfield_code="0",
                                        source_record_id=cand_rec.source_id,
                                        source=cand_rec.source,
                                        alignment_score=float(score),
                                    )
                                )

                        # Extract $1 URIs (Wikidata QIDs)
                        for u1 in cand_agent.uri_subfield_1:
                            if u1 and u1 not in seen_uris:
                                seen_uris.add(u1)
                                parsed_src, _ = parse_curie(u1)
                                vocab = detect_vocabulary(u1) or (parsed_src.upper() if parsed_src and parsed_src != "unknown" else "OTHER")
                                discovered[idx].append(
                                    AgentURI(
                                        uri=u1,
                                        vocabulary=vocab,
                                        subfield_code="1",
                                        source_record_id=cand_rec.source_id,
                                        source=cand_rec.source,
                                        alignment_score=float(score),
                                    )
                                )

        return discovered


def verify_life_dates(
    target_birth: Optional[int],
    candidate_birth: Optional[int],
    target_death: Optional[int],
    candidate_death: Optional[int],
) -> tuple[bool, str]:
    """Verify consistency between target agent life dates and candidate authority life dates.

    Returns (passed, reason).
    """
    if target_birth is not None and candidate_birth is not None:
        if abs(target_birth - candidate_birth) > 1:
            return (
                False,
                f"birth_year_mismatch: target {target_birth} vs candidate {candidate_birth}",
            )

    if target_death is not None and candidate_death is not None:
        if abs(target_death - candidate_death) > 1:
            return (
                False,
                f"death_year_mismatch: target {target_death} vs candidate {candidate_death}",
            )

    if target_birth is not None and candidate_death is not None:
        if target_birth > candidate_death:
            return (
                False,
                f"chronology_inversion: target birth {target_birth} after candidate death {candidate_death}",
            )

    if candidate_birth is not None and target_death is not None:
        if candidate_birth > target_death:
            return (
                False,
                f"chronology_inversion: candidate birth {candidate_birth} after target death {target_death}",
            )

    if (target_birth or target_death) and (candidate_birth or candidate_death):
        return True, "dates_confirmed"
    return True, "neutral_no_dates"


def verify_publication_chronology(
    pub_year: Optional[int],
    birth_year: Optional[int],
    death_year: Optional[int],
    min_author_age: int = 12,
    max_posthumous_years: int = 70,
    is_reprint: bool = False,
) -> tuple[bool, str]:
    """Verify that a target publication year is biologically and historically plausible for the candidate agent.

    Returns (passed, reason).
    """
    if pub_year is None:
        return True, "neutral_no_pub_year"

    if birth_year is not None:
        min_allowed_pub = birth_year + min_author_age
        if pub_year < min_allowed_pub:
            return False, (
                f"chronology_violation_pre_birth: published in {pub_year} before minimum author age "
                f"{min_author_age} (born {birth_year}, minimum publication year {min_allowed_pub})"
            )

    if death_year is not None and not is_reprint:
        max_allowed_pub = death_year + max_posthumous_years
        if pub_year > max_allowed_pub:
            return False, (
                f"chronology_violation_posthumous: published in {pub_year} which is > {max_posthumous_years} "
                f"years post mortem (died {death_year}, maximum publication year {max_allowed_pub})"
            )

    return True, "chronology_valid"


def verify_qualifier(
    target_qualifier: Optional[str],
    candidate_qualifier: Optional[str],
) -> tuple[bool, str]:
    """Verify compatibility of agent qualifiers (scalar helper).

    Returns (passed, reason).
    """
    if not target_qualifier or not candidate_qualifier:
        return True, "neutral_no_qualifier"

    t_norm = target_qualifier.strip().lower()
    c_norm = candidate_qualifier.strip().lower()

    if t_norm in c_norm or c_norm in t_norm:
        return True, "qualifier_matched"

    sim = fuzz.token_set_ratio(t_norm, c_norm)
    if sim >= 80.0:
        return True, "qualifier_similar"

    return False, "qualifier_mismatch"


def verify_orcid(
    target_orcid: Optional[str],
    candidate_orcid: Optional[str],
) -> tuple[bool, str]:
    """Verify persistent academic ORCID identifier.

    Returns (passed, reason).
    """
    if not target_orcid or not candidate_orcid:
        return True, "neutral_no_orcid"

    def _clean_orcid(raw: str) -> str:
        s = raw.strip().lower()
        s = s.replace("https://orcid.org/", "").replace("http://orcid.org/", "")
        return s.strip("/")

    t_clean = _clean_orcid(target_orcid)
    c_clean = _clean_orcid(candidate_orcid)

    if t_clean == c_clean:
        return True, "orcid_confirmed"
    return False, f"orcid_mismatch: target {target_orcid} vs candidate {candidate_orcid}"


def _verify_distribution_match(
    target_dist: dict[str, int],
    candidate_dist: dict[str, int],
    label: str,
    threshold: float = 80.0,
) -> tuple[bool, str]:
    """Verify compatibility of two string distribution bags using substring and fuzzy token matching."""
    if not target_dist or not candidate_dist:
        return True, f"neutral_no_{label}"

    for t_item in target_dist:
        t_norm = t_item.strip().lower()
        for c_item in candidate_dist:
            c_norm = c_item.strip().lower()
            if t_norm in c_norm or c_norm in t_norm:
                return True, f"{label}_matched"
            if fuzz.token_set_ratio(t_norm, c_norm) >= threshold:
                return True, f"{label}_similar"

    return False, f"{label}_mismatch"


def verify_qualifiers(
    target_qualifiers: dict[str, int],
    candidate_qualifiers: dict[str, int],
) -> tuple[bool, str]:
    """Verify compatibility of empirical agent qualifier distributions."""
    return _verify_distribution_match(target_qualifiers, candidate_qualifiers, "qualifier")


def verify_affiliations(
    target_affiliations: dict[str, int],
    candidate_affiliations: dict[str, int],
) -> tuple[bool, str]:
    """Verify compatibility of agent affiliation distributions (e.g. universities, institutes)."""
    return _verify_distribution_match(target_affiliations, candidate_affiliations, "affiliation")


def verify_languages(
    target_languages: dict[str, int],
    candidate_languages: dict[str, int],
) -> tuple[bool, str]:
    """Verify compatibility of agent language footprints.

    Returns (passed, reason).
    """
    if not target_languages or not candidate_languages:
        return True, "neutral_no_languages"

    common = set(target_languages.keys()) & set(candidate_languages.keys())
    if common:
        return True, "language_matched"

    return False, "language_mismatch"


def verify_subjects(
    target_subjects: dict[str, int],
    candidate_subjects: dict[str, int],
    vocab_store: Optional[Any] = None,
) -> tuple[bool, str]:
    """Verify thematic/subject domain compatibility between target and candidate agent ID distributions.

    Checks:
    1. Exact ID key match (e.g. 'yso:p2591' in both).
    2. Concept ID token match across vocabularies (e.g. 'p2591' from 'yso:p2591' and 'kauno:p2591').
    3. Normalized concept mapping match via vocab_store if available.

    Returns (passed, reason).
    """
    if not target_subjects or not candidate_subjects:
        return True, "neutral_no_subjects"

    # 1. Exact ID key match (e.g. 'yso:p2591' in both)
    common_keys = set(target_subjects.keys()) & set(candidate_subjects.keys())
    if common_keys:
        return True, "subject_matched"

    # 2. Concept ID match across vocabularies (e.g. 'p2591' in 'yso:p2591' and 'kauno:p2591')
    target_ids = {extract_concept_id(k).lower() for k in target_subjects if extract_concept_id(k)}
    cand_ids = {extract_concept_id(k).lower() for k in candidate_subjects if extract_concept_id(k)}
    if target_ids & cand_ids:
        return True, "subject_matched"

    # 3. Check normalized concept mapping in vocab_store
    if vocab_store is not None and hasattr(vocab_store, "find_legacy_mapping_by_uri"):
        from reconciliator.curie import format_source_uri

        for k in target_subjects:
            v_prefix, _, cid = k.partition(":")
            m = None
            if cid and hasattr(vocab_store, "find_legacy_mapping"):
                m = vocab_store.find_legacy_mapping(v_prefix, cid)
            if not m:
                uri = format_source_uri(v_prefix, cid) if cid else k
                m = vocab_store.find_legacy_mapping_by_uri(uri)
            if m:
                m_cid = extract_concept_id(getattr(m, "target_uri", ""))
                if m_cid and m_cid.lower() in cand_ids:
                    return True, "subject_matched"
        for k in candidate_subjects:
            v_prefix, _, cid = k.partition(":")
            m = None
            if cid and hasattr(vocab_store, "find_legacy_mapping"):
                m = vocab_store.find_legacy_mapping(v_prefix, cid)
            if not m:
                uri = format_source_uri(v_prefix, cid) if cid else k
                m = vocab_store.find_legacy_mapping_by_uri(uri)
            if m:
                m_cid = extract_concept_id(getattr(m, "target_uri", ""))
                if m_cid and m_cid.lower() in target_ids:
                    return True, "subject_matched"

    return False, "subject_mismatch"


def check_homonym_ambiguity(
    top_candidates: list[dict[str, Any]],
    score_margin: float = 0.03,
    target_birth: Optional[int] = None,
    target_death: Optional[int] = None,
    target_qualifier: Optional[str] = None,
    target_qualifiers: Optional[dict[str, int]] = None,
    target_affiliations: Optional[dict[str, int]] = None,
    target_languages: Optional[dict[str, int]] = None,
    target_subjects: Optional[dict[str, int]] = None,
    target_orcid: Optional[str] = None,
    authority_store: Optional[Any] = None,
    target_coauthors: Optional[list[str]] = None,
    target_publisher: Optional[str] = None,
    target_classifications: Optional[list[tuple[str, str]]] = None,
    vocab_store: Optional[Any] = None,
) -> tuple[bool, str]:
    """Detect if multiple candidates share near-identical names without distinguishing metadata.

    Tier 1 (In-Memory Footprint): Evaluates dates, ORCID, qualifier counts, affiliation counts, and language counts.
    Tier 2 (Relational SQLite Graph): Executes targeted SQL queries on contributions/agents/manifestations to evaluate
    co-author graph overlap, publisher history, and scheme-agnostic classification overlap.
    """
    if len(top_candidates) <= 1:
        return True, "unambiguous_single_candidate"

    if (
        vocab_store is None
        and authority_store is not None
        and hasattr(authority_store, "vocab_store")
    ):
        vocab_store = authority_store.vocab_store

    top_score = top_candidates[0].get("score", 0.0)
    close_candidates = [
        c for c in top_candidates if (top_score - c.get("score", 0.0)) <= score_margin
    ]

    if len(close_candidates) <= 1:
        return True, "unambiguous_margin"

    # Merge target_qualifier into target_qualifiers dict if provided
    effective_qualifiers = dict(target_qualifiers or {})
    if target_qualifier and target_qualifier not in effective_qualifiers:
        effective_qualifiers[target_qualifier] = 1

    # --- Tier 1 In-Memory Disambiguation ---
    # 1. ORCID check
    if target_orcid:
        matching_orcid_cands = [
            c
            for c in close_candidates
            if c.get("orcid") and verify_orcid(target_orcid, c.get("orcid"))[0]
        ]
        if len(matching_orcid_cands) == 1:
            return True, "ambiguity_resolved_by_orcid"

    # 2. Distinguishing dates check
    if target_birth is not None or target_death is not None:
        matching_date_cands = []
        for c in close_candidates:
            cb = c.get("birth_year")
            cd = c.get("death_year")
            passed, _ = verify_life_dates(target_birth, cb, target_death, cd)
            if passed and (cb is not None or cd is not None):
                matching_date_cands.append(c)
        if len(matching_date_cands) == 1:
            return True, "ambiguity_resolved_by_dates"

    # 3. Qualifier distribution overlap
    if effective_qualifiers:
        matching_q_cands = []
        for c in close_candidates:
            c_q_dict = c.get("qualifier_counts") or {}
            if not c_q_dict and c.get("qualifier"):
                c_q_dict = {str(c["qualifier"]): 1}
            if c_q_dict and verify_qualifiers(effective_qualifiers, c_q_dict)[0]:
                matching_q_cands.append(c)
        if len(matching_q_cands) == 1:
            return True, "ambiguity_resolved_by_qualifier"

    # 4. Affiliation distribution overlap
    if target_affiliations:
        matching_a_cands = []
        for c in close_candidates:
            c_a_dict = c.get("affiliation_counts") or {}
            if not c_a_dict and c.get("affiliation"):
                c_a_dict = {str(c["affiliation"]): 1}
            if c_a_dict and verify_affiliations(target_affiliations, c_a_dict)[0]:
                matching_a_cands.append(c)
        if len(matching_a_cands) == 1:
            return True, "ambiguity_resolved_by_affiliation"

    # 5. Language distribution overlap
    if target_languages:
        matching_l_cands = [
            c
            for c in close_candidates
            if c.get("language_counts")
            and verify_languages(target_languages, c["language_counts"])[0]
        ]
        if len(matching_l_cands) == 1:
            return True, "ambiguity_resolved_by_language"

    # 6. Subject distribution overlap
    if target_subjects:
        matching_s_cands = [
            c
            for c in close_candidates
            if c.get("subject_counts")
            and verify_subjects(target_subjects, c["subject_counts"], vocab_store=vocab_store)[0]
        ]
        if len(matching_s_cands) == 1:
            return True, "ambiguity_resolved_by_subjects"

    # --- Tier 2 Relational Deep Disambiguation (On-Demand SQLite Graph Query) ---
    if authority_store and hasattr(authority_store, "get_relational_disambiguation_evidence"):
        cand_ids = [c.get("id") for c in close_candidates if c.get("id") is not None]
        if cand_ids:
            rel_evidence = authority_store.get_relational_disambiguation_evidence(
                candidate_agent_ids=cand_ids,
                target_coauthors=target_coauthors or [],
                target_publisher=target_publisher,
                target_classifications=target_classifications or [],
            )
            scored_rel_cands: list[tuple[int, dict[str, Any]]] = []
            for c in close_candidates:
                cid = c.get("id")
                ev = rel_evidence.get(cid, {})
                matches = sum(
                    [
                        1 if ev.get("has_coauthor_match") else 0,
                        1 if ev.get("has_publisher_match") else 0,
                        1 if ev.get("has_classification_match") else 0,
                    ]
                )
                if matches > 0:
                    scored_rel_cands.append((matches, c))

            if scored_rel_cands:
                scored_rel_cands.sort(key=lambda x: x[0], reverse=True)
                if len(scored_rel_cands) == 1 or scored_rel_cands[0][0] > scored_rel_cands[1][0]:
                    return True, "ambiguity_resolved_by_relational_graph"

    names = [c.get("name", "") for c in close_candidates[:3]]
    return (
        False,
        f"ambiguous_homonyms: {len(close_candidates)} candidates with similar scores ({names})",
    )


class AgentFallbackResolver:
    """Evaluates agent fallback candidates against target agent using dynamic selective guards."""

    def __init__(
        self,
        similarity_threshold: float = 0.90,
        verify_chronology: bool = True,
        min_author_age: int = 12,
        max_posthumous_years: int = 70,
        strict_homonym_guard: bool = True,
    ) -> None:
        self.similarity_threshold = similarity_threshold
        self.verify_chronology = verify_chronology
        self.min_author_age = min_author_age
        self.max_posthumous_years = max_posthumous_years
        self.strict_homonym_guard = strict_homonym_guard

    def evaluate_candidates(
        self,
        target_agent: Agent,
        target_pub_year: Optional[int],
        candidates: list[dict[str, Any]],
        authority_store: Optional[Any] = None,
        target_coauthors: Optional[list[str]] = None,
        target_publisher: Optional[str] = None,
        target_classifications: Optional[list[tuple[str, str]]] = None,
        target_subjects: Optional[dict[str, int]] = None,
        vocab_store: Optional[Any] = None,
    ) -> tuple[Optional[dict[str, Any]], str]:
        """Filter and select the best candidate agent via dynamic selective guards.

        Returns (best_candidate, resolution_reason).
        """
        if not candidates:
            return None, "no_candidates"

        if target_agent.is_fictional:
            return None, "fictional_agent_skipped"

        # 1. Filter candidates by score threshold
        viable = [c for c in candidates if c.get("score", 0.0) >= self.similarity_threshold]
        if not viable:
            return None, f"all_candidates_below_threshold_{self.similarity_threshold}"

        # 2. Check homonym ambiguity if enabled
        if self.strict_homonym_guard:
            passed, homonym_reason = check_homonym_ambiguity(
                viable,
                target_birth=target_agent.birth_year,
                target_death=target_agent.death_year,
                target_orcid=target_agent.orcid,
                target_qualifiers=target_agent.qualifier_counts,
                target_affiliations=target_agent.affiliation_counts,
                target_languages=target_agent.language_counts,
                target_subjects=target_subjects or target_agent.subject_counts,
                authority_store=authority_store,
                target_coauthors=target_coauthors,
                target_publisher=target_publisher,
                target_classifications=target_classifications,
                vocab_store=vocab_store,
            )
            if not passed:
                return None, homonym_reason

        # 3. Evaluate each viable candidate with dynamic guards
        for cand in viable:
            # ORCID guard: if both have ORCID, verify exact match
            if target_agent.orcid and cand.get("orcid"):
                orcid_ok, orcid_reason = verify_orcid(target_agent.orcid, str(cand["orcid"]))
                if not orcid_ok:
                    continue

            # Life dates guard
            cand_birth = cand.get("birth_year")
            cand_death = cand.get("death_year")
            date_ok, date_reason = verify_life_dates(
                target_agent.birth_year,
                cand_birth,
                target_agent.death_year,
                cand_death,
            )
            if not date_ok:
                continue

            # Chronology guard
            if self.verify_chronology:
                chrono_ok, chrono_reason = verify_publication_chronology(
                    target_pub_year,
                    cand_birth,
                    cand_death,
                    min_author_age=self.min_author_age,
                    max_posthumous_years=self.max_posthumous_years,
                )
                if not chrono_ok:
                    continue

            # Surname guard: if target has surname, candidate must match surname token
            if target_agent.surname and cand.get("surname"):
                s_target = target_agent.surname.strip().lower()
                s_cand = str(cand["surname"]).strip().lower()
                if s_target != s_cand and fuzz.ratio(s_target, s_cand) < 85.0:
                    continue

            # Qualifier distribution guard
            cand_q = cand.get("qualifier_counts") or {}
            if not cand_q and cand.get("qualifier"):
                cand_q = {str(cand["qualifier"]): 1}
            if target_agent.qualifier_counts and cand_q:
                q_ok, _ = verify_qualifiers(target_agent.qualifier_counts, cand_q)
                if not q_ok:
                    continue

            # Affiliation distribution guard
            cand_a = cand.get("affiliation_counts") or {}
            if not cand_a and cand.get("affiliation"):
                cand_a = {str(cand["affiliation"]): 1}
            if target_agent.affiliation_counts and cand_a:
                a_ok, _ = verify_affiliations(target_agent.affiliation_counts, cand_a)
                if not a_ok:
                    continue

            # Candidate passed all active dynamic selective guards!
            return cand, "accepted_via_selective_guards"

        return None, "all_candidates_rejected_by_guards"
