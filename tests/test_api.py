"""Integration tests for Reconciliator REST API, RBAC, Changesets, and Data Readiness."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from reconciliator.api.app import create_app
from reconciliator.config import AppConfig, EntraAuthConfig
from reconciliator.database import WEMIRepository, metadata, works_table, review_changesets_table, entity_pins_table


@pytest.fixture
def test_repo(tmp_path):
    """Create in-memory SQLite database repository for testing."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    metadata.create_all(engine)

    repo = WEMIRepository.__new__(WEMIRepository)
    repo.engine = engine
    repo.corpus_engine = engine
    repo.work_indexer = None
    repo.manifestation_indexer = None
    repo.agent_indexer = None
    repo.fts_config = None
    repo.embedder = None
    return repo


@pytest.fixture
def client(test_repo):
    """Create FastAPI test client with local dev auth mode."""
    cfg = AppConfig()
    cfg.auth = EntraAuthConfig(enabled=False, dev_default_role="Admin")
    app = create_app(config=cfg, repo=test_repo, embedder=None)
    return TestClient(app)


def test_system_health(client):
    """Test /api/v1/system/health."""
    res = client.get("/api/v1/system/health")
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "ok"
    assert data["service"] == "reconciliator"


def test_system_readiness_unpopulated(client):
    """Test /api/v1/system/readiness on an unpopulated database."""
    res = client.get("/api/v1/system/readiness")
    assert res.status_code == 200
    data = res.json()
    assert data["database_connected"] is True
    assert data["overall_readiness"] == "unpopulated"
    assert data["entity_counts"]["works"] == 0
    assert data["vector_store"]["available"] is False
    assert data["pipeline_readiness"]["authorities"]["ready"] is False


def test_auth_config_dev_mode(client):
    """Test /api/v1/auth/config reports dev_mode when auth is disabled."""
    res = client.get("/api/v1/auth/config")
    assert res.status_code == 200
    data = res.json()
    assert data["enabled"] is False
    assert data["dev_mode"] is True
    assert data["dev_default_role"] == "Admin"


def test_role_enforcement_with_header(client):
    """Test RBAC role enforcement using X-Dev-Role header."""
    # Reader tries to create changeset -> 403 Forbidden
    res_reader = client.post(
        "/api/v1/changesets",
        json={"title": "Unauthorized Changeset"},
        headers={"X-Dev-Role": "Reader"},
    )
    assert res_reader.status_code == 403

    # Curator can create changeset -> 201 Created
    res_curator = client.post(
        "/api/v1/changesets",
        json={"title": "Curator Changeset"},
        headers={"X-Dev-Role": "Curator"},
    )
    assert res_curator.status_code == 201
    cs_id = res_curator.json()["id"]

    # Curator tries to commit changeset -> 403 Forbidden (Admin only)
    res_commit_curator = client.post(
        f"/api/v1/changesets/{cs_id}/commit",
        headers={"X-Dev-Role": "Curator"},
    )
    assert res_commit_curator.status_code == 403

    # Admin can commit changeset -> 200 OK
    res_commit_admin = client.post(
        f"/api/v1/changesets/{cs_id}/commit",
        headers={"X-Dev-Role": "Admin"},
    )
    assert res_commit_admin.status_code == 200
    assert res_commit_admin.json()["status"] == "committed"


def test_search_empty_and_lexical(client, test_repo):
    """Test search endpoint with empty results and lexical matching."""
    # Empty search
    res = client.get("/api/v1/search?q=UnknownTitle")
    assert res.status_code == 200
    data = res.json()
    assert data["total"] == 0
    assert data["vector_available"] is False

    # Insert a test work
    with test_repo.get_connection() as conn:
        conn.execute(
            works_table.insert().values(
                source="melinda",
                source_id="1001",
                record_origin="melinda",
                title="Seitsemän veljestä",
                title_normalized="seitseman veljesta",
                work_type="text",
            )
        )
        conn.commit()

    # Search for inserted work
    res2 = client.get("/api/v1/search?q=Seitseman")
    assert res2.status_code == 200
    data2 = res2.json()
    assert data2["total"] >= 1
    assert data2["works"][0]["title"] == "Seitsemän veljestä"

    # Search with matching work_type
    res_text = client.get("/api/v1/search?q=Seitseman&work_type=text")
    assert res_text.status_code == 200
    assert len(res_text.json()["works"]) == 1

    # Search with non-matching work_type
    res_music = client.get("/api/v1/search?q=Seitseman&work_type=music")
    assert res_music.status_code == 200
    assert len(res_music.json()["works"]) == 0

    # List works endpoint with work_type filter
    res_list_text = client.get("/api/v1/works?work_type=text")
    assert res_list_text.status_code == 200
    assert res_list_text.json()["total"] >= 1

    res_list_music = client.get("/api/v1/works?work_type=music")
    assert res_list_music.status_code == 200
    assert res_list_music.json()["total"] == 0


