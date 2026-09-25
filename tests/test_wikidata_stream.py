"""Unit tests for streaming gzipped Wikidata dump and constant-RAM pre-filtering."""

import gzip
from pathlib import Path

import orjson

from reconciliator.config import VectorEngineConfig
from reconciliator.database import WEMIRepository
from reconciliator.ingestion.wikidata import stream_wikidata_dump


def _make_store(tmp_path: Path) -> WEMIRepository:
    """Create a temporary WEMIRepository for testing."""
    db_path = tmp_path / "authority.db"
    index_path = tmp_path / "authority.index"
    vec_cfg = VectorEngineConfig(backend="usearch", device="cpu", mode="embedded")
    return WEMIRepository(db_path, index_path, dimension=64, vector_engine_config=vec_cfg)


def test_stream_wikidata_dump(tmp_path: Path) -> None:
    gz_path = tmp_path / "test_wikidata.json.gz"
    store = _make_store(tmp_path)

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

    items = list(stream_wikidata_dump(gz_path, store=store))
    assert len(items) == 1
    rec = items[0]
    assert rec["source_id"] == "Q12345"
    assert rec["title"] == "Sinuhe egyptiläinen"
    assert "978-951-0-00001-0" in rec["identifiers"]["isbn"]
    assert len(rec["agents"]) == 1
    assert rec["agents"][0]["name"] == "Mika Waltari"
    assert any(
        u in rec["agents"][0]["uri_subfield_0"]
        for u in ("https://urn.fi/URN:NBN:fi:au:pn:000123456", "kanto:000123456")
    )


def test_stream_sample_wikidata_fixture(tmp_path: Path) -> None:
    fixture_path = Path(__file__).parent / "fixtures" / "sample_wikidata.json.gz"
    assert fixture_path.exists()
    store = _make_store(tmp_path)
    items = list(stream_wikidata_dump(fixture_path, store=store))
    assert len(items) == 1
    rec = items[0]
    assert rec["source_id"] == "Q478440"
    assert rec["title"] == "Sinuhe egyptiläinen"
    assert "978-951-0-00001-0" in rec["identifiers"]["isbn"]
    assert len(rec["agents"]) == 1
    assert rec["agents"][0]["name"] == "Mika Waltari"
    assert any(
        u in rec["agents"][0]["uri_subfield_0"]
        for u in ("https://urn.fi/URN:NBN:fi:au:pn:000123456", "kanto:000123456")
    )


def test_wikidata_composer_and_author_string_and_publisher(tmp_path: Path) -> None:
    gz_path = tmp_path / "test_agents_pub.json.gz"
    store = _make_store(tmp_path)

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

    items = list(stream_wikidata_dump(gz_path, store=store))
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


def test_wikidata_author_authority_filtering(tmp_path: Path) -> None:
    from reconciliator.ingestion.wikidata import _extract_author_data

    # Human with no authority IDs (e.g. footballer)
    athlete = {
        "id": "Q1001",
        "claims": {
            "P31": [{"mainsnak": {"datavalue": {"value": {"id": "Q5"}}}}],
        },
        "labels": {"en": {"value": "Random Athlete"}},
    }

    # Human with VIAF (P214)
    author_viaf = {
        "id": "Q1002",
        "claims": {
            "P31": [{"mainsnak": {"datavalue": {"value": {"id": "Q5"}}}}],
            "P214": [{"mainsnak": {"datavalue": {"value": "12345678"}}}],
        },
        "labels": {"en": {"value": "VIAF Author"}},
    }

    # Human with GND (P227)
    author_gnd = {
        "id": "Q1003",
        "claims": {
            "P31": [{"mainsnak": {"datavalue": {"value": {"id": "Q5"}}}}],
            "P227": [{"mainsnak": {"datavalue": {"value": "118540238"}}}],
        },
        "labels": {"de": {"value": "Goethe"}},
    }

    # By default (require_authority_id=True), athlete is skipped
    assert _extract_author_data(athlete, require_authority_id=True) is None
    # VIAF and GND authors are kept
    assert _extract_author_data(author_viaf, require_authority_id=True) is not None
    assert _extract_author_data(author_gnd, require_authority_id=True) is not None

    # When require_authority_id=False, athlete is also kept
    res = _extract_author_data(athlete, require_authority_id=False)
    assert res is not None
    assert res["name"] == "Random Athlete"


