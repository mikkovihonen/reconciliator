"""Tests for dynamic selective guards and AgentFallbackResolver."""

from __future__ import annotations

from reconciliator.agent_transfer import (
    AgentFallbackResolver,
    check_homonym_ambiguity,
    verify_affiliations,
    verify_languages,
    verify_life_dates,
    verify_orcid,
    verify_publication_chronology,
    verify_qualifier,
    verify_qualifiers,
    verify_subjects,
)
from reconciliator.models import Agent
from reconciliator.repository import WEMIRepository


def test_verify_life_dates() -> None:
    # Exact match
    ok, reason = verify_life_dates(1908, 1908, 1979, 1979)
    assert ok is True
    assert reason == "dates_confirmed"

    # Permissible 1-year calendar discrepancy
    ok, _ = verify_life_dates(1908, 1909, None, None)
    assert ok is True

    # Direct birth year contradiction
    ok, reason = verify_life_dates(1908, 1950, None, None)
    assert ok is False
    assert "birth_year_mismatch" in reason

    # Direct death year contradiction
    ok, reason = verify_life_dates(None, None, 1979, 2010)
    assert ok is False
    assert "death_year_mismatch" in reason

    # Inverted chronology: target born after candidate died
    ok, reason = verify_life_dates(1985, None, None, 1950)
    assert ok is False
    assert "chronology_inversion" in reason

    # Neutral when missing
    ok, reason = verify_life_dates(None, None, None, None)
    assert ok is True
    assert reason == "neutral_no_dates"


def test_verify_publication_chronology_configurable() -> None:
    # Plausible publication
    ok, _ = verify_publication_chronology(
        pub_year=1950,
        birth_year=1920,
        death_year=1990,
        min_author_age=12,
        max_posthumous_years=70,
    )
    assert ok is True

    # Pre-birth violation: published when author is 5 years old (min_age=12)
    ok, reason = verify_publication_chronology(
        pub_year=1925,
        birth_year=1920,
        death_year=1990,
        min_author_age=12,
    )
    assert ok is False
    assert "chronology_violation_pre_birth" in reason

    # Custom min_author_age allows publication at age 5
    ok, _ = verify_publication_chronology(
        pub_year=1925,
        birth_year=1920,
        death_year=1990,
        min_author_age=4,
    )
    assert ok is True

    # Posthumous violation: published 80 years after death (default max_posthumous=70)
    ok, reason = verify_publication_chronology(
        pub_year=2000,
        birth_year=1850,
        death_year=1910,
        max_posthumous_years=70,
    )
    assert ok is False
    assert "chronology_violation_posthumous" in reason

    # Custom max_posthumous_years allows publication 100 years after death
    ok, _ = verify_publication_chronology(
        pub_year=2000,
        birth_year=1850,
        death_year=1910,
        max_posthumous_years=100,
    )
    assert ok is True

    # Reprint allows publication long after death
    ok, _ = verify_publication_chronology(
        pub_year=2000,
        birth_year=1850,
        death_year=1910,
        max_posthumous_years=70,
        is_reprint=True,
    )
    assert ok is True


def test_verify_qualifier() -> None:
    # Exact or substring match
    ok, reason = verify_qualifier("professori", "professori")
    assert ok is True
    assert reason == "qualifier_matched"

    ok, reason = verify_qualifier("professori", "fysiikan professori")
    assert ok is True
    assert reason == "qualifier_matched"

    # Similar
    ok, reason = verify_qualifier("säveltäjä", "säveltäjä ja muusikko")
    assert ok is True
    assert reason == "qualifier_matched"

    # Neutral
    ok, reason = verify_qualifier(None, "professori")
    assert ok is True
    assert reason == "neutral_no_qualifier"


