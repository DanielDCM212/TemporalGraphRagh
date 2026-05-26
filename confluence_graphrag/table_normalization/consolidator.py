from __future__ import annotations

import logging
from typing import Dict, List, Optional

from confluence_graphrag.parser.models import ParsedCell, ParsedTable, TextStyle

from .models import CanonicalSchema, NormalizedCell, NormalizedRow

logger = logging.getLogger(__name__)


class TableConsolidator:
    """
    Applies a CanonicalSchema to a ParsedTable and returns a list of
    NormalizedRow objects ready for insertion into the master MongoDB collection.

    Each row includes a `_provenance` dict so the Retrieval layer can cite
    the exact source (page, table, row) in its answers.
    """

    def consolidate(
        self,
        table: ParsedTable,
        schema: CanonicalSchema,
    ) -> List[NormalizedRow]:
        col_map = self._build_col_map(table.headers, schema)
        if not col_map:
            logger.warning(
                "No column mapping found for table %s (type=%s) — skipping",
                table.table_index, schema.table_type,
            )
            return []

        rows: List[NormalizedRow] = []
        start = 1 if table.headers else 0

        for row_idx, row_cells in enumerate(table.cells[start:], start=start):
            values: Dict[str, NormalizedCell] = {}

            for cell in row_cells:
                canonical = col_map.get(cell.col)
                if not canonical:
                    continue
                values[canonical] = NormalizedCell(
                    value=_cell_text(cell),
                    is_cancelled=_cell_is_cancelled(cell),
                )

            # Skip rows that are completely empty after mapping
            if not any(v.value for v in values.values()):
                continue

            rows.append(NormalizedRow(
                values=values,
                provenance={
                    "path": table.provenance.to_path(),
                    "page_id": table.provenance.page_id,
                    "page_date": table.provenance.page_date.isoformat(),
                    "table_index": table.table_index,
                    "table_type": schema.table_type,
                    "row": row_idx,
                },
            ))

        return rows

    # ── Helpers ──────────────────────────────────────────────────────────────

    @staticmethod
    def _build_col_map(
        headers: List[str], schema: CanonicalSchema
    ) -> Dict[int, str]:
        """Maps column index → canonical field name via schema.column_mapping."""
        col_map: Dict[int, str] = {}
        for col_idx, header in enumerate(headers):
            # Direct lookup first
            canonical = schema.column_mapping.get(header)
            if canonical:
                col_map[col_idx] = canonical
                continue
            # Case-insensitive fallback
            header_lower = header.lower().strip()
            for variant, field in schema.column_mapping.items():
                if variant.lower().strip() == header_lower:
                    col_map[col_idx] = field
                    break
        return col_map


# ── Cell helpers ─────────────────────────────────────────────────────────────

def _cell_text(cell: ParsedCell) -> str:
    return " ".join(c.content for c in cell.text_chunks).strip()


def _cell_is_cancelled(cell: ParsedCell) -> bool:
    return any(c.style == TextStyle.CANCELLED for c in cell.text_chunks)
