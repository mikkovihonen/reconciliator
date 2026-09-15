"""Tests verifying the resolution of WEMI schema, decomposition, and pre-scan discrepancies."""

import logging
from pathlib import Path

import pytest
from rdflib import Graph

from reconciliator.db import get_db_connection, init_authority_db
from reconciliator.ingestion.wikidata import (
    AUTHOR_INDICATORS,
    _has_author_indicator,
)
from reconciliator.loaders.bibframe.decomposition import (
    decompose_bibframe_work,
)
from reconciliator.models import (
    ExpressionEntity,
    WorkEntity,
)
from reconciliator.repository import WEMIRepository


def test_work_type_hub_sqlite_constraint(tmp_path):
    """Verify that 'hub' is an accepted work_type under SQLite CHECK constraints."""
    db_path = tmp_path / "test_hub.db"
    conn = get_db_connection(db_path)
    init_authority_db(conn)
    conn.close()

    repo = WEMIRepository(db_path=db_path)

    work = WorkEntity(
        work_uri="urn:work:hub:123",
        source="loc",
        source_id="hub123",
        title="Hamlet [Hub]",
        title_normalized="hamlet",
        work_type="hub",
    )
    work_id = repo.insert_work(work)
    assert work_id > 0

    fetched = repo.get_work(work_id)
    assert fetched is not None
    assert fetched.work_type == "hub"


def test_expression_medium_of_performance_persistence(tmp_path):
    """Verify that medium_of_performance can be stored and retrieved on ExpressionEntity."""
    db_path = tmp_path / "test_mop.db"
    conn = get_db_connection(db_path)
    init_authority_db(conn)
    conn.close()

    repo = WEMIRepository(db_path=db_path)

    work = WorkEntity(
        work_uri="urn:work:music:1",
        source="melinda",
        source_id="w1",
        title="Symphony No. 5",
        title_normalized="symphony no 5",
        work_type="music",
    )
    work_id = repo.insert_work(work)

    expression = ExpressionEntity(
        work_id=work_id,
        expression_uri="urn:expression:music:1:arr",
        source="melinda",
        source_id="e1",
        title="Symphony No. 5 (Arr. for piano four-hands)",
        content_type="notated_music",
        language="und",
        medium_of_performance="piano four-hands",
    )
    expr_id = repo.insert_expression(expression)
    assert expr_id > 0

    fetched = repo.get_expression(expr_id)
    assert fetched is not None
    assert fetched.medium_of_performance == "piano four-hands"

    # Test update on conflict
    expression.medium_of_performance = "piano solo"
    repo.insert_expression(expression)
    fetched_updated = repo.get_expression(expr_id)
    assert fetched_updated is not None
    assert fetched_updated.medium_of_performance == "piano solo"


def test_wikidata_author_indicators():
    """Verify that AUTHOR_INDICATORS correctly identifies human (Q5), writer (Q170574), and agent entities."""
    # Q5 human line
    line_q5 = b'{"id":"Q42","type":"item","claims":{"P31":[{"mainsnak":{"datavalue":{"value":{"id":"Q5"}}}}]}}'
    assert _has_author_indicator(line_q5) is True

    # Q170574 writer occupation or type
    line_writer = (
        b'{"id":"Q12345","claims":{"P106":[{"mainsnak":{"datavalue":{"value":{"id":"Q170574"}}}}]}}'
    )
    assert _has_author_indicator(line_writer) is True

    # P214 VIAF ID indicator
    line_viaf = b'{"id":"Q999","claims":{"P214":[{"mainsnak":{"datavalue":{"value":"12345678"}}}]}}'
    assert _has_author_indicator(line_viaf) is True

    # Irrelevant line (e.g. asteroid or astronomy object with no author indicators)
    line_asteroid = (
        b'{"id":"Q1","claims":{"P31":[{"mainsnak":{"datavalue":{"value":{"id":"Q3863"}}}}]}}'
    )
    assert _has_author_indicator(line_asteroid) is False

    assert b'"Q5"' in AUTHOR_INDICATORS
    assert b'"Q170574"' in AUTHOR_INDICATORS


