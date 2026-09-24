"""End-to-end integration tests using real Finnish National Library fixtures.

Covers monographs, notated music scores, serials, academic theses, board games,
rare books, braille/accessible media, web archives, oral histories, broadcast media,
parallel texts, legal/archival records, medieval manuscript fragments, ephemera,
exhibition catalogues, critical editions, geodata/GIS layers, numismatics,
and 3D heritage digital twins.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import numpy as np
from lxml import etree
from rdflib import Graph, URIRef

from reconciliator.cli import reconcile_cmd, reset_cmd
from reconciliator.clustering import CorpusClusterer
from reconciliator.config import AppConfig
from reconciliator.database import WEMIRepository
from reconciliator.models import (
    ExpressionEntity,
    IdentifierEntity,
    ManifestationEntity,
    MatchRecord,
    WorkEntity,
)
from reconciliator.reconciliation import WorkMatcher
from reconciliator.reports import (
    EXPRESSION_TSV_COLUMNS,
    WORK_TSV_COLUMNS,
    ExpressionReportRow,
    ReportGenerator,
    WorkReportRow,
)
from reconciliator.enrichment import MARCXMLEnricher
from tests.conftest import persist_match_records

OWL_SAME_AS = URIRef("http://www.w3.org/2002/07/owl#sameAs")
FIXTURES_DIR = Path(__file__).parent / "fixtures"


def test_e2e_melinda_marcxml_pipeline(tmp_path: Path) -> None:
    """End-to-end test reconciling sample_melinda.xml with WEMI clustering, matching, and dual TSV reports."""
    sample_melinda = FIXTURES_DIR / "sample_melinda.xml"
    enriched_xml = tmp_path / "enriched_melinda.xml"
    reports_dir = tmp_path / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    config_file = tmp_path / "config.yaml"

    auth_db_path = tmp_path / "authority.db"

    config_yaml = f"""
storage:
  data_dir: "{tmp_path}"

embedding:
  model_name: "TurkuNLP/sbert-cased-finnish-paraphrase"
  template_path: "{Path(__file__).parent.parent}/src/reconciliator/templates/default_serialization.j2"
  device: "cpu"
  batch_size: 16

reconcile:
  target_file: "{sample_melinda}"
  format: "marcxml"
  output_file: "{enriched_xml}"
  enrich: true
  fold_legacy: true
  threshold: 0.85
  agent_reports:
    detailed_file: "{reports_dir}/agent_uris.tsv"
    summary_file: "{reports_dir}/agent_summary.tsv"
  subject_reports:
    detailed_file: "{reports_dir}/subject_uris.tsv"
    summary_file: "{reports_dir}/subject_summary.tsv"
  work_reports:
    detailed_file: "{reports_dir}/work_reconciliation.tsv"
    summary_file: "{reports_dir}/work_summary.tsv"
  expression_reports:
    detailed_file: "{reports_dir}/expression_reconciliation.tsv"
    summary_file: "{reports_dir}/expression_summary.tsv"
  cluster:
    enabled: true
    similarity_threshold: 0.85
  rerank:
    enabled: false
  link_policy: "priority_fallback"
  vocabularies: ["KANTO", "YSO", "MUSO", "KAUNO", "SLM", "SEKO", "VIAF", "ISNI", "WIKIDATA"]
  agent_priority: ["KANTO", "ISNI", "VIAF", "WIKIDATA"]
  subject_priority: ["YSO", "KAUNO", "MUSO", "SLM", "SEKO"]

telemetry:
  heartbeat_interval_seconds: 0.1
  watch_interval_seconds: 0.1
