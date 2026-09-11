"""Tests for RDFLib graph parsing, streaming JSON-LD parsing, and ResourceSync harvesting."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from rdflib import Graph

from reconciliator.harvest.resourcesync import ResourceSyncHarvester
from reconciliator.loaders.bibframe import (
    get_profile,
    iter_jsonld_bibframe_records,
    parse_bibframe_graph,
    stream_bibframe_records,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def test_bffi_natlibfi_ttl_parsing() -> None:
    ttl_path = FIXTURES_DIR / "sample_bffi_natlibfi.ttl"
    g = Graph()
    g.parse(str(ttl_path), format="turtle")

    profile = get_profile("bffi_1")
    records = list(parse_bibframe_graph(g, profile=profile, source="melinda"))
    assert len(records) == 1

    rec, nodes = records[0]
    inst_node, exp_node, work_node = nodes
    assert inst_node is not None
    assert exp_node is not None
    assert work_node is not None

    # Bibliographic fields
    assert rec["source"] == "melinda"
    assert rec["title"] == "Sinuhe egyptiläinen"
    assert rec["subtitle"] == "viisitoista kirjaa lääkäri Sinuhen elämästä"
    assert rec["publication_year"] == "1945"
    assert rec["publisher"] == "WSOY"
    assert rec["publication_place"] == "Helsinki"
    assert rec["language"] == "fin"
    assert "978-951-0-00001-0" in rec["identifiers"]["isbn"]

    # Agents & provenance
    assert len(rec["agents"]) == 1
    agent = rec["agents"][0]
    assert agent["name"] == "Waltari, Mika"
    assert agent["is_personal"] is True
    assert agent["role"] == "aut"
    assert agent["field_tag"] == "100"  # Extracted from bffi:marcKey 1001 $a...
    assert "http://urn.fi/URN:NBN:fi:au:pn:000123456" in agent["uri_subfield_0"]

    # Subjects
    assert len(rec["subjects"]["topical"]) == 1
    subj = rec["subjects"]["topical"][0]
    assert subj["heading"] == "lääkärit"
    assert subj["vocabulary"] == "yso"
    assert subj["uri"] == "http://www.yso.fi/onto/yso/p1234"


def test_bibframe_lc_ttl_with_pmo() -> None:
    ttl_path = FIXTURES_DIR / "sample_bibframe_lc.ttl"
    g = Graph()
    g.parse(str(ttl_path), format="turtle")

    records = list(parse_bibframe_graph(g, source="lc"))
    assert len(records) == 1

    rec, _ = records[0]
    assert rec["title"] == "Symphony No. 5 in E-flat major, op. 82"
    assert rec["publication_year"] == "1919"
    assert rec["publisher"] == "Hansen"
    assert "978-0-19-385012-3" in rec["identifiers"]["isbn"]

    # Agent
    assert len(rec["agents"]) == 1
    assert rec["agents"][0]["name"] == "Sibelius, Jean"
    assert rec["agents"][0]["role"] == "cmp"

    # Performed Music Ontology medium of performance
    assert len(rec["subjects"]["medium_of_performance"]) == 1
    mop = rec["subjects"]["medium_of_performance"][0]
    assert mop["heading"] == "orkesteri"
    assert mop["field"] == "382"


def test_bibframe_jsonld_stream() -> None:
    jsonld_path = FIXTURES_DIR / "sample_bibframe.jsonld"
    records = list(iter_jsonld_bibframe_records(jsonld_path, source="test_jsonld"))
    assert len(records) == 1

    rec, offset, orig = records[0]
    assert rec["source"] == "test_jsonld"
    assert rec["title"] == "Tuntematon sotilas"
    assert rec["publication_year"] == "1954"
    assert rec["publisher"] == "WSOY"
    assert "978-951-0-12345-6" in rec["identifiers"]["isbn"]

    assert len(rec["agents"]) == 1
    assert rec["agents"][0]["name"] == "Linna, Väinö"
    assert rec["agents"][0]["role"] == "aut"

    assert len(rec["subjects"]["topical"]) == 1
    assert rec["subjects"]["topical"][0]["heading"] == "jatkosota"
    assert rec["subjects"]["topical"][0]["vocabulary"] == "yso"


def test_stream_bibframe_records_dispatcher() -> None:
    ttl_path = FIXTURES_DIR / "sample_bffi_natlibfi.ttl"
    jsonld_path = FIXTURES_DIR / "sample_bibframe.jsonld"

    # From Turtle
    recs_ttl = list(stream_bibframe_records(ttl_path))
    assert len(recs_ttl) == 1
    assert recs_ttl[0][0]["title"] == "Sinuhe egyptiläinen"

    # From JSON-LD
    recs_json = list(stream_bibframe_records(jsonld_path))
    assert len(recs_json) == 1
    assert recs_json[0][0]["title"] == "Tuntematon sotilas"


def test_resourcesync_harvester() -> None:
    sample_urlset = b"""<?xml version="1.0" encoding="UTF-8"?>
    <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"
            xmlns:rs="http://www.openarchives.org/rs/terms/">
      <rs:md capability="resourcelist"/>
      <url>
        <loc>http://example.org/records/1.ttl</loc>
        <lastmod>2025-01-01T12:00:00Z</lastmod>
        <rs:md type="text/turtle" length="1024"/>
      </url>
      <url>
        <loc>http://example.org/records/2.ttl</loc>
        <lastmod>2025-01-02T12:00:00Z</lastmod>
        <rs:md type="text/turtle" length="2048"/>
      </url>
    </urlset>
    """
    harvester = ResourceSyncHarvester("http://example.org/resourcelist.xml", request_delay=0.0)

    mock_resp_index = MagicMock()
    mock_resp_index.content = sample_urlset
    mock_resp_index.raise_for_status = MagicMock()

    mock_resp_ttl = MagicMock()
    mock_resp_ttl.content = b"@prefix bf: <http://id.loc.gov/ontologies/bibframe/> .\n"
    mock_resp_ttl.raise_for_status = MagicMock()

    def mock_get(url: str, **kwargs: object) -> MagicMock:
        if "resourcelist.xml" in url:
            return mock_resp_index
        return mock_resp_ttl

    with patch("httpx2.Client.get", side_effect=mock_get):
        resources = list(harvester.list_resources())
        assert len(resources) == 2
        assert resources[0].loc == "http://example.org/records/1.ttl"
        assert resources[0].mime_type == "text/turtle"
        assert resources[0].length == 1024
        assert resources[1].loc == "http://example.org/records/2.ttl"

        contents = list(harvester.harvest_contents())
        assert len(contents) == 2
        item, data = contents[0]
        assert item.loc == "http://example.org/records/1.ttl"
        assert b"@prefix bf:" in data