def test_bffi_simple_agent_fallback_decomposition():
    """Verify that bffi:simpleAgent string literals synthesize unlinked AgentEntity when no agent node exists."""
    g = Graph()
    g.parse(
        data="""
        @prefix bffi: <http://urn.fi/URN:NBN:fi:schema:bffi:> .

        <http://example.org/instance/1> a bffi:Manifestation ;
            bffi:title [ a bffi:Title ; bffi:mainTitle "Test Book with Literal Agent" ] ;
            bffi:workManifested <http://example.org/work/1> .

        <http://example.org/work/1> a bffi:Work ;
            bffi:title [ a bffi:Title ; bffi:mainTitle "Test Book with Literal Agent" ] ;
            bffi:contribution [
                a bffi:PrimaryContribution ;
                bffi:simpleAgent "Virtanen, Matti, 1950-" ;
                bffi:role <http://id.loc.gov/vocabulary/relators/aut>
            ] .
        """,
        format="turtle",
    )

    (
        work,
        expression,
        manifestation,
        items,
        agents,
        relationships,
        identifiers,
        associations,
        mops,
        contributions,
    ) = decompose_bibframe_work(g)

    assert len(agents) >= 1
    matti = next((a for a in agents if "Virtanen" in a.name), None)
    assert matti is not None
    assert matti.name == "Virtanen, Matti, 1950-"
    assert matti.name_normalized == "virtanen, matti, 1950-"
    assert "urn:reconciliator:agent:" in matti.agent_uri

    assert len(contributions) >= 1
    primary_contrib = next((c for c in contributions if c.is_primary), None)
    assert primary_contrib is not None
    assert primary_contrib.role == "aut"


def test_dual_path_consistency_matching():
    """Verify that when expressionManifested -> expressionOf and workManifested agree, decomposition succeeds without warning."""
    g = Graph()
    g.parse(
        data="""
        @prefix bffi: <http://urn.fi/URN:NBN:fi:schema:bffi:> .

        <http://example.org/instance/dual1> a bffi:Manifestation ;
            bffi:title [ a bffi:Title ; bffi:mainTitle "Dual Path Agreeing" ] ;
            bffi:expressionManifested <http://example.org/expression/dual1> ;
            bffi:workManifested <http://example.org/work/canonical> .

        <http://example.org/expression/dual1> a bffi:Expression ;
            bffi:expressionOf <http://example.org/work/canonical> ;
            bffi:title [ a bffi:Title ; bffi:mainTitle "Dual Path Agreeing" ] .

        <http://example.org/work/canonical> a bffi:Work ;
            bffi:title [ a bffi:Title ; bffi:mainTitle "Dual Path Agreeing" ] .
        """,
        format="turtle",
    )

    work, expression, manifestation, _, _, _, _, _, _, _ = decompose_bibframe_work(g)
    assert work.work_uri == "http://example.org/work/canonical"
    assert expression.expression_uri == "http://example.org/expression/dual1"


def test_dual_path_consistency_discrepancy_warning(caplog):
    """Verify that when expression-mediated Work conflicts with shortcut Work, a warning is logged and the canonical path is kept."""
    g = Graph()
    g.parse(
        data="""
        @prefix bffi: <http://urn.fi/URN:NBN:fi:schema:bffi:> .

        <http://example.org/instance/dual_conflict> a bffi:Manifestation ;
            bffi:title [ a bffi:Title ; bffi:mainTitle "Conflicting Paths" ] ;
            bffi:expressionManifested <http://example.org/expression/canonical_exp> ;
            bffi:workManifested <http://example.org/work/stale_shortcut> .

        <http://example.org/expression/canonical_exp> a bffi:Expression ;
            bffi:expressionOf <http://example.org/work/canonical_work> ;
            bffi:title [ a bffi:Title ; bffi:mainTitle "Conflicting Paths" ] .

        <http://example.org/work/canonical_work> a bffi:Work ;
            bffi:title [ a bffi:Title ; bffi:mainTitle "Canonical Work Title" ] .

        <http://example.org/work/stale_shortcut> a bffi:Work ;
            bffi:title [ a bffi:Title ; bffi:mainTitle "Stale Shortcut Work Title" ] .
        """,
        format="turtle",
    )

    with caplog.at_level(logging.WARNING):
        work, expression, manifestation, _, _, _, _, _, _, _ = decompose_bibframe_work(g)

    # Canonical expression-mediated work must be preferred
    assert work.work_uri == "http://example.org/work/canonical_work"
    assert "Dual-path work discrepancy detected" in caplog.text
    assert "http://example.org/work/canonical_work" in caplog.text
    assert "http://example.org/work/stale_shortcut" in caplog.text