def test_author_lru_cache() -> None:
    from reconciliator.ingestion.wikidata import AuthorLRUCache

    cache = AuthorLRUCache(maxsize=3)
    assert len(cache) == 0

    cache.set("Q1", {"name": "Author 1"})
    cache.set("Q2", {"name": "Author 2"})
    cache.set("Q3", {"name": "Author 3"})

    assert len(cache) == 3
    assert "Q1" in cache
    assert "Q2" in cache
    assert "Q3" in cache
    assert "Q4" not in cache

    # Access Q1 to make it most recently used
    assert cache.get("Q1") == {"name": "Author 1"}

    # Insert Q4 -> Q2 should be evicted (oldest)
    cache.set("Q4", {"name": "Author 4"})
    assert len(cache) == 3
    assert "Q2" not in cache
    assert "Q1" in cache
    assert "Q3" in cache
    assert "Q4" in cache

    # Missing lookup returns None
    assert cache.get("Q999") is None


def test_find_pigz_binary(tmp_path: Path) -> None:
    from reconciliator.ingestion.wikidata import find_pigz_binary

    fake_pigz = tmp_path / "fake_pigz"
    fake_pigz.touch(mode=0o755)

    assert find_pigz_binary(str(fake_pigz)) == str(fake_pigz)
    assert find_pigz_binary(str(tmp_path / "non_existent")) is None


def test_open_dump_stream_fallback(tmp_path: Path) -> None:
    from reconciliator.config import WikidataIngestConfig
    from reconciliator.ingestion.wikidata import open_dump_stream

    data_file = tmp_path / "test.json.gz"
    payload = b'{"id": "Q1"}\n{"id": "Q2"}\n'
    with gzip.open(data_file, "wb") as f:
        f.write(payload)

    # 1. Test fallback with use_pigz=False (uses gzip.open directly)
    cfg_no_pigz = WikidataIngestConfig(dump_path=str(data_file), use_pigz=False)
    stream, proc = open_dump_stream(data_file, config=cfg_no_pigz)
    try:
        assert proc is None
        lines = [line.strip() for line in stream]
        assert lines == [b'{"id": "Q1"}', b'{"id": "Q2"}']
    finally:
        stream.close()

    # 2. Test with default config (pigz or gzip -dc subprocess)
    cfg_default = WikidataIngestConfig(dump_path=str(data_file), use_pigz=True)
    stream2, proc2 = open_dump_stream(data_file, config=cfg_default)
    try:
        lines2 = [line.strip() for line in stream2]
        assert lines2 == [b'{"id": "Q1"}', b'{"id": "Q2"}']
    finally:
        stream2.close()
        if proc2 is not None:
            try:
                proc2.terminate()
                proc2.wait(timeout=1.0)
            except Exception:
                pass


def test_chunk_stream_lines() -> None:
    import io

    from reconciliator.ingestion.wikidata import _chunk_stream_lines

    stream = io.BytesIO(b"line1\nline2\nline3\nline4\nline5\n")
    chunks = list(_chunk_stream_lines(stream, chunk_size=2))
    assert len(chunks) == 3
    assert chunks[0] == [b"line1\n", b"line2\n"]
    assert chunks[1] == [b"line3\n", b"line4\n"]
    assert chunks[2] == [b"line5\n"]


def test_worker_parse_authors_and_raw_bib_chunks() -> None:
    from reconciliator.ingestion.wikidata import (
        _worker_parse_authors_chunk,
        _worker_parse_raw_bib_chunk,
    )

    author_line = (
        orjson.dumps(
            {
                "id": "Q10",
                "claims": {
                    "P31": [{"mainsnak": {"datavalue": {"value": {"id": "Q5"}}}}],
                    "P213": [{"mainsnak": {"datavalue": {"value": "0000 0001 2345 6789"}}}],
                },
                "labels": {"fi": {"value": "Kirjailija"}},
            }
        )
        + b"\n"
    )

    book_line = (
        orjson.dumps(
            {
                "id": "Q20",
                "claims": {
                    "P31": [{"mainsnak": {"datavalue": {"value": {"id": "Q571"}}}}],
                    "P1476": [{"mainsnak": {"datavalue": {"value": {"text": "Kirja"}}}}],
                    "P212": [{"mainsnak": {"datavalue": {"value": "978-951-0-12345-6"}}}],
                },
                "labels": {"fi": {"value": "Kirja"}},
            }
        )
        + b"\n"
    )

    chunk = [b"[\n", author_line, book_line, b"]\n"]

    authors = _worker_parse_authors_chunk((chunk, True, None))
    assert len(authors) == 1
    assert authors[0]["qid"] == "Q10"
    assert authors[0]["name"] == "Kirjailija"

    bibs = _worker_parse_raw_bib_chunk(chunk)
    assert len(bibs) == 1
    assert bibs[0]["id"] == "Q20"


