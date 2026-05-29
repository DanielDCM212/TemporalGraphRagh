from __future__ import annotations

import logging
from datetime import datetime
from typing import List, Optional

from pymongo import AsyncMongoClient
from pymongo import ASCENDING

from .config import IngestionConfig
from .models import IngestionLogEntry, IngestionStatus

logger = logging.getLogger(__name__)

_COLLECTION = "ingestion_log"


class IngestionLog:
    """
    MongoDB-backed log that tracks the ingestion state of every Confluence page.
    It is the source of truth for:
      - which pages have been processed
      - whether a page needs re-processing (upsert)
      - error / retry state
    """

    def __init__(self, config: IngestionConfig):
        client = AsyncMongoClient(config.mongodb_uri)
        self._col = client[config.mongodb_db][_COLLECTION]

    async def setup_indexes(self) -> None:
        await self._col.create_index([("page_id", ASCENDING)], unique=True)
        await self._col.create_index([("status", ASCENDING)])
        await self._col.create_index([("space_key", ASCENDING)])
        logger.debug("ingestion_log indexes ensured")

    # ── Read ─────────────────────────────────────────────────────────────────

    async def get(self, page_id: str) -> Optional[IngestionLogEntry]:
        doc = await self._col.find_one({"page_id": page_id})
        return _from_doc(doc) if doc else None

    async def needs_upsert(self, page_id: str, confluence_last_modified: datetime) -> bool:
        """
        True when the page has never been ingested, failed previously, or
        Confluence has a newer version than what we last processed.
        """
        doc = await self._col.find_one(
            {"page_id": page_id},
            {"status": 1, "confluence_last_modified": 1},
        )
        if doc is None:
            return True
        if doc.get("status") != IngestionStatus.DONE:
            return True
        stored: Optional[datetime] = doc.get("confluence_last_modified")
        return stored is None or confluence_last_modified > stored

    async def list_errors(self, max_retry: int = 3) -> List[str]:
        """Return page_ids that failed and are below the retry limit."""
        cursor = self._col.find(
            {"status": IngestionStatus.ERROR, "retry_count": {"$lt": max_retry}},
            {"page_id": 1},
        )
        docs = await cursor.to_list()
        return [d["page_id"] for d in docs]

    # ── Write ─────────────────────────────────────────────────────────────────

    async def upsert(self, entry: IngestionLogEntry) -> None:
        await self._col.replace_one(
            {"page_id": entry.page_id},
            _to_doc(entry),
            upsert=True,
        )

    async def mark_processing(self, page_id: str) -> None:
        await self._col.update_one(
            {"page_id": page_id},
            {"$set": {
                "status": IngestionStatus.PROCESSING,
                "processed_at": datetime.utcnow(),
            }},
        )

    async def mark_done(self, page_id: str, attachment_count: int) -> None:
        await self._col.update_one(
            {"page_id": page_id},
            {"$set": {
                "status": IngestionStatus.DONE,
                "processed_at": datetime.utcnow(),
                "attachment_count": attachment_count,
                "error_message": None,
            }},
        )

    async def mark_error(self, page_id: str, error: str) -> None:
        await self._col.update_one(
            {"page_id": page_id},
            {
                "$set": {"status": IngestionStatus.ERROR, "error_message": error},
                "$inc": {"retry_count": 1},
            },
        )

    async def mark_needs_review(self, page_id: str, reason: str) -> None:
        """Page title had no parseable date — needs manual review."""
        await self._col.update_one(
            {"page_id": page_id},
            {"$set": {
                "status": IngestionStatus.NEEDS_REVIEW,
                "error_message": reason,
            }},
        )


# ── Serialization helpers ────────────────────────────────────────────────────

def _to_doc(entry: IngestionLogEntry) -> dict:
    return {
        "page_id": entry.page_id,
        "page_title": entry.page_title,
        "page_date": entry.page_date,
        "space_key": entry.space_key,
        "confluence_last_modified": entry.confluence_last_modified,
        "processed_at": entry.processed_at,
        "status": entry.status,
        "error_message": entry.error_message,
        "attachment_count": entry.attachment_count,
        "retry_count": entry.retry_count,
    }


def _from_doc(doc: dict) -> IngestionLogEntry:
    return IngestionLogEntry(
        page_id=doc["page_id"],
        page_title=doc["page_title"],
        page_date=doc.get("page_date", datetime.min),
        space_key=doc["space_key"],
        confluence_last_modified=doc.get("confluence_last_modified", datetime.min),
        processed_at=doc.get("processed_at", datetime.min),
        status=IngestionStatus(doc.get("status", IngestionStatus.PENDING)),
        error_message=doc.get("error_message"),
        attachment_count=doc.get("attachment_count", 0),
        retry_count=doc.get("retry_count", 0),
    )
