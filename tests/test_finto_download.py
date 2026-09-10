"""Unit and integration tests for FINTO value vocabularies downloader and directory ingestion."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import httpx2
import pytest

from reconciliator.cli import download_finto_cmd
from reconciliator.finto_store import FintoStore
from reconciliator.ingestion.finto import ingest_finto_file, parse_turtle_simple
from reconciliator.ingestion.finto_downloader import FintoDownloader
from reconciliator.version import get_version


def test_finto_downloader_user_agent_from_pyproject() -> None:
    """Test that default user_agent resolves version from pyproject.toml."""
    ver = get_version()
    assert ver == "0.1.0"

    downloader = FintoDownloader(api_url="https://api.finto.fi/rest/v1")
    assert downloader.user_agent == f"reconciliator/{ver}"

    custom_dl = FintoDownloader(user_agent="custom-agent/2.0")
    assert custom_dl.user_agent == "custom-agent/2.0"


def test_finto_downloader_list_vocabularies() -> None:
    """Test listing available vocabularies from Finto REST API."""
    downloader = FintoDownloader(api_url="https://api.finto.fi/rest/v1")
    sample_response = {
        "vocabularies": [
            {
                "id": "yso",
                "title": "YSO - Yleinen suomalainen ontologia",
                "uri": "http://www.yso.fi/onto/yso/",
            },
            {
                "id": "slm",
                "title": "SLM - Suomalainen lajityyppi- ja muotosanasto",
                "uri": "http://urn.fi/URN:NBN:fi:au:slm:",
            },
        ]
    }

    mock_resp = MagicMock()
    mock_resp.json.return_value = sample_response
    mock_resp.raise_for_status.return_value = None

    with patch("httpx2.Client.get", return_value=mock_resp) as mock_get:
        vocabs = downloader.list_vocabularies(lang="fi")
        assert len(vocabs) == 2
        assert vocabs[0]["id"] == "yso"
        assert vocabs[1]["id"] == "slm"
        mock_get.assert_called_once()


def test_finto_downloader_download_single(tmp_path: Path) -> None:
    """Test downloading a single vocabulary with chunked streaming and atomic write."""
    downloader = FintoDownloader(api_url="https://api.finto.fi/rest/v1")
    sample_turtle = b"@prefix skos: <http://www.w3.org/2004/02/skos/core#> .\n<http://www.yso.fi/onto/yso/p1> a skos:Concept .\n"

    mock_response = MagicMock()
    mock_response.raise_for_status.return_value = None
    mock_response.iter_bytes.return_value = [sample_turtle[:20], sample_turtle[20:]]

    mock_stream_ctx = MagicMock()
    mock_stream_ctx.__enter__.return_value = mock_response
    mock_stream_ctx.__exit__.return_value = None

    with patch("httpx2.Client.stream", return_value=mock_stream_ctx):
        dest = downloader.download_vocabulary("yso", output_dir=tmp_path)
        assert dest.exists()
        assert dest.name == "yso.ttl"
        assert dest.read_bytes() == sample_turtle
        # Check no leftover temp file
        assert not (tmp_path / "yso.ttl.tmp").exists()


def test_finto_downloader_skip_existing(tmp_path: Path) -> None:
    """Test skipping download if file exists and overwrite=False."""
    downloader = FintoDownloader(api_url="https://api.finto.fi/rest/v1")
    dest = tmp_path / "yso.ttl"
    dest.write_text("existing content", encoding="utf-8")

    with patch("httpx2.Client.stream") as mock_stream:
        # With overwrite=False, stream should NOT be called
        res = downloader.download_vocabulary("yso", output_dir=tmp_path, overwrite=False)
        assert res == dest
        mock_stream.assert_not_called()

        # With overwrite=True, stream SHOULD be called
        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.iter_bytes.return_value = [b"new content"]
        mock_stream_ctx = MagicMock()
        mock_stream_ctx.__enter__.return_value = mock_response
        mock_stream_ctx.__exit__.return_value = None
        mock_stream.return_value = mock_stream_ctx

        res2 = downloader.download_vocabulary("yso", output_dir=tmp_path, overwrite=True)
        assert res2 == dest
        mock_stream.assert_called_once()
        assert dest.read_text(encoding="utf-8") == "new content"


def test_finto_downloader_download_batch(tmp_path: Path) -> None:
    """Test downloading a batch of vocabularies."""
    downloader = FintoDownloader(api_url="https://api.finto.fi/rest/v1")

    mock_response = MagicMock()
    mock_response.raise_for_status.return_value = None
    mock_response.iter_bytes.return_value = [b"turtle data"]
    mock_stream_ctx = MagicMock()
    mock_stream_ctx.__enter__.return_value = mock_response
    mock_stream_ctx.__exit__.return_value = None

    progress_records: list[tuple[str, int, int]] = []

    def on_prog(v_id: str, idx: int, total: int) -> None:
        progress_records.append((v_id, idx, total))

    with patch("httpx2.Client.stream", return_value=mock_stream_ctx):
        downloaded = downloader.download_vocabularies(
            ["yso", "slm", "seko"],
            output_dir=tmp_path,
            on_progress=on_prog,
        )
        assert len(downloaded) == 3
        assert (tmp_path / "yso.ttl").exists()
        assert (tmp_path / "slm.ttl").exists()
        assert (tmp_path / "seko.ttl").exists()
        assert len(progress_records) == 3
        assert progress_records[0] == ("yso", 1, 3)
        assert progress_records[2] == ("seko", 3, 3)


def test_finto_downloader_http_error(tmp_path: Path) -> None:
    """Test error handling when HTTP request fails."""
    downloader = FintoDownloader(api_url="https://api.finto.fi/rest/v1")

    mock_response = MagicMock()
    req = httpx2.Request("GET", "https://api.finto.fi/rest/v1/invalid/data")
    mock_response.raise_for_status.side_effect = httpx2.HTTPStatusError(
        "404 Not Found", request=req, response=MagicMock()
    )

    mock_stream_ctx = MagicMock()
    mock_stream_ctx.__enter__.return_value = mock_response
    mock_stream_ctx.__exit__.return_value = None

    with patch("httpx2.Client.stream", return_value=mock_stream_ctx):
        with pytest.raises(httpx2.HTTPStatusError):
            downloader.download_vocabulary("invalid", output_dir=tmp_path)

    # Ensure no leftover temporary file
    assert not (tmp_path / "invalid.ttl.tmp").exists()
    assert not (tmp_path / "invalid.ttl").exists()


def test_parse_turtle_prefixed_and_yso_paikat() -> None:
    """Test parsing Turtle with prefix declarations and yso-paikat geographic concepts."""
    content = """
    @prefix skos: <http://www.w3.org/2004/02/skos/core#> .
    @prefix yso: <http://www.yso.fi/onto/yso/> .
    @prefix ysop: <http://www.yso.fi/onto/yso-paikat/> .

    yso:p123 a skos:Concept ;
        skos:prefLabel "lääkärit"@fi ;
        skos:prefLabel "läkare"@sv .

    ysop:p999 a skos:Concept ;
        skos:prefLabel "Helsinki"@fi ;
        skos:prefLabel "Helsingfors"@sv .
    """
    concepts, mappings = parse_turtle_simple(content)
    assert len(concepts) == 2

    # yso concept
    c0 = concepts[0]
    assert c0.uri == "http://www.yso.fi/onto/yso/p123"
    assert c0.vocabulary == "yso"
    assert c0.pref_label_fi == "lääkärit"
    assert c0.target_field == "650"

    # yso-paikat concept (geographic name -> 651)
    c1 = concepts[1]
    assert c1.uri == "http://www.yso.fi/onto/yso-paikat/p999"
    assert c1.vocabulary == "yso-paikat"
    assert c1.pref_label_fi == "Helsinki"
    assert c1.target_field == "651"


def test_ingest_finto_directory(tmp_path: Path) -> None:
    """Test ingesting an entire directory containing multiple .ttl files into finto.db."""
    finto_dir = tmp_path / "finto_vocabularies"
    finto_dir.mkdir()

    ttl_1 = finto_dir / "yso.ttl"
    ttl_1.write_text(
        """@prefix skos: <http://www.w3.org/2004/02/skos/core#> .