def test_prefetch_batch_qids(tmp_path: Path) -> None:
    from reconciliator.ingestion.wikidata import AuthorLRUCache, prefetch_batch_qids

    store = _make_store(tmp_path)
    store.insert_wikidata_authors(
        [
            {"qid": "Q101", "name": "Author 101", "identifiers": {"viaf": "101"}},
            {"qid": "Q102", "name": "Author 102", "identifiers": {"viaf": "102"}},
        ]
    )

    cache = AuthorLRUCache(maxsize=100)
    entities = [
        {
            "id": "Q201",
            "claims": {
                "P50": [{"mainsnak": {"datavalue": {"value": {"id": "Q101"}}}}],
                "P123": [{"mainsnak": {"datavalue": {"value": {"id": "Q102"}}}}],
            },
        }
    ]

    prefetch_batch_qids(entities, store=store, cache=cache)
    assert "Q101" in cache
    assert "Q102" in cache
    assert cache.get("Q101")["name"] == "Author 101"
    assert cache.get("Q102")["name"] == "Author 102"


def test_stream_wikidata_dump_multiprocess(tmp_path: Path) -> None:
    from reconciliator.config import WikidataIngestConfig

    gz_path = tmp_path / "test_mp.json.gz"
    store = _make_store(tmp_path)

    author = {
        "id": "Q555",
        "claims": {
            "P31": [{"mainsnak": {"datavalue": {"value": {"id": "Q5"}}}}],
            "P213": [{"mainsnak": {"datavalue": {"value": "0000 0001 0000 0000"}}}],
        },
        "labels": {"fi": {"value": "Moniprosessi Kirjailija"}},
    }
    book = {
        "id": "Q666",
        "claims": {
            "P31": [{"mainsnak": {"datavalue": {"value": {"id": "Q571"}}}}],
            "P1476": [{"mainsnak": {"datavalue": {"value": {"text": "Rinnakkainen Teos"}}}}],
            "P212": [{"mainsnak": {"datavalue": {"value": "978-951-0-99999-9"}}}],
            "P50": [{"mainsnak": {"datavalue": {"value": {"id": "Q555"}}}}],
        },
        "labels": {"fi": {"value": "Rinnakkainen Teos"}},
    }

    with gzip.open(gz_path, "wb") as f:
        f.write(orjson.dumps(author) + b"\n")
        f.write(orjson.dumps(book) + b"\n")

    cfg = WikidataIngestConfig(
        dump_path=str(gz_path),
        batch_size=10,
        workers=2,
        use_pigz=False,
    )

    items = list(stream_wikidata_dump(gz_path, store=store, config=cfg))
    assert len(items) == 1
    assert items[0]["source_id"] == "Q666"
    assert items[0]["title"] == "Rinnakkainen Teos"
    assert items[0]["agents"][0]["name"] == "Moniprosessi Kirjailija"


def test_insert_wikidata_authors_large_parameter_batch(tmp_path: Path) -> None:
    """Verify that batches with more than 65,535 parameters are chunked safely."""
    store = _make_store(tmp_path)

    # 3,000 authors * 8 variant names/qualifiers = >70,000 parameters for agent_attributes
    large_batch = []
    for i in range(2500):
        large_batch.append(
            {
                "qid": f"Q999{i}",
                "name": f"Author {i}",
                "family_name": f"Family {i}",
                "birth_year": 1900 + (i % 100),
                "variant_names": [f"Alias A {i}", f"Alias B {i}", f"Alias C {i}"],
                "qualifiers": [f"Qual 1 {i}", f"Qual 2 {i}", f"Qual 3 {i}"],
                "occupations": [f"Occ {i}"],
                "identifiers": {"isni": f"0000 0001 0000 {i:04d}", "viaf": f"12345{i}"},
            }
        )

    # This previously failed with:
    # OperationalError: sending query and params failed: number of parameters must be between 0 and 65535
    store.insert_wikidata_authors(large_batch, use_synchronous_commit_off=True)

    author = store.get_wikidata_author("Q9990")
    assert author is not None
    assert author["name"] == "Author 0"
    assert "Alias A 0" in author["variant_names"]
    assert author["identifiers"]["isni"] == "0000 0001 0000 0000"