def test_check_homonym_ambiguity() -> None:
    cands = [
        {"name": "Matti Virtanen", "score": 0.95, "birth_year": None, "death_year": None},
        {"name": "Matti Virtanen", "score": 0.94, "birth_year": None, "death_year": None},
    ]

    # Two identical candidates without distinguishing dates/qualifiers -> reject!
    ok, reason = check_homonym_ambiguity(cands, score_margin=0.03)
    assert ok is False
    assert "ambiguous_homonyms" in reason

    # Resolved by dates
    cands_with_dates = [
        {"name": "Matti Virtanen", "score": 0.95, "birth_year": 1920, "death_year": 1990},
        {"name": "Matti Virtanen", "score": 0.94, "birth_year": 1965, "death_year": None},
    ]
    ok, reason = check_homonym_ambiguity(
        cands_with_dates, score_margin=0.03, target_birth=1920, target_death=1990
    )
    assert ok is True
    assert reason == "ambiguity_resolved_by_dates"

    # Resolved by qualifier counts
    cands_with_qualifier = [
        {"name": "Matti Virtanen", "score": 0.95, "qualifier_counts": {"lääkäri": 1}},
        {"name": "Matti Virtanen", "score": 0.94, "qualifier_counts": {"kuvataiteilija": 1}},
    ]
    ok, reason = check_homonym_ambiguity(
        cands_with_qualifier, score_margin=0.03, target_qualifiers={"lääkäri": 1}
    )
    assert ok is True
    assert reason == "ambiguity_resolved_by_qualifier"

    # Resolved by affiliation counts
    cands_with_affil = [
        {"name": "Matti Virtanen", "score": 0.95, "affiliation_counts": {"Helsingin yliopisto": 5}},
        {"name": "Matti Virtanen", "score": 0.94, "affiliation_counts": {"Turun yliopisto": 3}},
    ]
    ok, reason = check_homonym_ambiguity(
        cands_with_affil, score_margin=0.03, target_affiliations={"Helsingin yliopisto": 1}
    )
    assert ok is True
    assert reason == "ambiguity_resolved_by_affiliation"

    # Resolved by language counts
    cands_with_lang = [
        {"name": "Matti Virtanen", "score": 0.95, "language_counts": {"fin": 10}},
        {"name": "Matti Virtanen", "score": 0.94, "language_counts": {"swe": 8}},
    ]
    ok, reason = check_homonym_ambiguity(
        cands_with_lang, score_margin=0.03, target_languages={"fin": 2}
    )
    assert ok is True
    assert reason == "ambiguity_resolved_by_language"

    # Resolved by subject counts
    cands_with_subj = [
        {
            "name": "Matti Virtanen",
            "score": 0.95,
            "subject_counts": {"yso:historia": 12, "slm:elämäkerrat": 4},
        },
        {"name": "Matti Virtanen", "score": 0.94, "subject_counts": {"yso:fysiikka": 20}},
    ]
    ok, reason = check_homonym_ambiguity(
        cands_with_subj, score_margin=0.03, target_subjects={"yso:historia": 1}
    )
    assert ok is True
    assert reason == "ambiguity_resolved_by_subjects"

    # Resolved by ORCID
    cands_with_orcid = [
        {"name": "Matti Virtanen", "score": 0.95, "orcid": "0000-0002-1825-0097"},
        {"name": "Matti Virtanen", "score": 0.94, "orcid": "0000-0001-5109-3700"},
    ]
    ok, reason = check_homonym_ambiguity(
        cands_with_orcid, score_margin=0.03, target_orcid="https://orcid.org/0000-0002-1825-0097"
    )
    assert ok is True
    assert reason == "ambiguity_resolved_by_orcid"

    # Resolved by Tier 2 Relational Deep Disambiguation
    class MockAuthorityStore:
        def get_relational_disambiguation_evidence(self, candidate_agent_ids, **kwargs):
            return {
                1: {
                    "has_coauthor_match": True,
                    "has_publisher_match": True,
                    "has_classification_match": False,
                },
                2: {
                    "has_coauthor_match": False,
                    "has_publisher_match": False,
                    "has_classification_match": False,
                },
            }

    cands_rel = [
        {"id": 1, "name": "Matti Virtanen", "score": 0.95},
        {"id": 2, "name": "Matti Virtanen", "score": 0.94},
    ]
    ok, reason = check_homonym_ambiguity(
        cands_rel,
        score_margin=0.03,
        authority_store=MockAuthorityStore(),
        target_coauthors=["Kivi, Aleksis"],
    )
    assert ok is True
    assert reason == "ambiguity_resolved_by_relational_graph"