<http://www.yso.fi/onto/yso/p1> a skos:Concept ;
    skos:prefLabel "kirjat"@fi .
""",
        encoding="utf-8",
    )

    ttl_2 = finto_dir / "slm.ttl"
    ttl_2.write_text(
        """@prefix skos: <http://www.w3.org/2004/02/skos/core#> .
<http://urn.fi/URN:NBN:fi:au:slm:s1> a skos:Concept ;
    skos:prefLabel "romaanit"@fi .
""",
        encoding="utf-8",
    )

    finto_db = tmp_path / "finto.db"
    store = FintoStore(finto_db)

    c_count, m_count = ingest_finto_file(finto_dir, store)
    assert c_count == 2
    assert m_count == 0

    c1 = store.find_concept_by_label("kirjat", vocabulary="yso")
    assert c1 is not None
    assert c1.uri == "http://www.yso.fi/onto/yso/p1"

    c2 = store.find_concept_by_label("romaanit", vocabulary="slm")
    assert c2 is not None
    assert c2.uri == "http://urn.fi/URN:NBN:fi:au:slm:s1"
    assert c2.target_field == "655"


def test_cli_download_finto_and_ingest(tmp_path: Path) -> None:
    """Test CLI download-finto command with --vocab and --ingest flags."""
    config_file = tmp_path / "config.yaml"
    finto_db = tmp_path / "finto.db"
    progress_db = tmp_path / "progress.db"
    out_dir = tmp_path / "finto_data"

    config_yaml = f"""
