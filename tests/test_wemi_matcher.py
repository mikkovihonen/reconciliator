"""Comprehensive test suite for WorkMatcher and the 42 specialized domain funnels."""

from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pytest

from reconciliator.indexer import TorchVectorEngine
from reconciliator.reconciliation import WorkMatcher
from reconciliator.models import (
    AgentEntity,
    ExpressionEntity,
    IdentifierEntity,
    EntitySubject,
    ManifestationEntity,
    ManifestationExpressionAssociation,
    MatchRecord,
    WorkEntity,
    WorkRelationship,
)
from reconciliator.database import WEMIRepository
from reconciliator.utils import compute_jaccard_overlap, extract_identifiers_from_target


@pytest.fixture
def repo(tmp_path: Path) -> WEMIRepository:
    """Fixture providing initialized WEMIRepository with vector indexes."""
    db_path = tmp_path / "authority.db"
    w_idx = TorchVectorEngine(tmp_path / "work.pt", dimension=4)
    m_idx = TorchVectorEngine(tmp_path / "manif.pt", dimension=4)
    return WEMIRepository(
        db_path=db_path,
        dimension=4,
        work_indexer=w_idx,
        manifestation_indexer=m_idx,
    )


def test_jaccard_overlap_helper():
    """Verify the set Jaccard helper.

    The bounding-box IoU helper it used to share this test with is gone: it had
    no production caller, and funnel L's docstring promising "bounding box IoU
    (034)" never called it either. Spatial overlap matching is now a handler-sized
    job if it is wanted.
    """
    s1 = {"Track 1", "Track 2", "Track 3"}
    s2 = {"Track 2", "Track 3", "Track 4"}
    assert compute_jaccard_overlap(s1, s2) == 0.5
    assert compute_jaccard_overlap(set(), s2) == 0.0


def test_extract_identifiers_from_target():
    """Identifiers come from the record, not from re-parsing its payload.

    They are extracted once by the loader and carried on the MatchRecord. The
    payload is deliberately NOT consulted: the old parse was a second full parse
    per match attempt, and on the reconcile path it parsed JSON as XML, threw,
    was swallowed, and returned nothing.
    """
    target = MatchRecord(
        record_id="rec-12345",
        format="marcxml",
        work_type="text",
        title="Test Title",
        title_stem="test title",
        work_blocking_key="key123",
        swhid_key="swh:1:rev:123456",
        identifiers={
            "isbn": ["978-951-0-12345-6"],
            "issn": ["0042-6806"],
            "doi": ["10.1234/test.doi"],
        },
    )

    ids = extract_identifiers_from_target(target)
    assert ids["isbn"] == ["978-951-0-12345-6"]
    assert ids["issn"] == ["0042-6806"]
    assert ids["doi"] == ["10.1234/test.doi"]
    # Domain keys still contribute their derived schemes.
    assert "swh:1:rev:123456" in ids["swhid"]


def test_identifiers_are_not_read_from_the_payload():
    """Regression guard for the tier-1 defect: a payload alone yields nothing."""
    raw_marc = b"""<record xmlns="http://www.loc.gov/MARC21/slim">
        <datafield tag="020" ind1=" " ind2=" ">
            <subfield code="a">978-951-0-12345-6</subfield>
        </datafield>
    </record>"""
    target = MatchRecord(
        record_id="rec-1",
        format="marcxml",
        title="T",
        title_stem="t",
        raw_payload=raw_marc,
    )
    assert extract_identifiers_from_target(target) == {}


def test_analytics_come_from_keys_not_the_payload():
    from reconciliator.loaders.marcxml import encode_analytic
    from reconciliator.utils import extract_target_analytics, extract_target_track_titles

    target = MatchRecord(
        record_id="rec-1",
        format="marcxml",
        title="Kokoelma",
        title_stem="kokoelma",
        keys={
            "analytic": [
                encode_analytic("Finlandia", "Sibelius, Jean"),
                encode_analytic("Valse triste", None),
            ]
        },
    )
    assert extract_target_analytics(target) == [
        ("Finlandia", "Sibelius, Jean"),
        ("Valse triste", None),
    ]
    assert extract_target_track_titles(target) == {"Finlandia", "Valse triste"}