def test_marcxml_domain_fields_parsing():
    """Verify H-22: MARCXML parser extracts 026, 013, 034, 347, 521, and 538 fields."""
    from lxml import etree

    from reconciliator.loaders.marcxml import (
        build_target_record_from_marc,
        decompose_marc_wemi,
        parse_marc_record_elem,
    )

    xml_data = """
    <record xmlns="http://www.loc.gov/MARC21/slim">
        <leader>01234nam a2200349 a 4500</leader>
        <controlfield tag="001">rec_domain_1</controlfield>
        <controlfield tag="008">240101s2024    fi |||||||||||||||||fin|d</controlfield>
        <datafield tag="245" ind1="1" ind2="0">
            <subfield code="a">Early Print and Technical Invention</subfield>
        </datafield>
        <datafield tag="026" ind1=" " ind2=" ">
            <subfield code="e">e-te e-re r-e- thal 1544R</subfield>
            <subfield code="2">stcn</subfield>
        </datafield>
        <datafield tag="013" ind1=" " ind2=" ">
            <subfield code="a">FI123456B</subfield>
            <subfield code="b">FI</subfield>
        </datafield>
        <datafield tag="034" ind1="1" ind2=" ">
            <subfield code="d">E0200000</subfield>
            <subfield code="e">E0300000</subfield>
            <subfield code="f">N0700000</subfield>
            <subfield code="g">N0600000</subfield>
        </datafield>
        <datafield tag="347" ind1=" " ind2=" ">
            <subfield code="a">3D model</subfield>
            <subfield code="b">GLTF</subfield>
            <subfield code="2">gltf</subfield>
        </datafield>
        <datafield tag="521" ind1=" " ind2=" ">
            <subfield code="a">PEGI 18</subfield>
        </datafield>
        <datafield tag="538" ind1=" " ind2=" ">
            <subfield code="a">Requires 16 GB RAM and GPU acceleration</subfield>
        </datafield>
    </record>
    """
    elem = etree.fromstring(xml_data.encode("utf-8"))

    # 1. Test parse_marc_record_elem
    parsed = parse_marc_record_elem(elem)
    assert parsed["fingerprints"] == ["e-te e-re r-e- thal 1544R"]
    assert parsed["patent_numbers"] == ["FI123456B"]
    assert "d:E0200000" in parsed["cartographic_coordinates"][0]
    assert parsed["digital_file_formats"] == ["GLTF"]
    assert parsed["target_audience"] == ["PEGI 18"]
    assert parsed["system_requirements"] == ["Requires 16 GB RAM and GPU acceleration"]

    # 2. Test decompose_marc_wemi IdentifierEntity creation
    _, _, _, _, _, _, identifiers, _, _, _ = decompose_marc_wemi(elem)
    schemes = {i.scheme: i.value for i in identifiers}
    assert schemes.get("fingerprint") == "e-te e-re r-e- thal 1544R"
    assert schemes.get("patent_number") == "FI123456B"
    assert schemes.get("file_format") == "GLTF"

    # 3. Test build_target_record_from_marc keys
    target = build_target_record_from_marc(elem)
    assert target.fingerprint_key == "e-te e-re r-e- thal 1544R"
    assert target.patent_key == "FI123456B"
    assert target.heritage_model_key == "GLTF"
    assert "d:E0200000" in (target.geodata_spatial_key or "")


def test_reconcile_config_defaults():
    """Verify M-02 (templates default-initialized) and opt-in reports configuration."""
    from pathlib import Path

    from reconciliator.config import ReconcileConfig, ReportSetPathsConfig

    cfg = ReconcileConfig(
        target_file=Path("dummy.xml"),
        agent_reports=ReportSetPathsConfig(
            detailed_file=Path("data/reports/agent_detailed.tsv"),
            summary_file=Path("data/reports/agent_summary.tsv"),
        ),
        subject_reports=ReportSetPathsConfig(
            detailed_file=Path("data/reports/subject_detailed.tsv"),
            summary_file=Path("data/reports/subject_summary.tsv"),
        ),
    )

    # M-02: Concrete default for templates
    assert cfg.templates is not None
    assert cfg.templates.work == Path("reconciliator/templates/work_serialization.j2")
    assert cfg.templates.expression == Path("reconciliator/templates/expression_serialization.j2")
    assert cfg.templates.manifestation == Path(
        "reconciliator/templates/manifestation_serialization.j2"
    )

    # Work/expression reports remain opt-in
    assert cfg.work_reports is None
    assert cfg.expression_reports is None


