from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import List, Optional, Protocol, runtime_checkable


class IngestionStatus(str, Enum):
    PENDING      = "pending"
    PROCESSING   = "processing"
    DONE         = "done"
    ERROR        = "error"
    NEEDS_REVIEW = "needs_review"  # page title has no parseable date


@dataclass
class IngestionLogEntry:
    page_id: str
    page_title: str
    page_date: datetime
    space_key: str
    confluence_last_modified: datetime
    processed_at: datetime
    status: IngestionStatus
    error_message: Optional[str] = None
    attachment_count: int = 0
    retry_count: int = 0


@dataclass
class AttachmentMetadata:
    attachment_id: str
    filename: str
    media_type: str
    download_url: str
    size_bytes: int


@dataclass
class PageMetadata:
    page_id: str
    title: str
    space_key: str
    html_content: str
    last_modified: datetime
    page_date: datetime                              # extracted from title
    attachments: List[AttachmentMetadata] = field(default_factory=list)


@runtime_checkable
class PagePipeline(Protocol):
    """Contract between the Ingestion Controller and the downstream stages."""

    async def ingest(self, metadata: PageMetadata) -> None:
        """Process a single page through the full pipeline (Stage 2 → Stage 5)."""
        ...

    async def soft_delete_page(self, page_id: str) -> None:
        """Mark all graph nodes of a page as deleted before re-ingesting (upsert)."""
        ...
