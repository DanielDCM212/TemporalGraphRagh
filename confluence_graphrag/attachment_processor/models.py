from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from ..parser.models import AttachmentType, ParsedTable, Provenance


@dataclass
class ExtractedAttachment:
    """
    Output of AttachmentProcessor for a single file.

    *provenance* carries the full location of the attachment inside the page:
      - page-level file  → provenance has no table_chain, row, or col
      - in-table file    → provenance.table_chain / .row / .col are set
    *source* distinguishes text-extraction ('text') from vision ('vision').
    *error* is set when extraction failed; text/tables will be empty.
    """

    attachment_id: str               # unique id from AttachmentMetadata (Confluence attachment id)
    filename: str
    attachment_type: AttachmentType
    provenance: Provenance           # full location — page-level or table/row/col
    text: str = ""
    tables: List[ParsedTable] = field(default_factory=list)
    source: str = "text"             # "text" | "vision"
    error: Optional[str] = None