def test_funnels_dispatch_anthology_conference_government(tmp_path):
    """Verify H-25: match_target dispatches to anthology, conference, and government funnels."""
    from reconciliator.db import get_db_connection, init_authority_db
    from reconciliator.matcher import WorkMatcher
    from reconciliator.models import TargetRecord
    from reconciliator.repository import WEMIRepository

    db_path = tmp_path / "test_authority.db"
    with get_db_connection(db_path) as conn:
        init_authority_db(conn)
    repo = WEMIRepository(db_path)
    matcher = WorkMatcher(wemi_repo=repo)

    # 1. Anthology funnel dispatch
    t_anthology = TargetRecord(
        record_id="rec_anthology",
        format="marcxml",
        work_type="anthology",
        title="Suomalaisia esseitä",
        title_stem="suomalaisia esseita",
        work_blocking_key="suomalaisia esseita",
        raw_payload=b"<record/>",
    )
    res_anth = matcher.match_target(t_anthology)
    assert res_anth.funnel_used == "funnel_g_anthology"

    # 2. Conference funnel dispatch
    t_conf = TargetRecord(
        record_id="rec_conf",
        format="marcxml",
        work_type="conference",
        title="Proceedings of the 2025 AI Conference",
        title_stem="proceedings 2025 ai conference",
        work_blocking_key="proceedings 2025 ai conference",
        raw_payload=b"<record/>",
    )
    res_conf = matcher.match_target(t_conf)
    assert res_conf.funnel_used == "funnel_j_conference"

    # 3. Government funnel dispatch
    t_gov = TargetRecord(
        record_id="rec_gov",
        format="marcxml",
        work_type="government",
        title="Valtioneuvoston selvitys",
        title_stem="valtioneuvoston selvitys",
        work_blocking_key="valtioneuvoston selvitys",
        raw_payload=b"<record/>",
    )
    res_gov = matcher.match_target(t_gov)
    assert res_gov.funnel_used == "funnel_p_government"

    # 4. Government funnel via Valto URN
    t_gov_valto = TargetRecord(
        record_id="rec_gov_valto",
        format="marcxml",
        work_type="text",
        title="Opetusministeriön julkaisuja",
        title_stem="opetusministerion julkaisuja",
        work_blocking_key="opetusministerion julkaisuja",
        raw_payload=b"<record/>",
    )
    res_valto = matcher.match_target(
        t_gov_valto, identifiers={"urn": ["urn:nbn:fi-fe20240101-valto-1234"]}
    )
    assert res_valto.funnel_used == "funnel_p_government"


def test_record_identifiers_and_repository_doi_matching(tmp_path):
    """Verify H-26 and M-17: DOI on RecordIdentifiers and Tier 1 exact matching."""
    from reconciliator.config import VectorEngineConfig
    from reconciliator.models import IdentifierEntity, ManifestationEntity, RecordIdentifiers
    from reconciliator.normalizers.record import RecordNormalizer
    from reconciliator.repository import WEMIRepository

    # 1. RecordIdentifiers contains doi
    ids = RecordIdentifiers(doi=["10.1000/182"])
    assert ids.doi == ["10.1000/182"]

    # 2. RecordNormalizer populates doi
    normalizer = RecordNormalizer()
    norm_rec = normalizer.normalize(
        {
            "source_id": "rec_doi_1",
            "source": "target",
            "title": "Article with DOI",
            "identifiers": {"doi": ["10.1234/test.doi"]},
        }
    )
    assert norm_rec.identifiers.doi == ["10.1234/test.doi"]

    # 3. Tier 1 exact identifier resolution now runs against the WEMI
    #    identifiers table via lookup_by_identifier.
    vec_cfg = VectorEngineConfig(backend="usearch", device="cpu", mode="embedded")
    repo = WEMIRepository(
        db_path=tmp_path / "auth.db",
        work_index_path=tmp_path / "work.index",
        dimension=8,
        vector_engine_config=vec_cfg,
    )
    manifestation_uri = "urn:manif:target:rec_doi_1"
    manif_id = repo.insert_manifestation(
        ManifestationEntity(
            manifestation_uri=manifestation_uri,
            source="target",
            source_id="rec_doi_1",
            title=norm_rec.title,
            title_normalized=norm_rec.title.lower(),
            carrier_type="volume",
        )
    )
    doi = norm_rec.identifiers.doi[0]
    repo.insert_identifiers_batch(
        [
            IdentifierEntity(
                entity_type="manifestation",
                entity_id=manif_id,
                scheme="doi",
                value=doi,
                value_normalized=doi.lower(),
            )
        ]
    )

    exact_matches = repo.lookup_by_identifier("doi", doi)
    assert exact_matches == [("manifestation", manif_id)]
    assert repo.get_manifestation(manif_id).source_id == "rec_doi_1"

    # Unknown DOIs resolve to nothing rather than a false positive.
    assert repo.lookup_by_identifier("doi", "10.9999/missing") == []


