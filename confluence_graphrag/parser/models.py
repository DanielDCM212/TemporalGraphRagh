from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import List, Optional


class TextStyle(Enum):
    NORMAL    = "normal"
    CANCELLED = "cancelled"
    BOLD      = "bold"
    UNDERLINE = "underline"
    ITALIC    = "italic"


class AttachmentType(Enum):
    PDF     = "pdf"
    EXCEL   = "excel"
    DOCX    = "docx"
    PPTX    = "pptx"
    IMAGE   = "image"
    UNKNOWN = "unknown"


@dataclass
class TableRef:
    table_index: int
    row: Optional[int]
    col: Optional[int]


@dataclass
class Provenance:
    page_id: str
    page_title: str
    page_date: datetime
    table_chain: List[TableRef] = field(default_factory=list)
    row: Optional[int] = None
    col: Optional[int] = None
    attachment_id: Optional[str] = None
    attachment_type: Optional[AttachmentType] = None

    def to_path(self) -> str:
        parts = [f"page:{self.page_id}"]
        for ref in self.table_chain:
            parts.append(f"table:{ref.table_index}")
            if ref.row is not None:
                parts.append(f"row:{ref.row}/col:{ref.col}")
        if self.row is not None:
            parts.append(f"row:{self.row}/col:{self.col}")
        if self.attachment_id:
            parts.append(f"attachment:{self.attachment_id}")
        return "/".join(parts)


@dataclass
class TextChunk:
    content: str
    style: TextStyle
    provenance: Provenance


@dataclass
class AttachmentRef:
    attachment_id: str
    filename: str
    url: str
    attachment_type: AttachmentType
    provenance: Provenance


@dataclass
class ParsedCell:
    row: int
    col: int
    text_chunks: List[TextChunk] = field(default_factory=list)
    attachments: List[AttachmentRef] = field(default_factory=list)
    sub_tables: List[ParsedTable] = field(default_factory=list)
    provenance: Optional[Provenance] = None
    is_propagated: bool = False  # True when this cell was copied from a rowspan in a prior row


@dataclass
class ParsedTable:
    table_index: int
    headers: List[str]
    cells: List[List[ParsedCell]]
    provenance: Provenance
    raw_html: str


@dataclass
class ContentTree:
    page_id: str
    page_title: str
    page_date: datetime
    text_blocks: List[TextChunk]
    tables: List[ParsedTable]
    attachments: List[AttachmentRef]  # page-level attachments (not inside tables)
    raw_html: str