"""
    config_file.write_text(config_yaml, encoding="utf-8")

    # Initialize storage using reset_cmd
    reset_cmd(config=config_file, yes=True)

    # Pre-seed authority storage with canonical Melinda Sinuhe work
    w_repo = WEMIRepository(auth_db_path)
    work_id = w_repo.insert_work(
        WorkEntity(
            work_uri="https://urn.fi/URN:NBN:fi:au:pn:000123456#work",
            source="melinda",
            source_id="sinuhe_canonical_work",
            work_type="text",
            title="Sinuhe egyptiläinen",
            title_normalized="sinuhe egyptilainen",
            original_language="fin",
            wikidata_qid="Q1198642",
        )
    )
    exp_id = w_repo.insert_expression(
        ExpressionEntity(
            work_id=work_id,
            expression_uri="https://urn.fi/URN:NBN:fi:au:pn:000123456#exp_fin",
            source="melinda",
            source_id="sinuhe_exp_fin",
            language="fin",
            content_type="text",
            title="Sinuhe egyptiläinen",
        )
    )
    manif_id = w_repo.insert_manifestation(
        ManifestationEntity(
            work_id=work_id,
            manifestation_uri="http://urn.fi/URN:ISBN:9789510000010",
            source="melinda",
            source_id="000100201",
            title="Sinuhe egyptiläinen",
            title_normalized="sinuhe egyptilainen",
            publication_year=1945,
            publisher="WSOY",
        )
    )
    w_repo.link_manifestation_expression(manif_id, exp_id)
    w_repo.insert_identifiers_batch(
        [
            IdentifierEntity(
                entity_type="manifestation",
                entity_id=manif_id,
                scheme="isbn",
                value="9789510000010",
                value_normalized="9789510000010",
            )
        ]
    )

    # Mock SentenceTransformer
    mock_model = MagicMock()
    mock_model.get_embedding_dimension.return_value = 128
    mock_model.get_sentence_embedding_dimension.return_value = 128

    def mock_encode(texts: list[str], **kwargs: Any) -> np.ndarray[Any, Any]:
        res = np.zeros((len(texts), 128), dtype=np.float32)
        for i, t in enumerate(texts):
            val = hash(t) % 100 / 100.0
            res[i, 0] = val
            res[i, 1] = 1.0 - val
        return res

    mock_model.encode.side_effect = mock_encode

    with patch("reconciliator.embedding.embedder.SentenceTransformer", return_value=mock_model):
        reconcile_cmd(config=config_file)

    # Verify enriched XML output
    assert enriched_xml.exists()
    tree = etree.parse(str(enriched_xml))
    root = tree.getroot()
    records = root.findall(".//{http://www.loc.gov/MARC21/slim}record")
    assert len(records) == 2

    # Check 758 Work and Expression links in first record
    rec1 = records[0]
    fields_758 = rec1.findall(".//{http://www.loc.gov/MARC21/slim}datafield[@tag='758']")
    assert len(fields_758) >= 2
    u0_links = [
        s.text
        for f in fields_758
        for s in f.findall(".//{http://www.loc.gov/MARC21/slim}subfield[@code='0']")
    ]
    assert "melinda:sinuhe_canonical_work" in u0_links
    assert "melinda:sinuhe_exp_fin" in u0_links

    # Verify second record clustered with first record
    rec2 = records[1]
    fields_758_rec2 = rec2.findall(".//{http://www.loc.gov/MARC21/slim}datafield[@tag='758']")
    assert len(fields_758_rec2) >= 1

    # Verify TSV reports
    work_tsv = reports_dir / "work_reconciliation.tsv"
    work_sum_tsv = reports_dir / "work_summary.tsv"
    exp_tsv = reports_dir / "expression_reconciliation.tsv"
    exp_sum_tsv = reports_dir / "expression_summary.tsv"

    assert work_tsv.exists()
    assert work_sum_tsv.exists()
    assert exp_tsv.exists()
    assert exp_sum_tsv.exists()

    work_lines = [
        line.strip().split("\t") for line in work_tsv.read_text(encoding="utf-8").splitlines()
    ]
    assert work_lines[0] == WORK_TSV_COLUMNS
    assert len(work_lines) >= 3  # Header + 2 records

    exp_lines = [
        line.strip().split("\t") for line in exp_tsv.read_text(encoding="utf-8").splitlines()
    ]
    assert exp_lines[0] == EXPRESSION_TSV_COLUMNS
    assert len(exp_lines) >= 3


def test_e2e_bffi_bibframe_pipeline(tmp_path: Path) -> None:
    """End-to-end test reconciling sample_bffi_natlibfi.ttl with native graph enrichment and WEMI links."""
    sample_bffi = FIXTURES_DIR / "sample_bffi_natlibfi.ttl"
    enriched_ttl = tmp_path / "enriched_bffi.ttl"
    reports_dir = tmp_path / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    config_file = tmp_path / "config.yaml"

    auth_db_path = tmp_path / "authority.db"

    config_yaml = f"""
storage:
  data_dir: "{tmp_path}"

embedding:
  model_name: "sentence-transformers/all-MiniLM-L6-v2"
  device: "cpu"
  batch_size: 16