storage:
  finto_db: "{finto_db}"
  progress_db: "{progress_db}"
download_finto:
  output_dir: "{out_dir}"
  vocabularies: ["yso"]
"""
    config_file.write_text(config_yaml, encoding="utf-8")

    sample_turtle = b"""@prefix skos: <http://www.w3.org/2004/02/skos/core#> .
<http://www.yso.fi/onto/yso/p42> a skos:Concept ;
    skos:prefLabel "filosofia"@fi .
"""

    mock_response = MagicMock()
    mock_response.raise_for_status.return_value = None
    mock_response.iter_bytes.return_value = [sample_turtle]
    mock_stream_ctx = MagicMock()
    mock_stream_ctx.__enter__.return_value = mock_response
    mock_stream_ctx.__exit__.return_value = None

    with patch("httpx2.Client.stream", return_value=mock_stream_ctx):
        # Run CLI with --ingest
        download_finto_cmd(
            config=config_file,
            vocab=["yso"],
            output_dir=out_dir,
            all_vocabs=False,
            list_vocabs=False,
            force=False,
            ingest=True,
        )

    # 1. Verify file was downloaded
    downloaded_file = out_dir / "yso.ttl"
    assert downloaded_file.exists()
    assert downloaded_file.read_bytes() == sample_turtle

    # 2. Verify concepts were ingested into finto.db
    assert finto_db.exists()
    store = FintoStore(finto_db)
    concept = store.find_concept_by_label("filosofia", vocabulary="yso")
    assert concept is not None
    assert concept.uri == "http://www.yso.fi/onto/yso/p42"


def test_cli_download_finto_list(capsys: Any, tmp_path: Path) -> None:
    """Test CLI download-finto command with --list flag."""
    sample_response = {
        "vocabularies": [
            {
                "id": "kauno",
                "title": "Kauno - Fiktiivisen aineiston ontologia",
                "uri": "http://www.yso.fi/onto/kauno/",
            }
        ]
    }

    mock_resp = MagicMock()
    mock_resp.json.return_value = sample_response
    mock_resp.raise_for_status.return_value = None

    with patch("httpx2.Client.get", return_value=mock_resp):
        download_finto_cmd(
            config=tmp_path / "nonexistent.yaml",
            vocab=None,
            output_dir=None,
            all_vocabs=False,
            list_vocabs=True,
            force=False,
            ingest=False,
        )
