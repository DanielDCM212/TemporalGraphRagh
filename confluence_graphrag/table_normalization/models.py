from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Dict, List, Optional


class SchemaStatus(str, Enum):
    PENDING_APPROVAL = "pending_approval"   # D1: batch — waits for human sign-off
    APPROVED         = "approved"           # Human reviewed and approved
    AUTO_APPROVED    = "auto_approved"      # D1: incremental — used immediately


@dataclass
class TableFingerprint:
    table_id: str               # "{page_id}_table_{table_index}"
    page_id: str
    page_date: datetime
    raw_headers: List[str]
    col_count: int
    row_count: int
    sample_values: List[List[str]]   # up to 3 data rows, for LLM context


@dataclass
class CanonicalSchema:
    schema_id: str
    table_type: str                     # "agreements", "applications", "risks", …
    description: str
    canonical_columns: List[str]        # D6: English snake_case field names
    column_mapping: Dict[str, str]      # raw variant → canonical field
    status: SchemaStatus
    approved_by: Optional[str] = None
    approved_at: Optional[datetime] = None
    version: int = 1


@dataclass
class NormalizedCell:
    value: str
    is_cancelled: bool


@dataclass
class NormalizedRow:
    """
    One data row after applying a CanonicalSchema to a ParsedTable.
    Stored in the per-type master collection in MongoDB.
    """
    values: Dict[str, NormalizedCell]   # canonical_column → cell
    provenance: Dict                    # serialized path + metadata for RAG citations
