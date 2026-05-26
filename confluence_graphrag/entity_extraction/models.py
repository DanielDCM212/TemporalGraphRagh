from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Dict, List, Optional

from pydantic import BaseModel, Field


class EventType(str, Enum):
    DECISION      = "decision"
    STATUS_CHANGE = "status_change"
    CANCELLATION  = "cancellation"
    APPROVAL      = "approval"
    RISK          = "risk"
    ACTION_ITEM   = "action_item"


class CandidateId(BaseModel):
    raw_value: str
    id_type: str                        # "app_id" | "project_id"
    context: str                        # surrounding text window for manual review
    provenance_path: str
    normalized_value: Optional[str] = None   # after LLM normalization


class ValidatedId(BaseModel):
    candidate: CandidateId
    is_valid: bool
    api_response: Optional[Dict] = None


class ExtractedEvent(BaseModel):
    event_type: EventType
    description: str
    app_ids: List[str] = Field(default_factory=list)
    project_ids: List[str] = Field(default_factory=list)
    is_cancelled: bool = False
    provenance_path: str = ""


class LLMExtractionResult(BaseModel):
    """Structured output from the LLM entity extractor (per page call)."""
    additional_app_ids: List[str] = Field(
        default_factory=list,
        description="app_ids (11 digits) found in non-standard formats not caught by regex",
    )
    additional_project_ids: List[str] = Field(
        default_factory=list,
        description="project_ids (9 digits) found in non-standard formats not caught by regex",
    )
    events: List[ExtractedEvent] = Field(
        default_factory=list,
        description="Decisions, approvals, cancellations, risks and action items",
    )


class EntitySet(BaseModel):
    """Output of Stage 4 for a single page — input to Stage 5 (Graph Construction)."""
    page_id: str
    page_date: datetime
    app_ids: List[str]           # validated
    project_ids: List[str]       # validated
    unvalidated_ids: List[CandidateId]
    events: List[ExtractedEvent]


class UnvalidatedIdRecord(BaseModel):
    """Persisted to MongoDB unvalidated_ids collection for manual review."""
    candidate_id: str
    id_type: str
    provenance_path: str
    context: str
    page_id: str
    detected_at: datetime
    status: str = "pending_review"   # pending_review | confirmed_manual | rejected
