"""Unit tests for Virtual Authority File API endpoints (OpenRefine, Content Negotiation, OAI-PMH)."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from reconciliator.api.app import create_app
from reconciliator.models import (
    AgentCluster,
    AgentClusterAttribute,
    AgentClusterMember,
    ClusterRedirection,
)


@pytest.fixture
def mock_repo():
    repo = MagicMock()

    sample_cluster = AgentCluster(
        cluster_id="va_agent_test_001",
        canonical_name="Waltari, Mika",
        canonical_surname="Waltari",
        canonical_uri="urn:reconciliator:agent:va_agent_test_001",
        birth_year=1908,
        death_year=1979,
        member_count=2,
        work_count=10,
        authority_status="established",
        canonical_access_point="Waltari, Mika, 1908-1979",
        members=[
            AgentClusterMember(
                agent_id=1,
                cluster_id="va_agent_test_001",
                source="asteri",
                source_id="00001",
                link_method="exact",
                confidence=1.0,
            )
        ],
        attributes=[
            AgentClusterAttribute(
                cluster_id="va_agent_test_001",
                attribute_type="variant_name",
                value="M. Waltari",
                frequency=1,
            )
        ],
    )

    repo.get_agent_cluster_full.side_effect = lambda cid: (
        sample_cluster if cid == "va_agent_test_001" else None
    )
    repo.get_agent_clusters_paginated.return_value = ([sample_cluster], 1)
    repo.get_cluster_lifecycle_events.return_value = []

    # Redirection mock
    def get_redirection_mock(cid):
        if cid == "va_agent_old_merged":
            return ClusterRedirection(
                source_cluster_id="va_agent_old_merged",
                target_cluster_id="va_agent_test_001",
                cluster_type="agent",
                status_code=308,
                reason="merge",
            )
        if cid == "va_agent_tombstone":
            return ClusterRedirection(
                source_cluster_id="va_agent_tombstone",
                target_cluster_id=None,
                cluster_type="agent",
                status_code=308,
                reason="dissolved",
            )
        return None

    repo.get_cluster_redirection.side_effect = get_redirection_mock
    repo.list_authority_clusters_modified_since.return_value = (
        [{"cluster_id": "va_agent_test_001", "canonical_name": "Waltari, Mika"}],
        1,
    )
    return repo


@pytest.fixture
def client(mock_repo):
    app = create_app(repo=mock_repo)
    return TestClient(app)


def test_openrefine_manifest(client: TestClient):
    """Verify that GET /api/v1/reconcile returns the OpenRefine service manifest."""
    resp = client.get("/api/v1/reconcile")
    assert resp.status_code == 200
    data = resp.json()
    assert data["name"] == "Reconciliator Virtual Authority File"
    assert data["identifierSpace"] == "urn:reconciliator:"
    assert "defaultTypes" in data
    assert "preview" in data


def test_openrefine_batch_reconcile(client: TestClient):
    """Verify batch reconciliation candidate matching."""
    queries = {
        "q0": {"query": "Mika Waltari", "type": "agent", "limit": 3},
        "q1": {"query": "Nonexistent Agent", "type": "agent"},
    }
    resp = client.post("/api/v1/reconcile", params={"queries": json.dumps(queries)})
    assert resp.status_code == 200
    data = resp.json()
    assert "q0" in data
    assert len(data["q0"]["result"]) >= 1
    assert data["q0"]["result"][0]["id"] == "va_agent_test_001"
    assert data["q0"]["result"][0]["score"] > 80.0


def test_openrefine_preview_html(client: TestClient):
    """Verify that GET /api/v1/reconcile/preview/{id} returns HTML preview card."""
    resp = client.get("/api/v1/reconcile/preview/va_agent_test_001")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "Waltari, Mika" in resp.text
    assert "ESTABLISHED" in resp.text


def test_openrefine_suggestions(client: TestClient):
    """Verify OpenRefine type-ahead suggestions."""
    resp_entity = client.get("/api/v1/reconcile/suggest/entity?prefix=Walt")
    assert resp_entity.status_code == 200
    assert len(resp_entity.json()["result"]) >= 1

    resp_type = client.get("/api/v1/reconcile/suggest/type")
    assert resp_type.status_code == 200

    resp_prop = client.get("/api/v1/reconcile/suggest/property")
    assert resp_prop.status_code == 200


def test_authority_content_negotiation(client: TestClient):
    """Verify format negotiation on /authority/agent/{id} based on Accept header."""
    # 1. Default JSON
    resp_json = client.get("/authority/agent/va_agent_test_001")
    assert resp_json.status_code == 200
    assert resp_json.json()["cluster_id"] == "va_agent_test_001"

    # 2. MARCXML
    resp_xml = client.get(
        "/authority/agent/va_agent_test_001", headers={"Accept": "application/marcxml+xml"}
    )
    assert resp_xml.status_code == 200
    assert "application/marcxml+xml" in resp_xml.headers["content-type"]
    assert "<leader>" in resp_xml.text
    assert "Waltari, Mika" in resp_xml.text

    # 3. JSON-LD
    resp_jsonld = client.get(
        "/authority/agent/va_agent_test_001", headers={"Accept": "application/ld+json"}
    )
    assert resp_jsonld.status_code == 200
    assert resp_jsonld.json()["@type"] == ["skos:Concept", "schema:Person"]

    # 4. Turtle
    resp_turtle = client.get(
        "/authority/agent/va_agent_test_001", headers={"Accept": "text/turtle"}
    )
    assert resp_turtle.status_code == 200
    assert "@prefix skos:" in resp_turtle.text


def test_authority_redirection_and_tombstone(client: TestClient):
    """Verify that merged clusters return HTTP 308 redirect and tombstoned clusters return HTTP 410 Gone."""
    # Merged cluster -> 308 Permanent Redirect (follow_redirects=False)
    resp_merged = client.get("/authority/agent/va_agent_old_merged", follow_redirects=False)
    assert resp_merged.status_code == 308
    assert resp_merged.headers["location"] == "/authority/agent/va_agent_test_001"

    # Tombstoned cluster -> 410 Gone
    resp_tombstone = client.get("/authority/agent/va_agent_tombstone")
    assert resp_tombstone.status_code == 410


def test_oai_pmh_provider_verbs(client: TestClient):
    """Verify OAI-PMH 2.0 provider verbs."""
    # Identify
    resp_id = client.get("/oai/authority?verb=Identify")
    assert resp_id.status_code == 200
    assert "<repositoryName>" in resp_id.text

    # ListMetadataFormats
    resp_mf = client.get("/oai/authority?verb=ListMetadataFormats")
    assert resp_mf.status_code == 200
    assert "marc21_authority" in resp_mf.text
    assert "oai_dc" in resp_mf.text

    # ListSets
    resp_sets = client.get("/oai/authority?verb=ListSets")
    assert resp_sets.status_code == 200
    assert "<setSpec>agents</setSpec>" in resp_sets.text
