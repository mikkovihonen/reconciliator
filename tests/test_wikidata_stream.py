"""Unit tests for streaming gzipped Wikidata dump and constant-RAM pre-filtering."""

import gzip
from pathlib import Path

import orjson

from reconciliator.ingestion.wikidata import stream_wikidata_dump


def test_stream_wikidata_dump(tmp_path: Path) -> None:
    gz_path = tmp_path / "test_wikidata.json.gz"

    # Create dummy entities: 1 non-bibliographic, 1 author, 1 book
    non_bib = {"id": "Q1", "claims": {}, "labels": {"en": {"value": "Universe"}}}
    author = {
        "id": "Q273115",
        "claims": {
            "P31": [{"mainsnak": {"datavalue": {"value": {"id": "Q5"}}}}],
            "P3595": [{"mainsnak": {"datavalue": {"value": "000123456"}}}],
            "P213": [{"mainsnak": {"datavalue": {"value": "0000000121477983"}}}],
        },
        "labels": {"fi": {"value": "Mika Waltari"}},
    }
    book = {
        "id": "Q12345",
        "claims": {
            "P31": [{"mainsnak": {"datavalue": {"value": {"id": "Q571"}}}}],
            "P1476": [{"mainsnak": {"datavalue": {"value": {"text": "Sinuhe egyptiläinen"}}}}],
            "P212": [{"mainsnak": {"datavalue": {"value": "978-951-0-00001-0"}}}],
            "P50": [{"mainsnak": {"datavalue": {"value": {"id": "Q273115"}}}}],
        },
        "labels": {"fi": {"value": "Sinuhe egyptiläinen"}},
    }

    with gzip.open(gz_path, "wb") as f:
        f.write(b"[\n")
        f.write(orjson.dumps(non_bib) + b",\n")
        f.write(orjson.dumps(author) + b",\n")
        f.write(orjson.dumps(book) + b"\n")
        f.write(b"]\n")

    items = list(stream_wikidata_dump(gz_path))
    assert len(items) == 1
    rec = items[0]
    assert rec["source_id"] == "Q12345"
    assert rec["title"] == "Sinuhe egyptiläinen"
    assert "978-951-0-00001-0" in rec["identifiers"]["isbn"]
    assert len(rec["agents"]) == 1
    assert rec["agents"][0]["name"] == "Mika Waltari"
    assert "https://urn.fi/URN:NBN:fi:au:pn:000123456" in rec["agents"][0]["uri_subfield_0"]


def test_stream_sample_wikidata_fixture() -> None:
    fixture_path = Path(__file__).parent / "fixtures" / "sample_wikidata.json.gz"
    assert fixture_path.exists()
    items = list(stream_wikidata_dump(fixture_path))
    assert len(items) == 1
    rec = items[0]
    assert rec["source_id"] == "Q478440"
    assert rec["title"] == "Sinuhe egyptiläinen"
    assert "978-951-0-00001-0" in rec["identifiers"]["isbn"]
    assert len(rec["agents"]) == 1
    assert rec["agents"][0]["name"] == "Mika Waltari"
    assert "https://urn.fi/URN:NBN:fi:au:pn:000123456" in rec["agents"][0]["uri_subfield_0"]