def test_work_matcher_fast_path_isbn(repo: WEMIRepository):
    """Verify Tier 1 fast-path matching on ISBN."""
    # Seed authority DB with Work, Expression, Manifestation, and Identifier
    work = WorkEntity(
        work_uri="http://example.org/work/sinuhe",
        source="melinda",
        source_id="sinuhe_work",
        title="Sinuhe egyptiläinen",
        title_normalized="sinuhe egyptilainen",
        original_language="fin",
    )
    work_id = repo.insert_works_batch([work])[0]

    expr = ExpressionEntity(
        work_id=work_id,
        expression_uri="http://example.org/expr/sinuhe_fin",
        source="melinda",
        source_id="sinuhe_expr_fin",
        language="fin",
        content_type="text",
        title="Sinuhe egyptiläinen",
    )
    expr_id = repo.insert_expressions_batch([expr])[0]

    manif = ManifestationEntity(
        manifestation_uri="http://example.org/manif/sinuhe_1945",
        source="melinda",
        source_id="sinuhe_manif_1945",
        work_id=work_id,
        title="Sinuhe egyptiläinen",
        title_normalized="sinuhe egyptilainen",
        publication_year=1945,
    )
    manif_id = repo.insert_manifestations_batch([manif])[0]

    repo.insert_manifestation_expressions_batch(
        [
            ManifestationExpressionAssociation(
                manifestation_id=manif_id,
                expression_id=expr_id,
                is_primary=True,
            )
        ]
    )

    repo.insert_identifiers_batch(
        [
            IdentifierEntity(
                entity_type="manifestation",
                entity_id=manif_id,
                scheme="isbn",
                value="978-951-0-12345-6",
                value_normalized="9789510123456",
            )
        ]
    )

    matcher = WorkMatcher(wemi_repo=repo)

    target = MatchRecord(
        record_id="target_001",
        format="marcxml",
        work_type="text",
        title="Sinuhe egyptiläinen",
        title_stem="sinuhe egyptilainen",
        work_blocking_key="waltari::sinuhe",
        standard_key="978-951-0-12345-6",
        raw_payload=b"",
    )

    result = matcher.match_target(target)
    assert result.match_tier == "tier1_exact_identifier"
    assert result.confidence_score == 1.0
    assert result.matched_work is not None
    assert result.matched_work.work_uri == work.curie
    assert result.matched_expression is not None
    assert result.matched_expression.language == "fin"
    assert result.matched_manifestation is not None
    assert result.matched_manifestation.manifestation_uri == manif.curie


def test_work_matcher_monograph_author_title_and_vector_fallback(repo: WEMIRepository):
    """Verify Tier 2 author+title stem matching and Work vector fallback for monographs."""
    agent = AgentEntity(
        agent_uri="http://kanto.fi/agent/linna",
        source="asteri",
        name="Väinö Linna",
        name_normalized="vaino linna",
        surname="Linna",
    )
    agent_id = repo.insert_agents_batch([agent])[0]

    work_vec = np.array([[1.0, 0.0, 0.0, 0.0]], dtype=np.float32)
    work = WorkEntity(
        work_uri="http://example.org/work/tuntematon",
        source="melinda",
        source_id="linna_tuntematon",
        title="Tuntematon sotilas",
        title_normalized="tuntematon sotilas",
        primary_agent_id=agent_id,
        original_language="fin",
    )
    work_id = repo.insert_works_batch([work], vectors=work_vec)[0]

    expr = ExpressionEntity(
        work_id=work_id,
        expression_uri="http://example.org/expr/tuntematon_fin",
        source="melinda",
        source_id="linna_tuntematon_fin",
        language="fin",
        content_type="text",
        title="Tuntematon sotilas",
    )
    repo.insert_expressions_batch([expr])

    matcher = WorkMatcher(wemi_repo=repo)

    # 1. Match via author key and title stem
    target1 = MatchRecord(
        record_id="target_linna_01",
        format="marcxml",
        work_type="text",
        title="Tuntematon sotilas",
        title_stem="tuntematon sotilas",
        author_key="linna",
        work_blocking_key="linna::tuntematon sotilas",
        raw_payload=b"",
    )
    res1 = matcher.match_target(target1)
    assert res1.match_tier == "tier2_domain_key"
    assert res1.matched_work is not None
    assert res1.matched_work.work_uri == work.curie

    # 2. Vector fallback for uncataloged edition with altered wording
    mock_embedder = MagicMock()
    mock_embedder.embed_texts.return_value = np.array([[0.99, 0.05, 0.0, 0.0]], dtype=np.float32)
    matcher_vec = WorkMatcher(wemi_repo=repo, embedder=mock_embedder)

    target_uncataloged = MatchRecord(
        record_id="target_uncataloged",
        format="marcxml",
        work_type="text",
        title="En okänd soldat (roman)",
        title_stem="en okand soldat roman",
        author_key="okand_forfattare",
        work_blocking_key="misc_key",
        raw_payload=b"",
    )
    res_vec = matcher_vec.match_target(target_uncataloged)
    assert res_vec.match_tier == "tier2_vector_fallback"
    assert res_vec.matched_work is not None
    assert res_vec.matched_work.work_uri == work.curie