def test_marc_predicate_profiles_default():
    """Verify EnrichmentTogglesConfig defaults include bibframe, bffi, and rda."""
    from reconciliator.config import EnrichmentTogglesConfig

    cfg = EnrichmentTogglesConfig()
    assert cfg.marc_predicate_profiles == ["bibframe", "bffi", "rda"]


def test_render_work_original_title():
    """Verify M-23: render_work dynamically includes original_title."""
    from reconciliator.models import WorkEntity
    from reconciliator.template import WEMISerializationEngine

    engine = WEMISerializationEngine()
    work = WorkEntity(
        work_uri="http://example.org/work/1",
        source="test",
        source_id="w1",
        title="Sinuhe the Egyptian",
        title_normalized="sinuhe the egyptian",
        original_title="Sinuhe egyptiläinen",
        work_type="text",
    )

    rendered = engine.render_work(work)
    assert "[Alkukielinen: Sinuhe egyptiläinen]" in rendered


def test_translator_alignment_via_agent_aligner(tmp_path):
    """Verify H-31: _align_expression uses AgentAligner to match translators."""
    from reconciliator.db import get_db_connection, init_authority_db
    from reconciliator.matcher import WorkMatcher
    from reconciliator.models import AgentEntity, ContributionEntity, ExpressionEntity, WorkEntity
    from reconciliator.repository import WEMIRepository

    db_path = tmp_path / "wemi_auth.db"
    with get_db_connection(db_path) as conn:
        init_authority_db(conn)
    repo = WEMIRepository(db_path)
    matcher = WorkMatcher(wemi_repo=repo)

    # Insert work
    repo.insert_works_batch(
        [
            WorkEntity(
                id=1,
                work_uri="http://example.org/work/1",
                source="test",
                source_id="w1",
                title="Sinuhe egyptiläinen",
                title_normalized="sinuhe egyptilainen",
                work_type="text",
            )
        ]
    )

    # Insert two agents (translators)
    ag_ids = repo.insert_agents_batch(
        [
            AgentEntity(
                agent_uri="http://example.org/agent/minaudier",
                source="test",
                source_id="a1",
                name="Jean-Pierre Minaudier",
                name_normalized="jean pierre minaudier",
                surname="Minaudier",
            ),
            AgentEntity(
                agent_uri="http://example.org/agent/smith",
                source="test",
                source_id="a2",
                name="John Smith",
                name_normalized="john smith",
                surname="Smith",
            ),
        ]
    )
    ag1_id, ag2_id = ag_ids[0], ag_ids[1]

    # Insert two French expressions under work 1
    e1 = ExpressionEntity(
        id=1,
        work_id=1,
        expression_uri="http://example.org/expr/fr_minaudier",
        source="test",
        source_id="e1",
        language="fre",
        content_type="text",
        title="Sinouhé l'Égyptien",
    )
    e2 = ExpressionEntity(
        id=2,
        work_id=1,
        expression_uri="http://example.org/expr/fr_smith",
        source="test",
        source_id="e2",
        language="fre",
        content_type="text",
        title="Sinouhé l'Égyptien (Smith)",
    )
    repo.insert_expressions_batch([e1, e2])

    # Insert contributions linking expressions to translators
    repo.insert_contributions_batch(
        [
            ContributionEntity(
                entity_type="expression",
                entity_id=1,
                agent_id=ag1_id,
                role="trl",
            ),
            ContributionEntity(
                entity_type="expression",
                entity_id=2,
                agent_id=ag2_id,
                role="trl",
            ),
        ]
    )

    # When no translator signature is given, falls back to first matching language
    aligned_default = matcher._align_expression([e1, e2], target_language="fre")
    assert aligned_default is not None
    assert aligned_default.id == 1

    # When translator signature matches Smith, AgentAligner aligns with e2
    aligned_smith = matcher._align_expression(
        [e1, e2], target_language="fre", translator_signature="Smith, John"
    )
    assert aligned_smith is not None
    assert aligned_smith.id == 2

    # When translator signature matches Minaudier, AgentAligner aligns with e1
    aligned_minaudier = matcher._align_expression(
        [e1, e2], target_language="fre", translator_signature="Minaudier"
    )
    assert aligned_minaudier is not None
    assert aligned_minaudier.id == 1


