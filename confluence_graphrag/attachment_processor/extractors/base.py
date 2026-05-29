from __future__ import annotations

from typing import List, Protocol, Tuple

from ...parser.models import ParsedTable, Provenance


class AttachmentExtractor(Protocol):
    """
    Sync extraction contract for a single file type.

    Implementors receive raw *data* bytes and the full *provenance* (already
    populated with attachment_id / attachment_type / table_chain / row / col)
    and return:
      - extracted text (str)
      - zero or more ParsedTable objects built with parser models so they are
        handled identically to page-body tables downstream
    """

    def extract(
        self,
        data: bytes,
        filename: str,
        provenance: Provenance,
    ) -> Tuple[str, List[ParsedTable]]:
        ...
