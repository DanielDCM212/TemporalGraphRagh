from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field


# ── Ingestion ─────────────────────────────────────────────────────────────────

class SpaceIngestRequest(BaseModel):
    space_key: str
    start_date: Optional[datetime] = None
    end_date: Optional[datetime] = None


class PageIngestRequest(BaseModel):
    space_key: str
    page_ids: List[str] = Field(min_length=1)


class JobResponse(BaseModel):
    job_id: str
    status: Literal["running", "done", "error"]
    started_at: datetime
    finished_at: Optional[datetime] = None
    stats: Optional[Dict[str, int]] = None   # total/ok/errors/skipped
    error: Optional[str] = None


class IngestionLogResponse(BaseModel):
    page_id: str
    page_title: str
    page_date: datetime
    space_key: str
    status: str
    processed_at: Optional[datetime] = None
    attachment_count: int = 0
    retry_count: int = 0
    error_message: Optional[str] = None


# ── Retrieval ─────────────────────────────────────────────────────────────────

class SearchRequest(BaseModel):
    query: str = Field(min_length=1)
    node_types: Optional[List[str]] = None   # e.g. ["Event", "ConfPage"]
    include_table_rows: bool = True
    before_date: Optional[datetime] = None
    query_date: Optional[datetime] = None
    limit: int = Field(default=20, ge=1, le=100)


class SearchHit(BaseModel):
    node_id: str
    node_type: str
    text: str
    timestamp: Optional[datetime]
    semantic_score: float
    temporal_score: float
    combined_score: float
    properties: Dict[str, Any]


class SearchResponse(BaseModel):
    query: str
    count: int
    results: List[SearchHit]


class NodeResponse(BaseModel):
    id: str
    type: str
    timestamp: Optional[datetime]
    is_deleted: bool
    properties: Dict[str, Any]


class TimelineResponse(BaseModel):
    app_id: str
    before_date: Optional[datetime]
    count: int
    events: List[NodeResponse]
