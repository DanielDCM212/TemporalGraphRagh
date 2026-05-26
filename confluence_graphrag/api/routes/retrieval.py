from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from ..deps import AdapterDep, RetrieverDep
from ..schemas import (
    NodeResponse,
    SearchRequest,
    SearchResponse,
    SearchHit,
    TimelineResponse,
)

router = APIRouter(tags=["retrieval"])


@router.post("/search", response_model=SearchResponse)
async def search(req: SearchRequest, retriever: RetrieverDep) -> SearchResponse:
    """
    Semantic search over ingested pages, events, and table rows.
    Results are ranked by combined_score = cosine_similarity × temporal_decay.
    """
    results = await retriever.search(
        query=req.query,
        node_types=req.node_types,
        include_table_rows=req.include_table_rows,
        before_date=req.before_date,
        limit=req.limit,
        query_date=req.query_date,
    )
    hits = [
        SearchHit(
            node_id=r.node_id,
            node_type=r.node_type,
            text=r.text,
            timestamp=r.timestamp,
            semantic_score=round(r.semantic_score, 4),
            temporal_score=round(r.temporal_score, 4),
            combined_score=round(r.combined_score, 4),
            properties=r.properties,
        )
        for r in results
    ]
    return SearchResponse(query=req.query, count=len(hits), results=hits)


@router.get("/graph/node/{node_id}", response_model=NodeResponse)
async def get_node(node_id: str, adapter: AdapterDep) -> NodeResponse:
    """Fetch a single graph node by its ID."""
    # search_by_property doesn't support ID lookup directly; use find_one
    docs = await adapter.nodes.find_one({"_id": node_id})
    if docs is None:
        raise HTTPException(status_code=404, detail=f"Node '{node_id}' not found")
    return NodeResponse(
        id=docs["_id"],
        type=docs["type"],
        timestamp=docs.get("timestamp"),
        is_deleted=docs.get("is_deleted", False),
        properties=docs.get("properties", {}),
    )


@router.get("/graph/app/{app_id}/timeline", response_model=TimelineResponse)
async def get_app_timeline(
    app_id: str,
    adapter: AdapterDep,
    before_date: Optional[datetime] = Query(default=None),
    limit: int = Query(default=20, ge=1, le=200),
) -> TimelineResponse:
    """
    Return all Event nodes that reference an application, ordered newest-first.
    Useful for seeing the full history of decisions/approvals/cancellations for an app.
    """
    nodes = await adapter.get_temporal_context(
        app_id=app_id,
        before_date=before_date,
        limit=limit,
    )
    events = [
        NodeResponse(
            id=n.id,
            type=n.type,
            timestamp=n.timestamp,
            is_deleted=n.is_deleted,
            properties=n.properties,
        )
        for n in nodes
    ]
    return TimelineResponse(
        app_id=app_id,
        before_date=before_date,
        count=len(events),
        events=events,
    )