def test_blocking_key_families_coverage(tmp_path):
    """Verify H-29: generate_blocking_keys covers material-specific blocking families."""
    from reconciliator.clusterer import CorpusClusterer
    from reconciliator.models import TargetRecord
    from reconciliator.repository import WEMIRepository

    db_path = tmp_path / "authority.db"
    repo = WEMIRepository(db_path)
    clusterer = CorpusClusterer(corpus_repo=repo)

    # 1. Moving image family
    t_movie = TargetRecord(
        record_id="m1",
        format="marcxml",
        work_type="moving_image",
        title="Tuntematon sotilas",
        title_stem="tuntematon sotilas",
        work_blocking_key="tuntematon sotilas",
        author_key="mollberg",
        year=1985,
        raw_payload=b"<record/>",
    )
    keys_movie = clusterer.generate_blocking_keys(t_movie)
    assert any(k.startswith("moving_image::mollberg::tuntematon sotilas::1985") for k in keys_movie)

    # 2. Drama family
    t_drama = TargetRecord(
        record_id="d1",
        format="marcxml",
        work_type="drama",
        title="Nummisuutarit",
        title_stem="nummisuutarit",
        work_blocking_key="nummisuutarit",
        author_key="kivi",
        raw_payload=b"<record/>",
    )
    keys_drama = clusterer.generate_blocking_keys(t_drama)
    assert any(k.startswith("drama::kivi::nummisuutarit") for k in keys_drama)

    # 3. Thesis family
    t_thesis = TargetRecord(
        record_id="th1",
        format="marcxml",
        work_type="thesis",
        title="Dissertation on AI",
        title_stem="dissertation on ai",
        work_blocking_key="dissertation on ai",
        author_key="virtanen",
        raw_payload=b"<record/>",
    )
    keys_thesis = clusterer.generate_blocking_keys(t_thesis)
    assert any(k.startswith("thesis::virtanen::dissertation on ai") for k in keys_thesis)

    # 4. Cartographic family
    t_map = TargetRecord(
        record_id="c1",
        format="marcxml",
        work_type="cartography",
        title="Helsingin kartta",
        title_stem="helsingin kartta",
        work_blocking_key="helsingin kartta",
        geodata_spatial_key="coords:60.1:24.9",
        raw_payload=b"<record/>",
    )
    keys_map = clusterer.generate_blocking_keys(t_map)
    assert any(k.startswith("cartographic::coords:60.1:24.9") for k in keys_map)

    # 5. Still image family
    t_photo = TargetRecord(
        record_id="p1",
        format="marcxml",
        work_type="still_image",
        title="Maisema",
        title_stem="maisema",
        work_blocking_key="maisema",
        author_key="into",
        year=1900,
        raw_payload=b"<record/>",
    )
    keys_photo = clusterer.generate_blocking_keys(t_photo)
    assert any(k.startswith("still_image::into::maisema::1900") for k in keys_photo)

    # 6. Preservation surrogate
    t_surrogate = TargetRecord(
        record_id="s1",
        format="marcxml",
        work_type="preservation_surrogate",
        title="Microfilm of News",
        title_stem="microfilm of news",
        work_blocking_key="microfilm of news",
        year=1950,
        raw_payload=b"<record/>",
    )
    keys_surrogate = clusterer.generate_blocking_keys(t_surrogate)
    assert any(
        k.startswith("preservation_surrogate::microfilm of news::1950") for k in keys_surrogate
    )


