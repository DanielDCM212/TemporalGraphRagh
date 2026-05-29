from __future__ import annotations

import logging
from datetime import datetime
from typing import List, Optional

from pymongo import AsyncMongoClient
from pymongo import ASCENDING

from confluence_graphrag.ingestion.config import IngestionConfig

from .models import CanonicalSchema, SchemaStatus

logger = logging.getLogger(__name__)

_COLLECTION = "canonical_schemas"


class CanonicalSchemaStore:
    """
    MongoDB-backed store for CanonicalSchema objects.

    Approved schemas are never recalculated unless entirely new column variants
    appear (those go to needs_review).
    """

    def __init__(self, config: IngestionConfig) -> None:
        client = AsyncMongoClient(config.mongodb_uri)
        self._col = client[config.mongodb_db][_COLLECTION]

    async def setup_indexes(self) -> None:
        await self._col.create_index([("schema_id", ASCENDING)], unique=True)
        await self._col.create_index([("table_type", ASCENDING)])
        await self._col.create_index([("status", ASCENDING)])

    # ── Read ──────────────────────────────────────────────────────────────────

    async def get(self, schema_id: str) -> Optional[CanonicalSchema]:
        doc = await self._col.find_one({"schema_id": schema_id})
        return _from_doc(doc) if doc else None

    async def get_by_type(self, table_type: str) -> Optional[CanonicalSchema]:
        """Returns the latest approved schema for a table type."""
        doc = await self._col.find_one(
            {"table_type": table_type, "status": {"$in": [
                SchemaStatus.APPROVED, SchemaStatus.AUTO_APPROVED,
            ]}},
            sort=[("version", -1)],
        )
        return _from_doc(doc) if doc else None

    async def list_approved(self) -> List[CanonicalSchema]:
        cursor = self._col.find({"status": {"$in": [
            SchemaStatus.APPROVED, SchemaStatus.AUTO_APPROVED,
        ]}})
        return [_from_doc(d) async for d in cursor]

    async def list_pending(self) -> List[CanonicalSchema]:
        """D1: returns schemas waiting for human approval."""
        cursor = self._col.find({"status": SchemaStatus.PENDING_APPROVAL})
        return [_from_doc(d) async for d in cursor]

    # ── Write ─────────────────────────────────────────────────────────────────

    async def save(self, schema: CanonicalSchema) -> None:
        await self._col.replace_one(
            {"schema_id": schema.schema_id},
            _to_doc(schema),
            upsert=True,
        )

    async def save_many(self, schemas: List[CanonicalSchema]) -> None:
        for schema in schemas:
            await self.save(schema)

    async def approve(self, schema_id: str, approved_by: str) -> None:
        """Human approval — used after D1 batch review."""
        await self._col.update_one(
            {"schema_id": schema_id},
            {"$set": {
                "status": SchemaStatus.APPROVED,
                "approved_by": approved_by,
                "approved_at": datetime.utcnow(),
            }},
        )
        logger.info("Schema '%s' approved by '%s'", schema_id, approved_by)

    async def add_mapping(
        self, schema_id: str, raw_header: str, canonical_field: str
    ) -> None:
        """Extend an existing schema's column_mapping with a new variant."""
        await self._col.update_one(
            {"schema_id": schema_id},
            {"$set": {f"column_mapping.{raw_header}": canonical_field}},
        )


# ── Serialization ─────────────────────────────────────────────────────────────

def _to_doc(s: CanonicalSchema) -> dict:
    return {
        "schema_id": s.schema_id,
        "table_type": s.table_type,
        "description": s.description,
        "canonical_columns": s.canonical_columns,
        "column_mapping": s.column_mapping,
        "status": s.status,
        "approved_by": s.approved_by,
        "approved_at": s.approved_at,
        "version": s.version,
    }


def _from_doc(doc: dict) -> CanonicalSchema:
    return CanonicalSchema(
        schema_id=doc["schema_id"],
        table_type=doc["table_type"],
        description=doc.get("description", ""),
        canonical_columns=doc["canonical_columns"],
        column_mapping=doc["column_mapping"],
        status=SchemaStatus(doc["status"]),
        approved_by=doc.get("approved_by"),
        approved_at=doc.get("approved_at"),
        version=doc.get("version", 1),
    )