def test_work_matcher_audiobook_cross_format(repo: WEMIRepository):
    """Verify Funnel E (Audiobooks) cross-format linking to underlying textual Work."""
    work = WorkEntity(
        work_uri="http://example.org/work/sinuhe",
        source="melinda",
        source_id="sinuhe_work",
        title="Sinuhe egyptiläinen",
        title_normalized="sinuhe egyptilainen",
        work_type="text",
        original_language="fin",
    )
    work_id = repo.insert_works_batch([work])[0]

    text_expr = ExpressionEntity(
        work_id=work_id,
        expression_uri="http://example.org/expr/sinuhe_fin_print",
        source="melinda",
        source_id="sinuhe_expr_print",
        language="fin",
        content_type="text",
        title="Sinuhe egyptiläinen",
    )
    repo.insert_expressions_batch([text_expr])

    matcher = WorkMatcher(wemi_repo=repo)

    # Audiobook target record
    audio_target = MatchRecord(
        record_id="audio_001",
        format="marcxml",
        work_type="audio_nonmusic",
        title="Sinuhe egyptiläinen",
        title_stem="sinuhe egyptilainen",
        author_key="waltari",
        work_blocking_key="waltari::sinuhe",
        language="fin",
        raw_payload=b"",
    )

    res = matcher.match_target(audio_target)
    assert res.funnel_used == "funnel_e_audiobook"
    assert res.match_tier == "cross_format"
    assert res.matched_work is not None
    assert res.matched_work.work_uri == work.curie
    assert res.matched_work.work_type == "text"
    assert res.matched_expression is not None
    assert res.matched_expression.content_type == "spoken_word"


def test_work_matcher_recorded_sound_and_tracklist(repo: WEMIRepository):
    """Verify Funnel B (Recorded Sound) publisher number and performer alignment."""
    # Music work
    m_work = WorkEntity(
        work_uri="http://example.org/work/sibelius_finlandia",
        source="melinda",
        source_id="finlandia_work",
        title="Finlandia, op. 26",
        title_normalized="finlandia op 26",
        work_type="music",
    )
    w_id = repo.insert_works_batch([m_work])[0]

    manif = ManifestationEntity(
        manifestation_uri="http://example.org/manif/finlandia_cd",
        source="melinda",
        source_id="finlandia_cd",
        work_id=w_id,
        title="Finlandia CD",
        title_normalized="finlandia cd",
    )
    m_id = repo.insert_manifestations_batch([manif])[0]

    repo.insert_identifiers_batch(
        [
            IdentifierEntity(
                entity_type="manifestation",
                entity_id=m_id,
                scheme="publisher_number",
                value="ODE 1020-2",
                value_normalized="ode10202",
            )
        ]
    )

    matcher = WorkMatcher(wemi_repo=repo)

    # 1. Fast path publisher number
    target_cd = MatchRecord(
        record_id="cd_target_01",
        format="marcxml",
        work_type="music",
        title="Finlandia CD",
        title_stem="finlandia cd",
        work_blocking_key="sibelius::finlandia",
        standard_key="ODE 1020-2",
        raw_payload=b"",
    )
    res = matcher.match_target(target_cd)
    assert res.funnel_used == "funnel_b_music_sound"
    assert res.match_tier == "tier1_exact_identifier"
    assert res.matched_work is not None
    assert res.matched_work.work_uri == m_work.curie