def test_version_resolution_exception_handling():
    """Verify H-33: get_version catches (PackageNotFoundError, Exception) without shadowing."""
    from unittest.mock import patch

    from reconciliator.version import get_version

    with patch("reconciliator.version.Path.is_file", return_value=False):
        with patch("reconciliator.version.version", side_effect=Exception("Metadata corrupted")):
            v = get_version()
            assert v == "0.1.0"


def test_run_cmd_pipeline_execution_and_reporting(tmp_path):
    """Verify H-32/M-46/L-11: run_cmd executes steps with progress, timing, and error handling."""
    from unittest.mock import patch

    from typer.testing import CliRunner

    from reconciliator.cli import app

    runner = CliRunner()
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        """
ingest_vocab:
  input_path: "vocabs/"
ingest_wikidata:
  dump_path: "dump.json"
"""
    )

    # Successful pipeline run
    with (
        patch("reconciliator.cli.ingest_vocab_cmd") as mock_vocab,
        patch("reconciliator.cli.ingest_wikidata_cmd") as mock_wiki,
    ):
        result = runner.invoke(app, ["run", "--config", str(cfg_file)])
        assert result.exit_code == 0
        assert mock_vocab.called
        assert mock_wiki.called
        assert "Starting end-to-end pipeline with 2 enabled step(s)" in result.stdout
        assert "Step 1/2: ingest-vocab" in result.stdout
        assert "Step 2/2: ingest-wikidata" in result.stdout
        assert "Pipeline completed successfully!" in result.stdout

    # Failed pipeline run stops further execution and exits with code 1
    with (
        patch("reconciliator.cli.ingest_vocab_cmd", side_effect=RuntimeError("Download timeout")),
        patch("reconciliator.cli.ingest_wikidata_cmd") as mock_wiki,
    ):
        result = runner.invoke(app, ["run", "--config", str(cfg_file)])
        assert result.exit_code == 1
        assert "Pipeline failed at step 1/2 (ingest-vocab)" in result.stdout
        assert "Download timeout" in result.stdout
        assert not mock_wiki.called


def test_marc_original_title_and_narrators_serialization():
    """Verify H-35 & H-36: original_title and narrators are populated and serialized."""
    from lxml import etree

    from reconciliator.loaders.marcxml import decompose_marc_wemi

    xml_data = b"""<record xmlns="http://www.loc.gov/MARC21/slim">
      <leader>01234cam a2200301 a 4500</leader>
      <controlfield tag="001">rec123</controlfield>
      <controlfield tag="008">240101s2020    fi |||||||||||000 0 fre d</controlfield>
      <datafield tag="041" ind1="1" ind2=" ">
        <subfield code="a">fre</subfield>
        <subfield code="h">fin</subfield>
      </datafield>
      <datafield tag="765" ind1="0" ind2=" ">
        <subfield code="t">Sinuhe egyptil\xc3\xa4inen</subfield>
      </datafield>
      <datafield tag="245" ind1="1" ind2="0">
        <subfield code="a">Sinouh\xc3\xa9 l'\xc3\x89gyptien</subfield>
      </datafield>
      <datafield tag="700" ind1="1" ind2=" ">
        <subfield code="a">Minaudier, Jean-Pierre</subfield>
        <subfield code="4">trl</subfield>
      </datafield>
      <datafield tag="700" ind1="1" ind2=" ">
        <subfield code="a">Depardieu, G\xc3\xa9rard</subfield>
        <subfield code="4">nrt</subfield>
      </datafield>
    </record>"""

    elem = etree.fromstring(xml_data)
    res = decompose_marc_wemi(elem, source="test")
    work = res[0]
    expression = res[1]

    # H-35: original_title is extracted and serialized
    assert work.original_title == "Sinuhe egyptiläinen"
    assert "[Alkukielinen: Sinuhe egyptiläinen]" in work.serialized_text

    # H-36: narrators are extracted and serialized in Expression tier
    assert "Lukija: Depardieu, Gérard" in expression.serialized_text
    assert "Kääntäjä: Minaudier, Jean-Pierre" in expression.serialized_text
