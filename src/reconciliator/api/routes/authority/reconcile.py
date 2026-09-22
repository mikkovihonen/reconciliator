"""W3C / OpenRefine Reconciliation Service API endpoints for Reconciliator."""

from __future__ import annotations

import json
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query, Request, Response, status
from fastapi.responses import HTMLResponse
from rapidfuzz import fuzz

from reconciliator.config import AppConfig, OpenRefineConfig
from reconciliator.database import WEMIRepository
from reconciliator.models import AgentCluster, WorkCluster

router = APIRouter(prefix="/api/v1/reconcile", tags=["OpenRefine Reconciliation API"])


def _get_openrefine_config(request: Request) -> OpenRefineConfig:
    app_cfg: AppConfig = getattr(request.app.state, "config", None) or AppConfig()
    return app_cfg.virtual_authority.openrefine


@router.get("")
@router.post("")
async def reconcile(
    request: Request,
    queries: Optional[str] = Query(None, description="URL-encoded JSON batch queries"),
) -> Any:
    """OpenRefine Reconciliation service endpoint.

    Returns the service manifest when queried without queries, or batch candidate matches when queries are provided.
    """
    repo: WEMIRepository = getattr(request.app.state, "repo", None)
    cfg = _get_openrefine_config(request)

    raw_queries = queries
    if not raw_queries and request.method == "POST":
        try:
            content_type = request.headers.get("content-type", "")
            if "application/x-www-form-urlencoded" in content_type:
                body_bytes = await request.body()
                from urllib.parse import parse_qs
                parsed_qs = parse_qs(body_bytes.decode("utf-8"))
                if "queries" in parsed_qs:
                    raw_queries = parsed_qs["queries"][0]
            else:
                body = await request.json()
                if isinstance(body, dict) and "queries" in body:
                    raw_queries = body["queries"]
                elif isinstance(body, dict):
                    raw_queries = json.dumps(body)
        except Exception:
            pass

    # 1. Manifest mode
    if not raw_queries:
        return {
            "name": cfg.service_name,
            "identifierSpace": cfg.identifier_space,
            "schemaSpace": cfg.schema_space,
            "defaultTypes": [{"id": t.id, "name": t.name} for t in cfg.default_types],
            "view": {"url": "/authority/agent/{{id}}"},
            "preview": {
                "url": "/api/v1/reconcile/preview/{{id}}",
                "width": cfg.preview_width,
                "height": cfg.preview_height,
            },
            "suggest": {
                "entity": {
                    "service_url": "/api/v1/reconcile/suggest/entity",
                    "service_path": "",
                },
                "type": {
                    "service_url": "/api/v1/reconcile/suggest/type",
                    "service_path": "",
                },
                "property": {
                    "service_url": "/api/v1/reconcile/suggest/property",
                    "service_path": "",
                },
            },
        }

    # 2. Batch Reconciliation Mode
    if not repo:
        return {}

    try:
        parsed_queries = json.loads(raw_queries) if isinstance(raw_queries, str) else raw_queries
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid JSON in 'queries' parameter"
        )

    results: dict[str, dict[str, list[dict[str, Any]]]] = {}

    for q_id, q_data in parsed_queries.items():
        q_str = (q_data.get("query") or "").strip()
        q_type = (q_data.get("type") or "agent").lower()
        limit = min(int(q_data.get("limit") or cfg.max_candidates), 50)

        candidates: list[dict[str, Any]] = []

        if q_type in ("agent", "person", "organization", "all"):
            clusters, _ = repo.get_agent_clusters_paginated(limit=100, offset=0)
            scored_candidates = []
            for c in clusters:
                score = fuzz.token_sort_ratio(q_str.lower(), c.canonical_name.lower())
                if score >= 50.0:
                    scored_candidates.append((c, score))
            scored_candidates.sort(key=lambda x: x[1], reverse=True)

            for c, score in scored_candidates[:limit]:
                is_exact = score >= 95.0
                candidates.append(
                    {
                        "id": c.cluster_id,
                        "name": c.canonical_access_point or c.canonical_name,
                        "type": [{"id": "agent", "name": "Agent"}],
                        "score": round(score, 2),
                        "match": is_exact,
                    }
                )

        results[q_id] = {"result": candidates}

    return results


@router.get("/preview/{cluster_id}", response_class=HTMLResponse)
def preview_cluster(
    request: Request,
    cluster_id: str,
) -> HTMLResponse:
    """HTML preview card rendering for OpenRefine hover flyouts."""
    repo: WEMIRepository = getattr(request.app.state, "repo", None)
    if not repo:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Cluster not found")

    # Check agent cluster
    agent_cluster = repo.get_agent_cluster_full(cluster_id)
    if agent_cluster:
        dates_display = ""
        if agent_cluster.birth_year or agent_cluster.death_year:
            dates_display = f"({agent_cluster.birth_year or ''} – {agent_cluster.death_year or ''})"

        variant_names = [
            a.value
            for a in agent_cluster.attributes
            if a.attribute_type == "variant_name" and a.value != agent_cluster.canonical_name
        ]
        variants_html = ", ".join(variant_names[:4]) if variant_names else "None"

        member_sources = ", ".join({m.source.upper() for m in agent_cluster.members})

        html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; margin: 0; padding: 14px; color: #1e293b; background: #ffffff; font-size: 13px; }}
