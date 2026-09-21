"""Hybrid and faceted search API endpoint."""

from __future__ import annotations

import logging
from typing import Any, Literal, Optional

from fastapi import APIRouter, Query, Request

from reconciliator.database import WEMIRepository

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/search", tags=["Search"])


@router.get("")
def search(
    request: Request,
    q: str = Query(..., min_length=1, description="Search query string"),
    mode: Literal["lexical", "semantic", "hybrid"] = Query("lexical", description="Search mode"),
    entity_type: Literal["all", "work", "manifestation", "agent"] = Query("all", alias="type"),
    source: Optional[str] = Query(None, description="Filter by record origin / source catalogue"),
    work_type: Optional[str] = Query(None, description="Filter by work type (e.g. text, sound_recording)"),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    """Execute unified search across works, manifestations, and agents."""
    repo: WEMIRepository = getattr(request.app.state, "repo", None)
    embedder = getattr(request.app.state, "embedder", None)

    if not repo:
        return {
            "query": q,
            "mode": mode,
            "vector_available": False,
            "total": 0,
            "works": [],
            "manifestations": [],
            "agents": [],
        }

    work_idx = getattr(repo, "work_indexer", None)
    vector_available = (
        embedder is not None
        and work_idx is not None
        and getattr(work_idx, "total_vectors", 0) > 0
    )

    actual_mode = mode
    semantic_work_results: list[dict[str, Any]] = []

    # Execute Semantic search if requested and available
    if mode in ("semantic", "hybrid"):
        if vector_available:
            try:
                q_vec = embedder.embed_query(q)
                vec_works = repo.search_work_vectors(q_vec, top_k=limit)
                for w, score in vec_works:
                    if work_type and w.work_type != work_type:
                        continue
                    w_dict = w.model_dump()
                    w_dict["score"] = round(float(score), 4)
                    w_dict["match_type"] = "semantic_vector"
                    semantic_work_results.append(w_dict)
            except Exception as e:
                logger.warning("Semantic vector search failed: %s", e)
                vector_available = False
                actual_mode = "lexical"
        else:
            actual_mode = "lexical"

    works_results: list[dict[str, Any]] = []
    manifestations_results: list[dict[str, Any]] = []
    agents_results: list[dict[str, Any]] = []
    works_total = 0
    manifs_total = 0
    agents_total = 0

    if entity_type in ("all", "work"):
        if actual_mode == "semantic" and semantic_work_results:
            works_results = semantic_work_results
            works_total = len(semantic_work_results)
        else:
            works, works_total = repo.get_works_paginated(
                query=q, source=source, work_type=work_type, limit=limit, offset=offset
            )
            works_results = [w.model_dump() for w in works]
            for w in works_results:
                w["match_type"] = "lexical_fuzzy"

            # If hybrid, merge semantic results
            if mode == "hybrid" and semantic_work_results:
                seen_ids = {w["id"] for w in works_results}
                for sw in semantic_work_results:
                    if sw["id"] not in seen_ids:
                        works_results.append(sw)
                works_total = len(works_results)

    if entity_type in ("all", "manifestation"):
        manifs, manifs_total = repo.get_manifestations_paginated(
            query=q, source=source, limit=limit, offset=offset
        )
        manifestations_results = [m.model_dump() for m in manifs]

    if entity_type in ("all", "agent"):
        agents, agents_total = repo.get_agents_paginated(
            query=q, source=source, limit=limit, offset=offset
        )
        agents_results = [a.model_dump() for a in agents]

    total_count = works_total + manifs_total + agents_total

    return {
        "query": q,
        "mode": actual_mode,
        "requested_mode": mode,
        "vector_available": vector_available,
        "total": total_count,
        "works": works_results,
        "manifestations": manifestations_results,
        "agents": agents_results,
    }

