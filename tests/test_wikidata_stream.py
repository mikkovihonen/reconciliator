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


def test_wikidata_composer_and_author_string_and_publisher(tmp_path: Path) -> None:
    gz_path = tmp_path / "test_agents_pub.json.gz"

    # 1. Composer entity (P86)
    composer_item = {
        "id": "Q88888",
        "claims": {
            "P31": [{"mainsnak": {"datavalue": {"value": {"id": "Q5"}}}}],
            "P213": [{"mainsnak": {"datavalue": {"value": "0000000122843075"}}}],
        },
        "labels": {"fi": {"value": "Jean Sibelius"}},
    }

    # 2. Book with composer, author string P2093, and P1810 publisher qualifier
    book = {
        "id": "Q99999",
        "claims": {
            "P31": [{"mainsnak": {"datavalue": {"value": {"id": "Q571"}}}}],
            "P1476": [
                {"mainsnak": {"datavalue": {"value": {"text": "Viulukonsertto"}}}},
            ],
            "P86": [{"mainsnak": {"datavalue": {"value": {"id": "Q88888"}}}}],
            "P2093": [{"mainsnak": {"datavalue": {"value": "Runoilija Tuntematon"}}}],
            "P123": [
                {
                    "mainsnak": {"datavalue": {"value": {"id": "Q11886025"}}},
                    "qualifiers": {
                        "P1810": [{"datavalue": {"value": "Kustannusosakeyhtiö Otava"}}]
                    },
                }
            ],
            "P291": [
                {
                    "mainsnak": {"datavalue": {"value": {"id": "Q1757"}}},
                    "qualifiers": {"P1810": [{"datavalue": {"value": "Helsinki"}}]},
                }
            ],
        },
        "labels": {"fi": {"value": "Viulukonsertto"}},
    }

    # 3. Book with unresolvable raw QID publisher (should suppress to None)
    book_unresolved = {
        "id": "Q77777",
        "claims": {
            "P31": [{"mainsnak": {"datavalue": {"value": {"id": "Q571"}}}}],
            "P1476": [{"mainsnak": {"datavalue": {"value": {"text": "Tuntematon teos"}}}}],
            "P123": [{"mainsnak": {"datavalue": {"value": {"id": "Q99999999"}}}}],
            "P2093": [{"mainsnak": {"datavalue": {"value": "Kirjailija X"}}}],
        },
        "labels": {"fi": {"value": "Tuntematon teos"}},
    }

    with gzip.open(gz_path, "wb") as f:
        f.write(b"[\n")
        f.write(orjson.dumps(composer_item) + b",\n")
        f.write(orjson.dumps(book) + b",\n")
        f.write(orjson.dumps(book_unresolved) + b"\n")
        f.write(b"]\n")

    items = list(stream_wikidata_dump(gz_path))
    assert len(items) == 2

    rec1 = items[0]
    assert rec1["source_id"] == "Q99999"
    assert rec1["publisher"] == "Kustannusosakeyhtiö Otava"
    assert rec1["publication_place"] == "Helsinki"
    roles = {a["role"] for a in rec1["agents"]}
    assert "cmp" in roles
    assert any(a["name"] == "Jean Sibelius" and a["role"] == "cmp" for a in rec1["agents"])

    rec2 = items[1]
    assert rec2["source_id"] == "Q77777"
    # Unresolved raw QID must be suppressed to None
    assert rec2["publisher"] is None
    # P2093 fallback author present
    assert len(rec2["agents"]) == 1
    assert rec2["agents"][0]["name"] == "Kirjailija X"
    assert rec2["agents"][0]["role"] == "aut"
