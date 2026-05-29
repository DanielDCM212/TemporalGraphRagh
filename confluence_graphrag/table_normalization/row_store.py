from __future__ import annotations

import logging
from datetime import datetime
from typing import List

from pymongo import AsyncMongoClient
from pymongo import ASCENDING

from ..ingestion.config import IngestionConfig
from .models import NormalizedRow

logger = logging.getLogger(__name__)

_COLLECTION = "normalized_rows"


class NormalizedRowStore:
    """
    Persists consolidated table rows to MongoDB.

    Document layout:
    {
        "_id":             "{page_id}_table_{table_index}_row_{row_index}",
        "page_id":         "12345678",
        "table_type":      "agreements",
        "table_index":     0,
        "row_index":       3,
        "page_date":       ISODate,
        "is_deleted":      false,
        "values": {
            "agreement":   "Deploy app X to production",
            "responsible": "Juan Perez",
            "due_date":    "2024-06-01",
            "status":      "pending"
        },
        "cancelled_fields": ["status"],
        "provenance": { "path": "...", "page_id": "...", ... }
    }

    Values are stored as plain strings. Cancelled cells are tracked
    separately in `cancelled_fields` so the text is never lost.
    """

    def __init__(self, config: IngestionConfig) -> None:
        client = AsyncMongoClient(config.mongodb_uri)
        self._col = client[config.mongodb_db][_COLLECTION]

    async def setup_indexes(self) -> None:
        await self._col.create_index([("page_id", ASCENDING)])
        await self._col.create_index([("table_type", ASCENDING)])
        await self._col.create_index([("is_deleted", ASCENDING)])
        await self._col.create_index([("page_date", ASCENDING)])
        await self._col.create_index(
            [("table_type", ASCENDING), ("is_deleted", ASCENDING)],
            name="type_active",
        )

    async def upsert_rows(self, rows: List[NormalizedRow]) -> None:
        for row in rows:
            doc = _to_doc(row)
            await self._col.replace_one({"_id": doc["_id"]}, doc, upsert=True)
        if rows:
            logger.debug(
                "Upserted %d normalized rows (type=%s, page=%s)",
                len(rows),
                rows[0].provenance.get("table_type"),
                rows[0].provenance.get("page_id"),
            )

    async def soft_delete_page(self, page_id: str) -> int:
        result = await self._col.update_many(
            {"page_id": page_id, "is_deleted": False},
            {"$set": {"is_deleted": True}},
        )
        return result.modified_count


# ── Serialization ─────────────────────────────────────────────────────────────

def _to_doc(row: NormalizedRow) -> dict:
    prov = row.provenance
    page_id     = prov.get("page_id", "")
    table_index = prov.get("table_index", 0)
    row_index   = prov.get("row", 0)
    page_date   = prov.get("page_date")

    # Values as plain strings; cancelled cells tracked separately
    values: dict = {}
    cancelled_fields: list = []
    for field, cell in row.values.items():
        values[field] = cell.value
        if cell.is_cancelled:
            cancelled_fields.append(field)

    return {
        "_id":              f"{page_id}_table_{table_index}_row_{row_index}",
        "page_id":          page_id,
        "table_type":       prov.get("table_type", ""),
        "table_index":      table_index,
        "row_index":        row_index,
        "page_date":        datetime.fromisoformat(page_date) if isinstance(page_date, str) else page_date,
        "is_deleted":       False,
        "values":           values,
        "cancelled_fields": cancelled_fields,
        "provenance":       prov,
    }
