"""Tests for dynamic Bruno test suite generator and endpoints."""

from __future__ import annotations

import io
import json
import shutil
import subprocess
import zipfile
from pathlib import Path

from fastapi.testclient import TestClient

from reconciliator.api.app import create_app
from reconciliator.api.bruno import BrunoCollectionGenerator
from reconciliator.config import AppConfig, BrunoConfig


def test_bruno_config_defaults():
    """Verify Bruno configuration defaults and overrides."""
    app_cfg = AppConfig()
    assert app_cfg.bruno.collection_name == "Reconciliator API"
    assert app_cfg.bruno.version == "1"
    assert app_cfg.bruno.environment_name == "local"
    assert app_cfg.bruno.base_url == "http://localhost:8000"
    assert app_cfg.bruno.include_assertions is True
    assert app_cfg.bruno.default_timeout_ms == 5000

    custom = BrunoConfig(
        collection_name="Custom Reconciliator Test",
        base_url="https://api.staging.example.com",
        environment_name="staging",
        include_assertions=False,
    )
    assert custom.collection_name == "Custom Reconciliator Test"
    assert custom.base_url == "https://api.staging.example.com"
    assert custom.include_assertions is False


def test_bruno_generator_file_structure():
    """Verify BrunoCollectionGenerator generates valid collection files."""
    app = create_app()
    generator = BrunoCollectionGenerator(app.openapi())
    files = generator.generate_collection_files()

    # Manifest
    assert "bruno.json" in files
    manifest = json.loads(files["bruno.json"])
    assert manifest["version"] == "1"
    assert manifest["name"] == "Reconciliator API"
    assert manifest["type"] == "collection"

    # Environment
    assert "environments/local.bru" in files
    env_content = files["environments/local.bru"]
    assert "baseUrl: http://localhost:8000" in env_content

    # Tag folders and .bru files
    assert any(k.startswith("system/") for k in files)
    assert any(k.startswith("authentication/") for k in files)

    # Health check file
    health_files = [k for k in files if "health" in k]
    assert len(health_files) > 0
    health_content = files[health_files[0]]
    assert "meta {" in health_content
    assert "get {" in health_content
    assert "url: {{baseUrl}}/api/v1/system/health" in health_content
    assert "assert {" in health_content
    assert "res.status: eq 200" in health_content
    assert "res.body.status: eq ok" in health_content


def test_bruno_zip_endpoint():
    """Verify GET /api/v1/system/bruno.zip exports a valid zip file."""
    app = create_app()
    client = TestClient(app)

    response = client.get("/api/v1/system/bruno.zip")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    assert "reconciliator-bruno.zip" in response.headers["content-disposition"]

    # Verify zip content
    with zipfile.ZipFile(io.BytesIO(response.content)) as zf:
        namelist = zf.namelist()
        assert "bruno.json" in namelist
        assert "environments/local.bru" in namelist
        assert any(n.endswith(".bru") and "system" in n for n in namelist)

        # Ensure valid non-empty files
        manifest_data = zf.read("bruno.json").decode("utf-8")
        assert json.loads(manifest_data)["name"] == "Reconciliator API"


def test_bruno_json_endpoint():
    """Verify GET /api/v1/system/bruno/json returns full JSON structure."""
    app = create_app()
    client = TestClient(app)

    response = client.get("/api/v1/system/bruno/json")
    assert response.status_code == 200
    data = response.json()

    assert data["name"] == "Reconciliator API"
    assert data["files_count"] > 10
    assert "bruno.json" in data["files"]
    assert "environments/local.bru" in data["files"]


def test_bruno_custom_query_params():
    """Verify base_url and assertions toggles via query params."""
    app = create_app()
    client = TestClient(app)

    response = client.get(
        "/api/v1/system/bruno/json?base_url=http://dev.local:9000&include_assertions=false"
    )
    assert response.status_code == 200
    data = response.json()

    assert data["environment"]["base_url"] == "http://dev.local:9000"
    health_files = [k for k in data["files"] if "health" in k]
    assert len(health_files) > 0
    # Assertion should be disabled
    assert "assert {" not in data["files"][health_files[0]]


def test_bruno_cli_syntax_validation(tmp_path: Path):
    """Verify that Bruno CLI (if installed) can parse the generated collection without syntax errors."""
    bru_bin = shutil.which("bru")
    if not bru_bin:
        return

    app = create_app()
    generator = BrunoCollectionGenerator(app.openapi())
    files = generator.generate_collection_files()

    collection_dir = tmp_path / "reconciliator_collection"
    collection_dir.mkdir(parents=True, exist_ok=True)

    for rel_path, content in files.items():
        dest = collection_dir / rel_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding="utf-8")

    # Run bru check / formatting or dry run
    # bru run returns error code if request fails to connect, but will fail with parse error if syntax is invalid
    res = subprocess.run(
        [bru_bin, "run", str(collection_dir / "system"), "--env", "local"],
        capture_output=True,
        text=True,
        check=False,
    )
    # The output should NOT report syntax/parsing errors
    assert "SyntaxError" not in res.stderr
    assert "Unable to parse" not in res.stderr