def test_review_changeset_lifecycle(client, test_repo):
    """Test the complete changeset lifecycle: create, stage operations, commit, and revert."""
    # 1. Create a changeset
    res_create = client.post(
        "/api/v1/changesets",
        json={"title": "Viola Review Batch", "description": "Consolidating duplicates"},
        headers={"X-Dev-Role": "Curator"},
    )
    assert res_create.status_code == 201
    cs = res_create.json()
    cs_id = cs["id"]
    assert cs["status"] == "draft"

    # 2. Stage an operation
    res_op = client.post(
        f"/api/v1/changesets/{cs_id}/operations",
        json={
            "operation_type": "work_merge",
            "entity_type": "work",
            "entity_id": "1",
            "parameters": {"work_ids": [1, 2], "canonical_id": 1},
            "notes": "Merged identical recordings",
        },
        headers={"X-Dev-Role": "Curator"},
    )
    assert res_op.status_code == 201
    op = res_op.json()
    assert op["operation_type"] == "work_merge"
    assert op["sequence"] == 1

    # 3. Retrieve changeset details
    res_get = client.get(f"/api/v1/changesets/{cs_id}")
    assert res_get.status_code == 200
    cs_details = res_get.json()
    assert cs_details["operations_count"] == 1
    assert len(cs_details["operations"]) == 1

    # 4. Commit changeset as Admin
    res_commit = client.post(
        f"/api/v1/changesets/{cs_id}/commit",
        headers={"X-Dev-Role": "Admin"},
    )
    assert res_commit.status_code == 200
    committed = res_commit.json()
    assert committed["status"] == "committed"

    # 5. Verify pin created
    pins = test_repo.get_pinned_entities("work_consolidation")
    assert "1" in pins or "2" in pins

    # 6. Revert changeset as Admin
    res_revert = client.post(
        f"/api/v1/changesets/{cs_id}/revert",
        headers={"X-Dev-Role": "Admin"},
    )
    assert res_revert.status_code == 200
    reverted = res_revert.json()
    assert reverted["status"] == "reverted"

    # Verify pins were cleared
    pins_after = test_repo.get_pinned_entities("work_consolidation")
    assert len(pins_after) == 0


def test_pipeline_trigger_authorization(client):
    """Test that only Admins can trigger pipeline batch jobs."""
    # Reader rejected
    res_reader = client.post(
        "/api/v1/pipeline/run/consolidate-works",
        headers={"X-Dev-Role": "Reader"},
    )
    assert res_reader.status_code == 403

    # Curator rejected
    res_curator = client.post(
        "/api/v1/pipeline/run/consolidate-works",
        headers={"X-Dev-Role": "Curator"},
    )
    assert res_curator.status_code == 403

    # Admin accepted
    res_admin = client.post(
        "/api/v1/pipeline/run/consolidate-works",
        headers={"X-Dev-Role": "Admin"},
    )
    assert res_admin.status_code == 202
    assert res_admin.json()["status"] == "accepted"


def test_static_spa_fallback(client):
    """Test that static index.html is served for client-side routes."""
    res = client.get("/")
    assert res.status_code == 200
    assert "Reconciliator" in res.text
    # Check for compact navbar monogram and theme toggle
    assert "theme-toggle-btn" in res.text
    assert "reconciliator_theme" in res.text
    assert "reconciliator_emblem" in res.text
    assert "reconciliator_large_logo.svg" in res.text


def test_asset_serving(client):
    """Test that hero graphic / logo assets are properly served with correct mime types."""
    res = client.get("/assets/reconciliator_large_logo.svg")
    assert res.status_code == 200
    assert "image/svg+xml" in res.headers.get("content-type", "")
    assert "<svg" in res.text

    # Test emblem SVG and PNG assets
    res_emblem_svg = client.get("/assets/reconciliator_emblem.svg")
    assert res_emblem_svg.status_code == 200
    assert "image/svg+xml" in res_emblem_svg.headers.get("content-type", "")

    res_emblem_png = client.get("/assets/reconciliator_emblem.png")
    assert res_emblem_png.status_code == 200
    assert "image/png" in res_emblem_png.headers.get("content-type", "")