def test_agent_fallback_resolver_e2e() -> None:
    resolver = AgentFallbackResolver(
        similarity_threshold=0.90,
        verify_chronology=True,
        min_author_age=12,
        max_posthumous_years=70,
        strict_homonym_guard=True,
    )

    target = Agent(
        name="Virtanen, Matti",
        surname="Virtanen",
        birth_year=1920,
        death_year=1995,
        qualifier_counts={"professori": 1},
    )

    # 1. Candidate with matching dates accepted
    cands = [
        {
            "name": "Virtanen, Matti",
            "surname": "Virtanen",
            "birth_year": 1920,
            "death_year": 1995,
            "qualifier_counts": {"professori": 1},
            "score": 0.96,
            "agent_uri": "http://urn.fi/URN:NBN:fi:au:pn:0001",
        }
    ]
    best, reason = resolver.evaluate_candidates(target, target_pub_year=1960, candidates=cands)
    assert best is not None
    assert best["agent_uri"] == "http://urn.fi/URN:NBN:fi:au:pn:0001"
    assert reason == "accepted_via_selective_guards"

    # 2. Candidate with contradictory dates rejected
    cands_wrong_date = [
        {
            "name": "Virtanen, Matti",
            "surname": "Virtanen",
            "birth_year": 1965,
            "death_year": None,
            "score": 0.96,
            "agent_uri": "http://urn.fi/URN:NBN:fi:au:pn:0002",
        }
    ]
    best, reason = resolver.evaluate_candidates(
        target, target_pub_year=1960, candidates=cands_wrong_date
    )
    assert best is None
    assert reason == "all_candidates_rejected_by_guards"

    # 3. Chronologically impossible candidate rejected
    cands_impossible = [
        {
            "name": "Virtanen, Matti",
            "surname": "Virtanen",
            "birth_year": 1970,  # born 1970, book published 1960!
            "death_year": None,
            "score": 0.95,
        }
    ]
    target_no_dates = Agent(name="Virtanen, Matti", surname="Virtanen")
    best, reason = resolver.evaluate_candidates(
        target_no_dates, target_pub_year=1960, candidates=cands_impossible
    )
    assert best is None
    assert reason == "all_candidates_rejected_by_guards"


def test_verify_orcid() -> None:
    ok, reason = verify_orcid("https://orcid.org/0000-0002-1825-0097", "0000-0002-1825-0097")
    assert ok is True
    assert reason == "orcid_confirmed"

    ok, reason = verify_orcid("0000-0002-1825-0097", "0000-0001-5109-3700")
    assert ok is False
    assert "orcid_mismatch" in reason

    ok, reason = verify_orcid(None, "0000-0002-1825-0097")
    assert ok is True
    assert reason == "neutral_no_orcid"


def test_verify_qualifiers_distribution() -> None:
    # Overlap
    ok, reason = verify_qualifiers({"professori": 5, "lääkäri": 1}, {"lääkäri": 10})
    assert ok is True
    assert reason == "qualifier_matched"

    # Similar
    ok, reason = verify_qualifiers({"säveltäjä": 1}, {"säveltäjä ja kapellimestari": 2})
    assert ok is True
    assert reason in ("qualifier_matched", "qualifier_similar")

    # Disjoint
    ok, reason = verify_qualifiers({"professori": 1}, {"valokuvaaja": 1})
    assert ok is False
    assert reason == "qualifier_mismatch"

    # Empty / neutral
    ok, reason = verify_qualifiers({}, {"professori": 1})
    assert ok is True
    assert reason == "neutral_no_qualifier"