def test_work_matcher_notated_music_and_mop(repo: WEMIRepository):
    """Verify Funnel D (Notated Music) ISMN and Medium of Performance alignment."""
    work = WorkEntity(
        work_uri="http://example.org/work/karelia_suite",
        source="melinda",
        source_id="karelia_suite",
        title="Karelia-sarja, op. 11",
        title_normalized="karelia sarja op 11",
        work_type="music",
    )
    w_id = repo.insert_works_batch([work])[0]

    repo.insert_entity_subjects_batch(
        [
            EntitySubject(
                entity_type="work",
                entity_id=w_id,
                concept_id="http://id.loc.gov/authorities/performanceMediums/mp2013015550",
                raw_label="orkesteri",
                relation_type="medium_of_performance",
                vocabulary="lcmpt",
                is_resolved=True,
            )
        ]
    )

    score_expr = ExpressionEntity(
        work_id=w_id,
        expression_uri="http://example.org/expr/karelia_full_score",
        source="melinda",
        source_id="karelia_score",
        language="und",
        content_type="notated_music",
        title="Karelia-sarja (Partituuri)",
        version_designation="Full Score",
    )
    repo.insert_expressions_batch([score_expr])

    matcher = WorkMatcher(wemi_repo=repo)

    score_target = MatchRecord(
        record_id="score_target_01",
        format="marcxml",
        work_type="music",
        # From the MARC leader. This, not the music_work key, is what separates a
        # score from a sound recording: every music record carries a music_work
        # key, so keying dispatch on it made funnel_b_music_sound unreachable.
        content_type="notated_music",
        title="Karelia-sarja, op. 11 (Partituuri)",
        title_stem="karelia sarja op 11",
        author_key="sibelius",
        work_blocking_key="sibelius::karelia",
        music_work_key="sibelius::karelia::op11",
        raw_payload=b"",
    )

    res = matcher.match_target(score_target)
    assert res.funnel_used == "funnel_d_notated_music"
    assert res.match_tier == "tier2_music_signature"
    assert res.matched_work is not None
    assert res.matched_work.work_uri == work.curie


def test_work_matcher_articles_host_citation(repo: WEMIRepository):
    """Verify Funnel C (Articles) host citation key and DOI matching."""
    work = WorkEntity(
        work_uri="http://example.org/work/article_virtanen",
        source="melinda",
        source_id="art_virtanen",
        title="Kielitieteen kentältä",
        title_normalized="kielitieteen kentalta",
        work_type="text",
    )
    _ = repo.insert_works_batch([work])[0]

    matcher = WorkMatcher(wemi_repo=repo)

    article_target = MatchRecord(
        record_id="art_001",
        format="marcxml",
        work_type="text",
        title="Kielitieteen kentältä",
        title_stem="kielitieteen kentalta",
        is_component=True,
        author_key="virtanen",
        host_citation_key="0042-6806::2023::180::virtanen",
        work_blocking_key="virtanen::kielitieteen",
        raw_payload=b"",
    )

    res = matcher.match_target(article_target)
    assert res.funnel_used == "funnel_c_article"
    assert res.match_tier == "tier2_host_citation_key"
    assert res.matched_work is not None
    assert res.matched_work.work_uri == work.curie


def test_work_matcher_serials_succession_chain(repo: WEMIRepository):
    """Verify Funnel H (Serials) succession chain traversal."""
    work_paivalehti = WorkEntity(
        work_uri="http://example.org/work/paivalehti",
        source="melinda",
        source_id="paivalehti_work",
        title="Päivälehti",
        title_normalized="paivalehti",
        work_type="serial",
    )
    w_p_id = repo.insert_works_batch([work_paivalehti])[0]

    work_hs = WorkEntity(
        work_uri="http://example.org/work/helsingin_sanomat",
        source="melinda",
        source_id="hs_work",
        title="Helsingin Sanomat",
        title_normalized="helsingin sanomat",
        work_type="serial",
    )
    w_hs_id = repo.insert_works_batch([work_hs])[0]

    repo.insert_work_relationships_batch(
        [
            WorkRelationship(
                source_work_id=w_hs_id,
                target_work_id=w_p_id,
                relationship_type="continues",
            )
        ]
    )

    matcher = WorkMatcher(wemi_repo=repo)

    serial_target = MatchRecord(
        record_id="serial_001",
        format="marcxml",
        work_type="serial",
        title="Helsingin Sanomat",
        title_stem="helsingin sanomat",
        work_blocking_key="hs_key",
        raw_payload=b"",
    )

    res = matcher.match_target(serial_target)
    assert res.funnel_used == "funnel_h_serial"
    assert res.match_tier == "tier2_succession_chain"
    assert len(res.relationships) == 1
    assert res.relationships[0].relationship_type == "continues"