reconcile:
  target_file: "{sample_bffi}"
  format: "bibframe"
  schema_profile: "bffi_1"
  output_file: "{enriched_ttl}"
  enrich: true
  fold_legacy: true
  threshold: 0.85
  agent_reports:
    detailed_file: "{reports_dir}/agent_uris.tsv"
    summary_file: "{reports_dir}/agent_summary.tsv"
  subject_reports:
    detailed_file: "{reports_dir}/subject_uris.tsv"
    summary_file: "{reports_dir}/subject_summary.tsv"
  work_reports:
    detailed_file: "{reports_dir}/work_reconciliation.tsv"
    summary_file: "{reports_dir}/work_summary.tsv"
  expression_reports:
    detailed_file: "{reports_dir}/expression_reconciliation.tsv"
    summary_file: "{reports_dir}/expression_summary.tsv"
  cluster:
    enabled: true
    similarity_threshold: 0.85
  rerank:
    enabled: false
  link_policy: "priority_fallback"
  vocabularies: ["KANTO", "YSO", "MUSO", "KAUNO", "SLM", "SEKO", "VIAF", "ISNI", "WIKIDATA"]
  agent_priority: ["KANTO", "ISNI", "VIAF", "WIKIDATA"]
  subject_priority: ["YSO", "KAUNO", "MUSO", "SLM", "SEKO"]

telemetry:
  heartbeat_interval_seconds: 0.1
  watch_interval_seconds: 0.1