def test_verify_affiliations_distribution() -> None:
    ok, reason = verify_affiliations(
        {"Helsingin yliopisto": 3}, {"Helsingin yliopiston lääketieteellinen tiedekunta": 1}
    )
    assert ok is True
    assert reason in ("affiliation_matched", "affiliation_similar")

    ok, reason = verify_affiliations({"Aalto-yliopisto": 1}, {"Turun yliopisto": 1})
    assert ok is False
    assert reason == "affiliation_mismatch"

    ok, reason = verify_affiliations({}, {"Aalto-yliopisto": 1})
    assert ok is True
    assert reason == "neutral_no_affiliation"


def test_verify_languages_distribution() -> None:
    ok, reason = verify_languages({"fin": 5, "swe": 2}, {"swe": 10, "eng": 3})
    assert ok is True
    assert reason == "language_matched"

    ok, reason = verify_languages({"fin": 5}, {"ger": 3})
    assert ok is False
    assert reason == "language_mismatch"

    ok, reason = verify_languages({}, {"fin": 1})
    assert ok is True
    assert reason == "neutral_no_languages"


def test_verify_subjects_distribution() -> None:
    # 1. Exact match with same vocabulary prefix
    ok, reason = verify_subjects({"yso:p2591": 5}, {"yso:p2591": 10, "yso:p1024": 2})
    assert ok is True
    assert reason == "subject_matched"

    # 2. Cross-vocabulary match with identical concept ID token
    ok, reason = verify_subjects({"yso:p2591": 3}, {"kauno:p2591": 8})
    assert ok is True
    assert reason == "subject_matched"

    # 3. Neutral on empty distributions
    ok, reason = verify_subjects({}, {"yso:p2591": 5})
    assert ok is True
    assert reason == "neutral_no_subjects"

    ok, reason = verify_subjects({"yso:p2591": 5}, {})
    assert ok is True
    assert reason == "neutral_no_subjects"

    # 4. Direct subject mismatch
    ok, reason = verify_subjects({"yso:p2591": 5}, {"yso:p1054": 10})
    assert ok is False
    assert reason == "subject_mismatch"


def test_verify_subjects_with_concept_ids_and_vocab_mapping() -> None:
    """Verify that verify_subjects matches concept IDs and checks mapped concept URIs via vocab_store."""

    class MockMapping:
        target_uri = "http://www.yso.fi/onto/yso/p2591"

    class MockVocabStore:
        def find_legacy_mapping_by_uri(self, uri: str):
            if "p9999" in uri:
                return MockMapping()
            return None

    store = MockVocabStore()

    # 1. Matches via mapped concept in vocab_store
    ok, reason = verify_subjects({"ysa:p9999": 1}, {"yso:p2591": 2}, vocab_store=store)
    assert ok is True
    assert reason == "subject_matched"

    # 2. Mismatch when no mapping exists
    ok, reason = verify_subjects({"yso:p1054": 1}, {"yso:p2591": 2}, vocab_store=store)
    assert ok is False
    assert reason == "subject_mismatch"


def test_check_homonym_ambiguity_resolved_by_subject_ids() -> None:
    """Verify check_homonym_ambiguity resolves homonyms when one candidate matches target subject IDs."""
    cands = [
        {
            "id": 101,
            "name": "Virtanen, Matti",
            "score": 0.95,
            "subject_counts": {"yso:p2591": 5},  # historia
        },
        {
            "id": 102,
            "name": "Virtanen, Matti",
            "score": 0.94,
            "subject_counts": {"yso:p1054": 8},  # fysiikka
        },
    ]
    # Target has subject ID p2591 -> candidate 101 matches uniquely
    ok, reason = check_homonym_ambiguity(
        cands,
        target_subjects={"yso:p2591": 2},
    )
    assert ok is True
    assert reason == "ambiguity_resolved_by_subjects"