h2 {{ margin: 0 0 4px 0; font-size: 15px; color: #0f172a; font-weight: 600; }}
.badge {{ display: inline-block; padding: 2px 6px; border-radius: 4px; font-size: 11px; font-weight: 600; text-transform: uppercase; }}
.badge-est {{ background: #dcfce7; color: #166534; }}
.badge-prov {{ background: #fef9c3; color: #854d0e; }}
.prop {{ margin: 6px 0; }}
.label {{ font-weight: 600; color: #64748b; font-size: 11px; text-transform: uppercase; }}
.uri {{ color: #2563eb; text-decoration: none; word-break: break-all; font-family: monospace; font-size: 11px; }}
</style>
</head>
<body>
    <div style="display: flex; justify-content: space-between; align-items: baseline;">
        <h2>{agent_cluster.canonical_name}</h2>
        <span class="badge {'badge-est' if agent_cluster.authority_status == 'established' else 'badge-prov'}">{agent_cluster.authority_status.upper()}</span>
    </div>
    <div style="color: #64748b; margin-bottom: 8px;">{dates_display}</div>
    <div class="prop"><span class="label">Cluster PID:</span> <span class="uri">{agent_cluster.cluster_id}</span></div>
    <div class="prop"><span class="label">Contributing Sources:</span> {member_sources}</div>
    <div class="prop"><span class="label">Variants:</span> {variants_html}</div>
    <div class="prop"><span class="label">Associated Works:</span> {agent_cluster.work_count}</div>
</body>
</html>"""
        return HTMLResponse(content=html)

    # Check work cluster
    work_cluster = repo.get_work_cluster_full(cluster_id)
    if work_cluster:
        html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; margin: 0; padding: 14px; color: #1e293b; background: #ffffff; font-size: 13px; }}
h2 {{ margin: 0 0 4px 0; font-size: 15px; color: #0f172a; font-weight: 600; }}
.badge {{ display: inline-block; padding: 2px 6px; border-radius: 4px; font-size: 11px; font-weight: 600; text-transform: uppercase; }}
.badge-est {{ background: #dcfce7; color: #166534; }}
.prop {{ margin: 6px 0; }}
.label {{ font-weight: 600; color: #64748b; font-size: 11px; text-transform: uppercase; }}
</style>
</head>
<body>
    <div style="display: flex; justify-content: space-between; align-items: baseline;">
        <h2>{work_cluster.canonical_title}</h2>
        <span class="badge badge-est">{work_cluster.authority_status.upper()}</span>
    </div>
    <div style="color: #64748b; margin-bottom: 8px;">{work_cluster.canonical_author or 'Unknown Author'}</div>
    <div class="prop"><span class="label">Cluster PID:</span> {work_cluster.cluster_id}</div>
    <div class="prop"><span class="label">Work Type:</span> {work_cluster.work_type}</div>
</body>
</html>"""
        return HTMLResponse(content=html)

    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Cluster not found")


@router.get("/suggest/entity")
def suggest_entity(
    request: Request,
    prefix: str = Query(..., min_length=1),
) -> dict[str, Any]:
    """Auto-complete entity suggestion for OpenRefine."""
    repo: WEMIRepository = getattr(request.app.state, "repo", None)
    if not repo:
        return {"result": []}

    clusters, _ = repo.get_agent_clusters_paginated(limit=50, offset=0)
    suggestions = []
    for c in clusters:
        if prefix.lower() in c.canonical_name.lower():
            desc = f"{c.authority_status.upper()} • {c.member_count} sources"
            if c.birth_year or c.death_year:
                desc = f"({c.birth_year or ''}-{c.death_year or ''}) • {desc}"
            suggestions.append(
                {
                    "id": c.cluster_id,
                    "name": c.canonical_access_point or c.canonical_name,
                    "description": desc,
                }
            )
        if len(suggestions) >= 10:
            break
    return {"result": suggestions}


@router.get("/suggest/type")
def suggest_type(request: Request) -> dict[str, Any]:
    """Entity types supported by Reconciliator Virtual Authority."""
    cfg = _get_openrefine_config(request)
    return {"result": [{"id": t.id, "name": t.name} for t in cfg.default_types]}


@router.get("/suggest/property")
def suggest_property() -> dict[str, Any]:
    """Properties supported for reconciliation queries."""
    return {
        "result": [
            {"id": "birth_year", "name": "Birth Year"},
            {"id": "death_year", "name": "Death Year"},
            {"id": "affiliation", "name": "Affiliation / Group"},
            {"id": "occupation", "name": "Occupation / Profession"},
            {"id": "variant_name", "name": "Variant Name / Pseudonym"},
        ]
    }
