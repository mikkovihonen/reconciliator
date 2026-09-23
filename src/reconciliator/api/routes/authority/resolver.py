"""Authority entity resolution and HTTP content negotiation endpoints.

Handles persistent cluster identifier dereferencing, automatic 308 permanent redirections for
merged clusters, 410 Gone for tombstoned clusters, and format negotiation for MARCXML, JSON-LD,
Turtle, and REST JSON.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse, RedirectResponse

from reconciliator.authority.serializers.marc21 import MARC21AuthoritySerializer
from reconciliator.authority.serializers.skos import SKOSAuthoritySerializer
from reconciliator.config import AppConfig
from reconciliator.database import WEMIRepository

router = APIRouter(prefix="/authority", tags=["Authority Resolution & Content Negotiation"])


@router.get("/agent/{cluster_id}")
def resolve_agent_authority(
    request: Request,
    cluster_id: str,
) -> Any:
    """Resolve and dereference an agent cluster with HTTP content negotiation and redirection support."""
    repo: WEMIRepository = getattr(request.app.state, "repo", None)
    if not repo:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Authority cluster not found"
        )

    # 1. Check Redirection / Split / Tombstone ledger
    redirection = repo.get_cluster_redirection(cluster_id)
    if redirection:
        if not redirection.target_cluster_id:
            raise HTTPException(
                status_code=status.HTTP_410_GONE,
                detail=f"Authority cluster {cluster_id} has been dissolved/tombstoned ({redirection.reason})",
            )
        return RedirectResponse(
            url=f"/authority/agent/{redirection.target_cluster_id}",
            status_code=redirection.status_code,
        )

    # 2. Load full cluster with attributes and members
    cluster = repo.get_agent_cluster_full(cluster_id)
    if not cluster:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Agent cluster {cluster_id} not found"
        )

    # 3. Content Negotiation via Accept header
    accept = (request.headers.get("accept") or "").lower()
    app_cfg: AppConfig = getattr(request.app.state, "config", None) or AppConfig()
    vaf_cfg = app_cfg.virtual_authority

    if "application/marcxml+xml" in accept or "application/xml" in accept:
        serializer = MARC21AuthoritySerializer(vaf_cfg.serialization.marc21)
        xml_content = serializer.serialize_agent_cluster(cluster)
        return Response(content=xml_content, media_type="application/marcxml+xml")

    if "application/ld+json" in accept:
        serializer_skos = SKOSAuthoritySerializer(vaf_cfg.serialization.skos)
        jsonld_doc = serializer_skos.serialize_agent_cluster_jsonld(cluster)
        return JSONResponse(content=jsonld_doc, media_type="application/ld+json")

    if "text/turtle" in accept:
        serializer_skos = SKOSAuthoritySerializer(vaf_cfg.serialization.skos)
        turtle_content = serializer_skos.serialize_agent_cluster_turtle(cluster)
        return Response(content=turtle_content, media_type="text/turtle")

    # Default to structured JSON
    c_dict = cluster.model_dump()
    c_dict["members"] = [m.model_dump() for m in cluster.members]
    c_dict["attributes"] = [a.model_dump() for a in cluster.attributes]
    c_dict["lifecycle_events"] = [
        e.model_dump() for e in repo.get_cluster_lifecycle_events(cluster_id)
    ]
    return c_dict


@router.get("/work/{cluster_id}")
def resolve_work_authority(
    request: Request,
    cluster_id: str,
) -> Any:
    """Resolve and dereference a work cluster with HTTP content negotiation and redirection support."""
    repo: WEMIRepository = getattr(request.app.state, "repo", None)
    if not repo:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Authority cluster not found"
        )

    # 1. Check Redirection ledger
    redirection = repo.get_cluster_redirection(cluster_id)
    if redirection:
        if not redirection.target_cluster_id:
            raise HTTPException(
                status_code=status.HTTP_410_GONE,
                detail=f"Work cluster {cluster_id} has been dissolved/tombstoned ({redirection.reason})",
            )
        return RedirectResponse(
            url=f"/authority/work/{redirection.target_cluster_id}",
            status_code=redirection.status_code,
        )

    # 2. Load work cluster
    cluster = repo.get_work_cluster_full(cluster_id)
    if not cluster:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Work cluster {cluster_id} not found"
        )

    # 3. Content Negotiation via Accept header
    accept = (request.headers.get("accept") or "").lower()
    app_cfg: AppConfig = getattr(request.app.state, "config", None) or AppConfig()
    vaf_cfg = app_cfg.virtual_authority

    if "application/marcxml+xml" in accept or "application/xml" in accept:
        serializer = MARC21AuthoritySerializer(vaf_cfg.serialization.marc21)
        xml_content = serializer.serialize_work_cluster(cluster)
        return Response(content=xml_content, media_type="application/marcxml+xml")

    if "application/ld+json" in accept:
        serializer_skos = SKOSAuthoritySerializer(vaf_cfg.serialization.skos)
        jsonld_doc = serializer_skos.serialize_work_cluster_jsonld(cluster)
        return JSONResponse(content=jsonld_doc, media_type="application/ld+json")

    if "text/turtle" in accept:
        serializer_skos = SKOSAuthoritySerializer(vaf_cfg.serialization.skos)
        turtle_content = serializer_skos.serialize_work_cluster_turtle(cluster)
        return Response(content=turtle_content, media_type="text/turtle")

    c_dict = cluster.model_dump()
    c_dict["lifecycle_events"] = [
        e.model_dump() for e in repo.get_cluster_lifecycle_events(cluster_id)
    ]
    return c_dict