def test_wemi_repository_relational_disambiguation_evidence(tmp_path) -> None:
    """Co-author, publisher and classification overlap resolve a homonym pair."""
    from reconciliator.config import VectorEngineConfig
    from reconciliator.models import AgentEntity

    vec_cfg = VectorEngineConfig(backend="usearch", device="cpu", mode="embedded")
    store = WEMIRepository(
        db_path=tmp_path / "authority.db",
        work_index_path=tmp_path / "work.index",
        dimension=8,
        vector_engine_config=vec_cfg,
    )

    # Insert agents
    agent_1920_id = store.insert_agents_batch(
        [
            AgentEntity(
                agent_uri="http://example.org/agent1",
                source="test",
                name="Virtanen, Matti",
                name_normalized="virtanen matti",
                surname="Virtanen",
                dates="1920-1990",
                birth_year=1920,
                death_year=1990,
            ),
            AgentEntity(
                agent_uri="http://example.org/agent2",
                source="test",
                name="Mäkinen, Kalle",
                name_normalized="mäkinen kalle",
                surname="Mäkinen",
            ),
            AgentEntity(
                agent_uri="http://example.org/agent3",
                source="test",
                name="Virtanen, Matti",
                name_normalized="virtanen matti",
                surname="Virtanen",
                dates="1950-2010",
                birth_year=1950,
                death_year=2010,
            ),
            AgentEntity(
                agent_uri="http://example.org/agent4",
                source="test",
                name="Aaltonen, Pekka",
                name_normalized="aaltonen pekka",
                surname="Aaltonen",
            ),
        ]
    )

    agent_1920 = agent_1920_id[0]
    agent_1950 = agent_1920_id[2]

    # Insert works
    from reconciliator.models import ManifestationEntity, WorkEntity

    work = WorkEntity(
        work_uri="http://example.org/work/rec-001",
        source="test",
        source_id="rec-001",
        title="Yhteistyöteos",
        title_normalized="yhteistyöteos",
        primary_agent_id=agent_1920,
    )
    store.insert_work(work)

    # Insert manifestations with publisher
    store.insert_manifestation(
        ManifestationEntity(
            manifestation_uri="http://example.org/manifest/rec-001",
            source="test",
            source_id="rec-001",
            title="Yhteistyöteos (Otava)",
            title_normalized="yhteistyöteos otava",
            publisher="Otava",
            carrier_type="volume",
        )
    )
    store.insert_manifestation(
        ManifestationEntity(
            manifestation_uri="http://example.org/manifest/rec-002",
            source="test",
            source_id="rec-002",
            title="Toinen teos (Gummerus)",
            title_normalized="toinen teos gummerus",
            publisher="Gummerus",
            carrier_type="volume",
        )
    )

    # Insert contributions
    from reconciliator.models import ContributionEntity

    store.insert_contributions_batch(
        [
            ContributionEntity(
                entity_type="manifestation",
                entity_id=1,
                agent_id=agent_1920,
                role="aut",
                is_primary=True,
            ),
            ContributionEntity(
                entity_type="manifestation",
                entity_id=1,
                agent_id=agent_1920_id[1],
                role="aut",
                is_primary=False,
            ),
            ContributionEntity(
                entity_type="manifestation",
                entity_id=2,
                agent_id=agent_1950,
                role="aut",
                is_primary=True,
            ),
        ]
    )

    # Query relational evidence for target record with coauthor "Mäkinen, Kalle", publisher "Otava"
    evidence = store.get_relational_disambiguation_evidence(
        candidate_agent_ids=[agent_1920, agent_1950],
        target_coauthors=["Mäkinen, Kalle"],
        target_publisher="Otava",
        target_classifications=[],
    )

    ev1 = evidence[agent_1920]
    assert ev1["has_coauthor_match"] is True
    assert "Mäkinen, Kalle" in ev1["matched_coauthors"]
    assert ev1["has_publisher_match"] is True
    assert "Otava" in ev1["matched_publishers"]
    assert ev1["has_classification_match"] is False

    ev2 = evidence[agent_1950]
    assert ev2["has_coauthor_match"] is False
    assert ev2["has_publisher_match"] is False
    assert ev2["has_classification_match"] is False