def test_work_matcher_board_game_expansion(repo: WEMIRepository):
    """Verify Funnel X (Board Games) base game vs expansion hierarchy."""
    base_game = WorkEntity(
        work_uri="http://example.org/work/catan_base",
        source="melinda",
        source_id="catan_base",
        title="Catan",
        title_normalized="catan",
        work_type="board_game",
    )
    bg_id = repo.insert_works_batch([base_game])[0]

    expansion_game = WorkEntity(
        work_uri="http://example.org/work/catan_merenkavijat",
        source="melinda",
        source_id="catan_seafarers",
        title="Catan: Merenkävijät",
        title_normalized="catan merenkavijat",
        work_type="board_game",
    )
    exp_id = repo.insert_works_batch([expansion_game])[0]

    repo.insert_work_relationships_batch(
        [
            WorkRelationship(
                source_work_id=exp_id,
                target_work_id=bg_id,
                relationship_type="expansion_of",
            )
        ]
    )

    matcher = WorkMatcher(wemi_repo=repo)

    game_target = MatchRecord(
        record_id="game_001",
        format="marcxml",
        work_type="board_game",
        title="Catan: Merenkävijät",
        title_stem="catan merenkavijat",
        work_blocking_key="teuber::catan",
        board_game_key="catan_seafarers",
        raw_payload=b"",
    )

    res = matcher.match_target(game_target)
    assert res.funnel_used == "funnel_x_board_game"
    assert res.matched_work is not None
    assert res.matched_work.work_uri == expansion_game.curie
    assert len(res.relationships) == 1
    assert res.relationships[0].relationship_type == "expansion_of"


def test_work_matcher_specialized_lineages(repo: WEMIRepository):
    """Verify specialized domain relationships: AI fine-tuning, critical editions, 3D digital twins."""
    # 1. AI Model Lineage (Funnel AA)
    base_llama = WorkEntity(
        work_uri="http://example.org/work/llama_base",
        source="melinda",
        source_id="llama_base",
        title="Llama-3 Base Model",
        title_normalized="llama 3 base model",
        work_type="ai_model",
    )
    base_id = repo.insert_works_batch([base_llama])[0]

    ai_work = WorkEntity(
        work_uri="http://example.org/work/llama_fin",
        source="melinda",
        source_id="llama_fin",
        title="Llama-3 Finnish Fine-Tune",
        title_normalized="llama 3 finnish fine tune",
        work_type="ai_model",
    )
    ai_id = repo.insert_works_batch([ai_work])[0]
    repo.insert_work_relationships_batch(
        [
            WorkRelationship(
                source_work_id=ai_id,
                target_work_id=base_id,
                relationship_type="fine_tuned_from",
            )
        ]
    )

    matcher = WorkMatcher(wemi_repo=repo)
    ai_target = MatchRecord(
        record_id="ai_01",
        format="marcxml",
        work_type="ai_model",
        title="Llama-3 Finnish Fine-Tune",
        title_stem="llama 3 finnish fine tune",
        work_blocking_key="ai_key",
        raw_payload=b"",
    )
    ai_res = matcher.match_target(ai_target)
    assert ai_res.funnel_used == "funnel_aa_software_ai"
    assert ai_res.relationships[0].relationship_type == "fine_tuned_from"

    # 2. Scholarly Critical Edition (Funnel MM)
    crit_work = WorkEntity(
        work_uri="http://example.org/work/kivi_seitseman_crit",
        source="melinda",
        source_id="kivi_crit",
        title="Seitsemän veljestä (Kriittinen editio)",
        title_normalized="seitseman veljesta kriittinen editio",
        work_type="critical_edition",
    )
    repo.insert_works_batch([crit_work])

    crit_target = MatchRecord(
        record_id="crit_01",
        format="marcxml",
        work_type="critical_edition",
        title="Seitsemän veljestä (Kriittinen editio)",
        title_stem="seitseman veljesta kriittinen editio",
        work_blocking_key="kivi::seitseman",
        raw_payload=b"",
    )
    crit_res = matcher.match_target(crit_target)
    assert crit_res.funnel_used == "funnel_mm_critical_edition"
    assert crit_res.relationships[0].relationship_type == "critical_edition_of"

    # 3. 3D Cultural Heritage Digital Twin (Funnel PP)
    twin_work = WorkEntity(
        work_uri="http://example.org/work/tuomiokirkko_3d",
        source="melinda",
        source_id="tuomiokirkko_3d",
        title="Turun tuomiokirkko 3D",
        title_normalized="turun tuomiokirkko 3d",
        work_type="heritage_model",
    )
    repo.insert_works_batch([twin_work])

    twin_target = MatchRecord(
        record_id="twin_01",
        format="marcxml",
        work_type="heritage_model",
        title="Turun tuomiokirkko 3D",
        title_stem="turun tuomiokirkko 3d",
        work_blocking_key="turku::tuomiokirkko",
        raw_payload=b"",
    )
    twin_res = matcher.match_target(twin_target)
    assert twin_res.funnel_used == "funnel_pp_heritage_3d"
    assert twin_res.relationships[0].relationship_type == "digital_twin_of"
