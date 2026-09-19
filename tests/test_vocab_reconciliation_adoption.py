"""Comprehensive tests for features adopted from vocabulary reconciliation.

Covers:
1. Native diacritic folding (preserving åäöÅÄÖ while folding foreign Latin diacritics)
2. Biographical dates stripping and fictional qualifier preservation
3. Dynamic RelatorRegistry backed by VocabStore and configuration
4. Fictional character safeguard preventing human authority file transfer
5. Dynamic bilingual alias resolution without hardcoded municipality lists
6. Bundled bridge vocabularies ingestion (LoC -> YSO exactMatch)
7. Wikidata P2347 alias ingestion and dual $0/$1 XML enrichment
8. MARC $2 language suffix formatting rules
9. SkosmosDownloader canonical dump URL prioritization for Finto
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

from lxml import etree

from reconciliator.agent_transfer import AgentAligner
from reconciliator.ingestion.skosmos_downloader import SkosmosDownloader
from reconciliator.ingestion.vocab import ingest_bridge_files
from reconciliator.ingestion.wikidata import extract_wikidata_yso_aliases
from reconciliator.loaders.marcxml import parse_marc_record_elem
from reconciliator.matcher import ReconciliationResult
from reconciliator.models import Agent, BibliographicRecord, VocabConcept
from reconciliator.normalizers.agent import (
    strip_agent_dates,
)
from reconciliator.normalizers.linguistic import (
    fold_diacritics,
    fold_label,
    strip_biographical_dates,
)
from reconciliator.normalizers.relators import RelatorRegistry
from reconciliator.vocab.bridges import get_bundled_bridge_files, parse_bridge_turtle
from reconciliator.vocab import VocabFolder, VocabStore, format_target_vocab
from reconciliator.xml_enricher import MARCXMLEnricher

# ---------------------------------------------------------------------------
# 1. Native Diacritic Folding
# ---------------------------------------------------------------------------


def test_fold_diacritics_preserves_nordic_characters() -> None:
    """Ensure åäöÅÄÖ are strictly preserved while foreign Latin diacritics are folded."""
    assert fold_diacritics("Åland") == "Åland"
    assert fold_diacritics("Helsingfors och Åbo") == "Helsingfors och Åbo"
    assert fold_diacritics("Äänekoski") == "Äänekoski"
    assert fold_diacritics("Örebro") == "Örebro"

    # Foreign diacritics folded to ASCII base
    assert fold_diacritics("München") == "Munchen"
    assert fold_diacritics("Curaçao") == "Curacao"
    assert fold_diacritics("Saint-Barthélemy") == "Saint-Barthelemy"
    assert fold_diacritics("São Paulo") == "Sao Paulo"
    assert fold_diacritics("Göran & François") == "Göran & Francois"


def test_fold_label_normalizes_and_strips_punctuation() -> None:
    assert fold_label("München.") == "munchen"
    assert fold_label("  Åbo : ") == "åbo"
    assert fold_label("Saint-Barthélemy /") == "saint-barthelemy"


# ---------------------------------------------------------------------------
# 2. Biographical Dates Stripping & Fictional Qualifier Preservation
# ---------------------------------------------------------------------------


def test_strip_biographical_dates() -> None:
    cleaned = strip_biographical_dates("Waltari, Mika, 1908-1979")
    assert cleaned == "Waltari, Mika"

    cleaned2 = strip_biographical_dates("Snellman, J. V., s. 1806, k. 1881")
    assert cleaned2 == "Snellman, J. V."

    cleaned3 = strip_biographical_dates("Aristoteles, 384 eaa. - 322 eaa.")
    assert cleaned3 == "Aristoteles"

    # Fictional qualifiers must not be stripped as dates
    fictional = "Ankka, Aku (fiktiivinen hahmo)"
    cleaned_f = strip_biographical_dates(fictional)
    assert "(fiktiivinen hahmo)" in cleaned_f

    # Test agent date extraction
    agent_clean = strip_agent_dates("Waltari, Mika, 1908-1979")
    assert agent_clean == "Waltari, Mika"


# ---------------------------------------------------------------------------
# 3. Dynamic RelatorRegistry
# ---------------------------------------------------------------------------


def test_relator_registry_dynamic_stripping(tmp_path: Path) -> None:
    store = VocabStore(tmp_path / "vocab.db")
    # Dynamically insert Finnish, Swedish, and English relators
    store.insert_relators(
        [
            ("kirjoittaja", "fi", "aut"),
            ("säveltäjä", "fi", "cmp"),
            ("författare", "sv", "aut"),
            ("author", "en", "aut"),
        ]
    )

    registry = RelatorRegistry(vocab_store=store, custom_relators=["toimittaja"])

    assert registry.is_relator("kirjoittaja")
    assert registry.is_relator("säveltäjä")
    assert registry.is_relator("författare")
    assert registry.is_relator("author")
    assert registry.is_relator("toimittaja")

    cleaned = registry.strip_relator("Waltari, Mika, kirjoittaja")
    assert cleaned == "Waltari, Mika"

    cleaned2 = registry.strip_relator("Jean Sibelius (säveltäjä)")
    assert cleaned2 == "Jean Sibelius"

    cleaned3 = registry.strip_relator("Tove Jansson, författare")
    assert cleaned3 == "Tove Jansson"


# ---------------------------------------------------------------------------
# 4. Fictional Character Safeguard
# ---------------------------------------------------------------------------


def test_marc_loader_tags_fictional_agents() -> None:
    from reconciliator.normalizers.record import RecordNormalizer

    xml = """<record xmlns="http://www.loc.gov/MARC21/slim">
      <datafield tag="100" ind1="1" ind2=" ">
        <subfield code="a">Ankka, Aku</subfield>
        <subfield code="c">(fiktiivinen hahmo)</subfield>
      </datafield>
      <datafield tag="700" ind1="1" ind2=" ">
        <subfield code="a">Muumipeikko (fiktiivinen hahmo)</subfield>
      </datafield>
      <datafield tag="700" ind1="1" ind2=" ">
        <subfield code="a">Waltari, Mika</subfield>
      </datafield>
    </record>"""
    elem = etree.fromstring(xml.encode("utf-8"))
    raw_dict = parse_marc_record_elem(elem, source="target")
    assert len(raw_dict["agents"]) == 3
    assert raw_dict["agents"][0]["name"] == "Ankka, Aku"
    assert raw_dict["agents"][0]["is_fictional"] is True
    assert raw_dict["agents"][1]["name"] == "Muumipeikko (fiktiivinen hahmo)"
    assert raw_dict["agents"][1]["is_fictional"] is True
    assert raw_dict["agents"][2]["name"] == "Waltari, Mika"
    assert raw_dict["agents"][2]["is_fictional"] is False

    # Normalization preserves is_fictional
    norm_rec = RecordNormalizer().normalize(raw_dict)
    assert len(norm_rec.agents) == 3
    assert norm_rec.agents[0].is_fictional is True
    assert norm_rec.agents[1].is_fictional is True
    assert norm_rec.agents[2].is_fictional is False


def test_agent_aligner_safeguard_blocks_human_authority_transfer() -> None:
    """Fictional agents must not receive human authority file URIs (e.g. Asteri/KANTO)."""
    aligner = AgentAligner()
    fictional_agent = Agent(
        name="Aku Ankka",
        role="subject",
        is_fictional=True,
    )
    real_agent = Agent(
        name="Waltari, Mika",
        role="author",
        is_fictional=False,
    )
    # Candidate record from Melinda authority (Asteri/KANTO)
    cand_rec = BibliographicRecord(
        source_id="MEL-001",
        source="melinda",
        title="Candidate Record",
        agents=[
            Agent(
                name="Aku Ankka",
                uri_subfield_0=["https://urn.fi/URN:NBN:fi:au:pn:999999"],
            ),
            Agent(
                name="Waltari, Mika",
                uri_subfield_0=["https://urn.fi/URN:NBN:fi:au:pn:000123456"],
            ),
        ],
    )

    discovered = aligner.align_target_agents([fictional_agent, real_agent], [cand_rec])

    # Fictional agent at index 0 must have 0 discovered URIs
    assert len(discovered[0]) == 0

    # Non-fictional agent at index 1 must receive the authority URI
    assert len(discovered[1]) == 1
    assert discovered[1][0].uri == "https://urn.fi/URN:NBN:fi:au:pn:000123456"


# ---------------------------------------------------------------------------
# 5. Dynamic Bilingual Alias Resolution (Without Hardcoded Lists)
# ---------------------------------------------------------------------------


def test_dynamic_bilingual_alias_resolution(tmp_path: Path) -> None:
    store = VocabStore(tmp_path / "vocab.db")
    concept = VocabConcept(
        uri="http://www.yso.fi/onto/yso-paikat/p105994",
        vocabulary="yso-paikat",
        pref_labels={"fi": "Maalahti", "sv": "Malax"},
        target_field="651",
    )
    store.insert_concepts([concept])
    store.rebuild_aliases_from_concepts()

    # Swedish name Malax must resolve to Maalahti concept without any hardcoded dictionary
    found, qid = store.find_concept_by_alias("malax", target_field="651")
    assert found is not None
    assert found.uri == "http://www.yso.fi/onto/yso-paikat/p105994"
    assert found.pref_labels.get("fi") == "Maalahti"
    assert found.pref_labels.get("sv") == "Malax"

    folder = VocabFolder(store)
    t_field, t_vocab, t_uri, t_label, m_type, _ = folder.fold_subject_heading(
        heading="Malax",
        source_field="651",
        lang="fi",
    )
    assert t_field == "651"
    assert t_vocab == "yso-paikat/fin"
    assert t_uri == "http://www.yso.fi/onto/yso-paikat/p105994"
    assert t_label == "Maalahti"
    assert m_type == "alias_match"


# ---------------------------------------------------------------------------
# 6. Bundled Bridge Vocabularies Ingestion (LoC -> YSO)
# ---------------------------------------------------------------------------


def test_bundled_bridge_vocabularies_ingestion(tmp_path: Path) -> None:
    store = VocabStore(tmp_path / "vocab.db")
    # Verify bundled bridge files exist
    bridge_files = get_bundled_bridge_files()
    assert len(bridge_files) >= 3

    # Check parse_bridge_turtle
    countries_bridge = next(f for f in bridge_files if "countries" in f.name)
    mappings = parse_bridge_turtle(countries_bridge)
    assert len(mappings) > 0
    # E.g. fi -> http://www.yso.fi/onto/yso/p94426 (Suomi)
    fi_mapping = next(
        (m for m in mappings if "http://id.loc.gov/vocabulary/countries/fi" in m["source_uri"]),
        None,
    )
    assert fi_mapping is not None
    assert fi_mapping["target_uri"] == "http://www.yso.fi/onto/yso/p94426"

    # Ingest into store
    count = ingest_bridge_files(store)
    assert count > 0

    # Look up via legacy URI
    found_map = store.find_legacy_mapping_by_uri("http://id.loc.gov/vocabulary/countries/fi")
    assert found_map is not None
    assert found_map.target_uri == "http://www.yso.fi/onto/yso/p94426"
    assert found_map.target_vocab == "yso"


# ---------------------------------------------------------------------------
# 7. Wikidata P2347 Alias Ingestion and Dual $0 / $1 Enrichment
# ---------------------------------------------------------------------------


def test_wikidata_p2347_alias_extraction_and_dual_enrichment(tmp_path: Path) -> None:
    store = VocabStore(tmp_path / "vocab.db")
    # Concept in VocabStore: YSO p105436 (Suomi)
    concept = VocabConcept(
        uri="http://www.yso.fi/onto/yso/p105436",
        vocabulary="yso",
        pref_labels={"fi": "Suomi", "sv": "Finland", "en": "Finland"},
        target_field="651",
    )
    store.insert_concepts([concept])
    store.rebuild_aliases_from_concepts()

    # Simulated Wikidata JSON for Q33 (Finland) with P2347 = "105436" (or full URI)
    wikidata_item = {
        "id": "Q33",
        "type": "item",
        "labels": {
            "fi": {"language": "fi", "value": "Suomi"},
            "sv": {"language": "sv", "value": "Finland"},
            "sme": {"language": "sme", "value": "Suopma"},
            "smn": {"language": "smn", "value": "Suomâ"},
            "sms": {"language": "sms", "value": "Lääˊddjânnam"},
            "fr": {"language": "fr", "value": "Finlande"},
        },
        "aliases": {
            "fi": [{"language": "fi", "value": "Suomen tasavalta"}],
        },
        "claims": {
            "P2347": [
                {
                    "mainsnak": {
                        "snaktype": "value",
                        "property": "P2347",
                        "datavalue": {"value": "p105436", "type": "string"},
                    }
                }
            ]
        },
    }

    aliases = extract_wikidata_yso_aliases(wikidata_item)
    assert len(aliases) >= 6
    # Insert aliases into store
    store.insert_aliases(aliases)

    # Test Northern Sámi alias "Suopma" resolves to YSO p105436 with QID Q33
    found_c, qid = store.find_concept_by_alias("suopma")
    assert found_c is not None
    assert found_c.uri == "http://www.yso.fi/onto/yso/p105436"
    assert qid == "Q33"

    # Test dual enrichment in MARCXMLEnricher
    xml = """<record xmlns="http://www.loc.gov/MARC21/slim">
      <datafield tag="651" ind1=" " ind2=" ">
        <subfield code="a">Suopma</subfield>
      </datafield>
    </record>"""
    elem = etree.fromstring(xml.encode("utf-8"))

    folder = VocabFolder(store)
    enricher = MARCXMLEnricher(vocab_folder=folder)

    mock_rec = BibliographicRecord(
        source_id="TEST-001",
        source="target",
        title="Test Record",
    )
    result = ReconciliationResult(
        target_record=mock_rec,
    )

    enriched_elem = enricher.enrich_record_tree(elem, result, enrich=True, fold_legacy=True)

    # Verify field 651 has been updated:
    # 1. $a preferred label is Suomi
    # 2. $2 is yso/fin (or yso-paikat/fin)
    # 3. $0 is injected with modern YSO URI
    # 4. $1 is injected with Wikidata entity URI http://www.wikidata.org/entity/Q33
    f651 = enriched_elem.findall(
        ".//{http://www.loc.gov/MARC21/slim}datafield[@tag='651']"
    ) or enriched_elem.findall(".//datafield[@tag='651']")
    assert len(f651) == 1
    a_val = f651[0].findtext("{http://www.loc.gov/MARC21/slim}subfield[@code='a']") or f651[
        0
    ].findtext("subfield[@code='a']")
    assert a_val == "Suomi"

    u0 = [
        e.text
        for e in f651[0].findall(".//{http://www.loc.gov/MARC21/slim}subfield[@code='0']")
        or f651[0].findall(".//subfield[@code='0']")
    ]
    assert "http://www.yso.fi/onto/yso/p105436" in u0

    u1 = [
        e.text
        for e in f651[0].findall(".//{http://www.loc.gov/MARC21/slim}subfield[@code='1']")
        or f651[0].findall(".//subfield[@code='1']")
    ]
    assert "http://www.wikidata.org/entity/Q33" in u1


# ---------------------------------------------------------------------------
# 8. MARC $2 Language Suffix Formatting
# ---------------------------------------------------------------------------


def test_format_target_vocab_language_suffixes() -> None:
    # Legacy Finnish thesauri fold to /fin
    assert format_target_vocab("yso", source_vocab="ysa") == "yso/fin"
    assert format_target_vocab("slm", source_vocab="kaunokki") == "slm/fin"
    assert format_target_vocab("kauno", source_vocab="kaunokki") == "kauno/fin"

    # Legacy Swedish thesauri fold to /swe
    assert format_target_vocab("yso", source_vocab="allars") == "yso/swe"
    assert format_target_vocab("slm", source_vocab="bella") == "slm/swe"

    # Preserve explicit suffix
    assert format_target_vocab("slm", source_vocab="slm/swe") == "slm/swe"
    assert format_target_vocab("yso", source_vocab="yso/fin") == "yso/fin"

    # Vocabularies without language subcodes keep base vocab
    assert format_target_vocab("seko", source_vocab="musa") == "seko"
    assert format_target_vocab("lcsh", source_vocab="lcsh") == "lcsh"


# ---------------------------------------------------------------------------
# 9. SkosmosDownloader Static Dump Prioritization
# ---------------------------------------------------------------------------


def test_skosmos_downloader_canonical_dump_prioritized(tmp_path: Path) -> None:
    expected_dump = "https://api.finto.fi/download/yso/yso-skos.ttl"
    downloader = SkosmosDownloader(
        api_url="https://api.finto.fi/rest/v1",
        dump_urls={"yso": expected_dump},
    )

    mock_resp = MagicMock()
    mock_resp.raise_for_status.return_value = None
    mock_resp.iter_bytes.return_value = [
        b"@prefix skos: <http://www.w3.org/2004/02/skos/core#> .\n"
    ]

    mock_stream_ctx = MagicMock()
    mock_stream_ctx.__enter__.return_value = mock_resp
    mock_stream_ctx.__exit__.return_value = None

    with patch("httpx2.Client.stream", return_value=mock_stream_ctx) as mock_stream:
        dest = downloader.download_vocabulary("yso", output_dir=tmp_path, overwrite=True)
        assert dest.exists()
        assert b"@prefix skos" in dest.read_bytes()
        # Verify it called the configured dump URL
        mock_stream.assert_called_once_with("GET", expected_dump)