"""
    config_file.write_text(config_yaml, encoding="utf-8")

    # Initialize storage using reset_cmd
    reset_cmd(config=config_file, yes=True)

    # Pre-seed authority storage with canonical BFFI Work
    w_repo = WEMIRepository(auth_db_path)
    work_id = w_repo.insert_work(
        WorkEntity(
            work_uri="http://urn.fi/URN:NBN:fi:bib:work:9912345678",
            source="bffi",
            source_id="sinuhe_bffi_work",
            work_type="text",
            title="Sinuhe egyptiläinen",
            title_normalized="sinuhe egyptilainen",
            original_language="fin",
            wikidata_qid="Q1198642",
        )
    )
    exp_id = w_repo.insert_expression(
        ExpressionEntity(
            work_id=work_id,
            expression_uri="http://urn.fi/URN:NBN:fi:bib:expression:9912345678",
            source="bffi",
            source_id="sinuhe_bffi_exp",
            language="fin",
            content_type="text",
            title="Sinuhe egyptiläinen",
        )
    )
    manif_id = w_repo.insert_manifestation(
        ManifestationEntity(
            work_id=work_id,
            manifestation_uri="http://urn.fi/URN:NBN:fi:bib:instance:9912345678",
            source="bffi",
            source_id="sinuhe_bffi_inst",
            title="Sinuhe egyptiläinen",
            title_normalized="sinuhe egyptilainen",
            publication_year=1945,
            publisher="WSOY",
        )
    )
    w_repo.link_manifestation_expression(manif_id, exp_id)
    w_repo.insert_identifiers_batch(
        [
            IdentifierEntity(
                entity_type="manifestation",
                entity_id=manif_id,
                scheme="isbn",
                value="978-951-0-00001-0",
                value_normalized="9789510000010",
            )
        ]
    )

    mock_model = MagicMock()
    mock_model.get_embedding_dimension.return_value = 128
    mock_model.get_sentence_embedding_dimension.return_value = 128

    def mock_encode(texts: list[str], **kwargs: Any) -> np.ndarray[Any, Any]:
        res = np.zeros((len(texts), 128), dtype=np.float32)
        for i, t in enumerate(texts):
            val = hash(t) % 100 / 100.0
            res[i, 0] = val
            res[i, 1] = 1.0 - val
        return res

    mock_model.encode.side_effect = mock_encode

    with patch("reconciliator.embedding.embedder.SentenceTransformer", return_value=mock_model):
        reconcile_cmd(config=config_file)

    # Assert native Turtle output contains owl:sameAs and WEMI links
    assert enriched_ttl.exists()
    g = Graph()
    g.parse(str(enriched_ttl), format="turtle")

    same_as_objects = {str(o) for _, _, o in g.triples((None, OWL_SAME_AS, None))}
    assert any("Q1198642" in u for u in same_as_objects)

    # Verify work_reports and expression_reports exist
    work_tsv = reports_dir / "work_reconciliation.tsv"
    exp_tsv = reports_dir / "expression_reconciliation.tsv"
    assert work_tsv.exists()
    assert exp_tsv.exists()


def test_e2e_all_specialized_domains_coverage(tmp_path: Path):
    """End-to-end integration test verifying all 42 specialized domains across the WEMI pipeline.

    Covers:
    - Monographs (Funnel A)
    - Recorded Sound (Funnel B)
    - Serial Components / Articles (Funnel C)
    - Notated Music (Funnel D)
    - Audiobooks (Funnel E)
    - Moving Image (Funnel F)
    - Anthologies (Funnel G)
    - Serials (Funnel H)
    - Multi-Part Series (Funnel I)
    - Conference Proceedings (Funnel J)
    - Academic Theses (Funnel K)
    - Cartography (Funnel L)
    - Comics & Manga (Funnel M)
    - Datasets (Funnel N)
    - Standards (Funnel O)
    - Government Publications (Funnel P)
    - 3D Objects / Realia (Funnel Q)
    - Archival Manuscripts (Funnel R)
    - Still Images (Funnel S)
    - Legal Norms / Statutes (Funnel T)
    - Patents (Funnel U)
    - Video Games (Funnel V)
    - Integrating Resources (Funnel W)
    - Board Games (Funnel X)
    - Early Printed Books (Funnel Y)
    - Tactile / Braille (Funnel Z)
    - Software Source Code & Trained AI (Funnel AA)
    - Dramatic Works & Choreography (Funnel BB)
    - Broadcast Podcasts (Funnel CC)
    - Bound-With / Sammelbände (Funnel DD)
    - Web Archives (Funnel EE)
    - Oral Histories (Funnel FF)
    - Broadcast Media / Flow (Funnel GG)
    - Parallel Multilingual Texts (Funnel HH)
    - Preservation Surrogates (Funnel II)
    - Medieval Manuscript Fragments (Funnel JJ)
    - Ephemera (Funnel KK)
    - Exhibition Catalogues (Funnel LL)
    - Scholarly Critical Editions (Funnel MM)
    - Geodata & GIS Layers (Funnel NN)
    - Numismatics (Funnel OO)
    - 3D Heritage Digital Twins (Funnel PP)
    """
    auth_db_path = tmp_path / "authority.db"

    repo = WEMIRepository(auth_db_path)
    matcher = WorkMatcher(wemi_repo=repo)
    clusterer = CorpusClusterer(corpus_repo=repo, wemi_repo=repo, matcher=matcher)
    enricher = MARCXMLEnricher()
    rep_gen = ReportGenerator(config=AppConfig())

    # Define domain test specifications: (domain_code, work_type, id_scheme, id_val, title, author, extra_kwargs)
    domain_specs = [
        ("A", "text", "isbn", "978-951-0-12345-6", "Monografia Teos", "Virtanen, Matti", {}),
        ("B", "music", "isrc", "FI-AAA-24-00001", "Klassinen konsertto", "Sibelius, Jean", {}),
        (
            "C",
            "text",
            "doi",
            "10.1234/journal.art.001",
            "Tieteellinen artikkeli",
            "Korhonen, Liisa",
            {"is_component": True, "host_citation_key": "1234-5678::2024::45::korhonen"},
        ),
        (
            "D",
            "music",
            "ismn",
            "979-0-55011-001-1",
            "Jousikvartetto op. 56",
            "Sibelius, Jean",
            {"music_work_key": "sibelius::jousikvartetto::op56"},
        ),
        (
            "E",
            "audio_nonmusic",
            "isbn",
            "978-951-0-55555-5",
            "Äänikirja Romaani",
            "Waltari, Mika",
            {},
        ),
        (
            "F",
            "moving_image",
            "ean",
            "6412345678901",
            "Tuntematon sotilas elokuva",
            "Mollberg, Rauni",
            {},
        ),
        (
            "G",
            "text",
            "isbn",
            "978-951-0-77777-7",
            "Suomalainen runoantologia",
            "Hellaakoski, Aaro",
            {},
        ),
        (
            "H",
            "serial",
            "issn",
            "0028-0887",
            "Aikakauskirja Luonto",
            "Suomen Luonto ry",
            {"series_key": "00280887::aikakauskirja luonto"},
        ),
        (
            "I",
            "text",
            "issn",
            "0355-1234",
            "Monografiasarja osa 12",
            "Nieminen, Pekka",
            {"series_key": "03551234::monografiasarja::12"},
        ),
        (
            "J",
            "text",
            "doi",
            "10.1109/conf.2024.12345",
            "Konferenssijulkaisu 2024",
            "Virtanen, Juha",
            {},
        ),
        (
            "K",
            "thesis",
            "urn",
            "URN:NBN:fi:hulib-20240101",
            "Väitöskirja bioinformatiikasta",
            "Mäkinen, Ville",
            {},
        ),
        (
            "L",
            "cartography",
            "isbn",
            "978-951-0-88888-8",
            "Suomen peruskartasto 1:20000",
            "Maanmittauslaitos",
            {},
        ),
        ("M", "comic", "isbn", "978-951-0-99999-9", "Sarjakuva-albumi osa 1", "Kallio, Pauli", {}),
        ("N", "dataset", "doi", "10.23729/data-001", "Tutkimusaineisto 2024", "CSC Data Group", {}),
        (
            "O",
            "standard",
            "strn",
            "SFS-EN-ISO-9001",
            "Laadunhallintajärjestelmät",
            "SFS ry",
            {"standard_key": "sfs-en-iso-9001"},
        ),
        (
            "P",
            "text",
            "urn",
            "URN:ISBN:978-952-383-001-1",
            "Valtioneuvoston selonteko",
            "Valtioneuvoston kanslia",
            {},
        ),
        (
            "Q",
            "realia",
            "barcode",
            "REALIA-3001",
            "Opetuskokoelma mineraaleja",
            "Geologian laitos",
            {},
        ),
        (
            "R",
            "archive_fonds",
            "handle",
            "hdl:10024/arch-001",
            "Kirjailijan kirjeenvaihto 1920-1950",
            "Leino, Eino",
            {"archival_fonds_key": "leino::kirjeet"},
        ),
        (
            "S",
            "still_image",
            "uri",
            "http://urn.fi/URN:NBN:fi:kuva-123",
            "Helsingin empire-keskusta 1870",
            "Nyblin, Daniel",
            {},
        ),
        (
            "T",
            "legal",
            "sdk",
            "SDK20240123",
            "Laki digitaalisesta hallinnosta",
            "Eduskunta",
            {"statute_key": "sdk20240123"},
        ),
        (
            "U",
            "patent",
            "patent_number",
            "FI123456B",
            "Menetelmä selluloosan käsittelyyn",
            "Aalto-yliopisto",
            {"patent_key": "fi123456b"},
        ),
        (
            "V",
            "interactive_game",
            "platform_sku",
            "PS5-FI-001",
            "Super Peli Seikkailu",
            "Remedy Entertainment",
            {"game_key": "super peli seikkailu::ps5"},
        ),
        ("W", "integrating", "issn", "1234-9999", "Päivittyvä lakikirja", "Alma Talent", {}),
        (
            "X",
            "board_game",
            "bgg",
            "BGG-12345",
            "Afrikan tähti erikoispainos",
            "Mannerheim, Ilmari",
            {"board_game_key": "afrikan tahti"},
        ),
        (
            "Y",
            "early_print",
            "fingerprint",
            "fe-ig-b-026",
            "Biblia se on Coco Pyhä Raamattu",
            "Henrik Florinus",
            {"fingerprint_key": "fe-ig-b-026"},
        ),
        ("Z", "tactile", "isbn", "978-951-0-11122-2", "Pistekirja Sinuhe", "Waltari, Mika", {}),
        (
            "AA",
            "software",
            "swhid",
            "swh:1:snp:1234567890abcdef",
            "Reconciliator Source Engine",
            "Kansalliskirjasto",
            {"swhid_key": "swh:1:snp:1234567890abcdef"},
        ),
        (
            "BB",
            "drama",
            "uniform_title",
            "Nummisuutarit",
            "Nummisuutarit neljässä näytöksessä",
            "Kivi, Aleksis",
            {},
        ),
        (
            "CC",
            "podcast",
            "guid_rss",
            "POD-EPISODE-101",
            "Tiedeykkönen jakso 101",
            "Yleisradio",
            {"podcast_key": "tiedeykkonen::101"},
        ),
        (
            "DD",
            "text",
            "barcode",
            "SAMMELBAND-BARCODE-99",
            "Kokoomasidos sidottu teos 1",
            "Agricola, Mikael",
            {"sammelband_item_id": "SAMMELBAND-BARCODE-99"},
        ),
        (
            "EE",
            "web_harvest",
            "warc_id",
            "WARC-UUID-550e8400",
            "Verkkoarkiston keruu 2024",
            "Suomen verkkoarkisto",
            {"web_seed_key": "https://www.kansalliskirjasto.fi"},
        ),
        (
            "FF",
            "oral_history",
            "handle",
            "hdl:10024/oral-55",
            "Karjalaismuistot haastattelu",
            "Virtanen, Eeva",
            {"oral_history_key": "karjalaismuistot::virtanen::1975"},
        ),
        (
            "GG",
            "broadcast_program",
            "eidr",
            "10.5240/1489-49A2-3956-4B2D-FE16-5",
            "Yle Uutiset 20.30",
            "Yleisradio",
            {"broadcast_timecode_key": "yle::tv1::202401012030"},
        ),
        (
            "HH",
            "text",
            "isbn",
            "978-951-0-33333-3",
            "Kalevala suomi-englanti rinnakkaisteksti",
            "Lönnrot, Elias",
            {"translators_signature": "parallel:fin,eng"},
        ),
        (
            "II",
            "serial",
            "urn",
            "URN:NBN:fi:mikro-001",
            "Sanomalehti mikrofilmi 1890",
            "Uusi Suometar",
            {},
        ),
        (
            "JJ",
            "codex_fragment",
            "fragmenta",
            "F.m.IV.12",
            "Fragmenta Membranea lehti 12",
            "Tuntematon kirjuri",
            {"fragment_codex_key": "fragmenta::f.m.iv.12"},
        ),
        (
            "KK",
            "ephemera",
            "uri",
            "http://urn.fi/URN:NBN:fi:pien-001",
            "Kevätkauden siemenluettelo 1930",
            "Keskusosuusliike Hankkija",
            {"ephemera_corp_key": "hankkija::siemenluettelo"},
        ),
        (
            "LL",
            "exhibition",
            "isbn",
            "978-951-0-44444-4",
            "Ateneumin Akseli Gallen-Kallela näyttelyluettelo",
            "Ateneumin taidemuseo",
            {"exhibition_event_key": "ateneum::gallen-kallela::2024"},
        ),
        (
            "MM",
            "critical_edition",
            "isbn",
            "978-951-0-66666-6",
            "Seitsemän veljestä kriittinen editio",
            "Kivi, Aleksis",
            {"critical_edition_key": "kivi::seitseman veljesta::variorum"},
        ),
        (
            "NN",
            "geodata_service",
            "epsg",
            "EPSG:3067",
            "Suomen maastotietokanta WFS",
            "Maanmittauslaitos",
            {"geodata_spatial_key": "epsg:3067::maastotietokanta"},
        ),
        (
            "OO",
            "numismatic",
            "km",
            "KM#123",
            "Suomen markka 1952 olympialaiset",
            "Rahapaja",
            {"numismatic_issue_key": "km#123::1952"},
        ),
        (
            "PP",
            "heritage_model",
            "gltf_hash",
            "GLTF-SHA256-AAAA1111",
            "Turun linna 3D digitaalinen kaksonen",
            "Turun museokeskus",
            {"heritage_model_key": "turun linna::3d"},
        ),
    ]

    target_records_list: list[MatchRecord] = []

    for code, w_type, scheme, id_val, title, author, extra in domain_specs:
        uri = f"http://urn.fi/URN:NBN:fi:test:{code}"
        w_id = repo.insert_work(
            WorkEntity(
                work_uri=uri,
                source="test_source",
                source_id=f"work_{code}",
                work_type=w_type,
                title=title,
                title_normalized=title.lower(),
            )
        )
        e_id = repo.insert_expression(
            ExpressionEntity(
                work_id=w_id,
                expression_uri=f"{uri}#exp",
                source="test_source",
                source_id=f"exp_{code}",
                language="fin",
                content_type="text",
                title=title,
            )
        )
        m_id = repo.insert_manifestation(
            ManifestationEntity(
                work_id=w_id,
                manifestation_uri=f"{uri}#manif",
                source="test_source",
                source_id=f"manif_{code}",
                title=title,
                title_normalized=title.lower(),
            )
        )
        repo.link_manifestation_expression(m_id, e_id)
        repo.insert_identifiers_batch(
            [
                IdentifierEntity(
                    entity_type="manifestation",
                    entity_id=m_id,
                    scheme=scheme,
                    value=id_val,
                    value_normalized=id_val.replace("-", "").lower(),
                )
            ]
        )

        t_rec = MatchRecord(
            record_id=f"target_{code}",
            format="marcxml",
            work_type=w_type,
            title=title,
            title_stem=title.lower(),
            author_key=author.lower(),
            work_blocking_key=f"{author.lower()}::{title.lower()}",
            raw_payload=f"<record><title>{title}</title></record>".encode("utf-8"),
            **extra,
        )
        target_records_list.append(t_rec)

    # 1. Persist as WEMI rows and cluster. There is no staging table: the corpus
    # is stored like any other source, under its own record_origin.
    persist_match_records(repo, target_records_list, origin="corpus")
    assert len(repo.get_all_match_records(origins=["corpus"])) == len(domain_specs)

    res_cluster = clusterer.cluster_corpus()
    assert res_cluster.records_clustered == len(domain_specs)
    assert res_cluster.work_clusters_count > 0

    # 2. Test WorkMatcher Funnels across all domains
    for code, w_type, scheme, id_val, title, author, extra in domain_specs:
        t_rec = [r for r in target_records_list if r.record_id == f"target_{code}"][0]
        match_res = matcher.match_target(t_rec, identifiers={scheme: [id_val]})
        assert match_res is not None
        assert match_res.matched_work is not None, (
            f"Domain {code} ({w_type}) failed to resolve work"
        )
        assert match_res.confidence_score >= 0.70

        # Add to TSV reporter
        rep_gen.add_work_row(
            WorkReportRow(
                target_record_id=t_rec.record_id,
                work_type=t_rec.work_type,
                target_title=t_rec.title,
                target_author=t_rec.author_key,
                target_year=t_rec.year,
                work_blocking_key=t_rec.work_blocking_key,
                matched_work_uri=match_res.work_uri,
                wikidata_qid=None,
                match_tier=match_res.match_tier,
                funnel_used=match_res.funnel_used,
                confidence_score=match_res.confidence_score,
                is_clustered=True,
                cluster_id=f"wc_{code}",
                action="injected",
            )
        )
        rep_gen.add_expression_row(
            ExpressionReportRow(
                target_record_id=t_rec.record_id,
                work_uri=match_res.work_uri,
                expression_uri=match_res.expression_uri,
                content_type="text",
                language="fin",
                version_designation=None,
                translators=None,
                performers=None,
                match_tier=match_res.match_tier,
                confidence_score=match_res.confidence_score,
                cluster_id=f"ec_{code}",
                action="injected",
            )
        )

        # 3. Test MARCXML Enrichment for this domain
        dummy_marc = etree.fromstring(
            f'<record xmlns="http://www.loc.gov/MARC21/slim"><controlfield tag="001">target_{code}</controlfield><datafield tag="245" ind1="1" ind2="0"><subfield code="a">{title}</subfield></datafield></record>'.encode(
                "utf-8"
            )
        )
        enriched_marc = enricher.enrich_record_tree(
            dummy_marc,
            wemi_result=match_res,
            target_record=t_rec,
            enrich=True,
        )
        fields_758 = enriched_marc.findall(
            ".//{http://www.loc.gov/MARC21/slim}datafield[@tag='758']"
        )
        assert len(fields_758) >= 1, f"Domain {code} failed 758 injection"

    # 4. Write TSV Reports and verify
    w_tsv = tmp_path / "work_all_domains.tsv"
    e_tsv = tmp_path / "exp_all_domains.tsv"
    sum_tsv = tmp_path / "summary_all_domains.tsv"

    rep_gen.write_work_report(w_tsv)
    rep_gen.write_expression_report(e_tsv)
    rep_gen.write_wemi_summary_report(sum_tsv)

    assert w_tsv.exists()
    assert e_tsv.exists()
    assert sum_tsv.exists()

    w_rows = w_tsv.read_text(encoding="utf-8").splitlines()
    assert len(w_rows) == len(domain_specs) + 1  # Header + 42 domains

    sum_content = sum_tsv.read_text(encoding="utf-8")
    assert "total_works_reconciled\t42" in sum_content
    assert "overview" in sum_content
    assert "work_types" in sum_content
