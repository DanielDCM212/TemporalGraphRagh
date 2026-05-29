from __future__ import annotations

import io
import logging
from typing import List, Tuple

from ...parser.models import (
    ParsedCell,
    ParsedTable,
    Provenance,
    TableRef,
    TextChunk,
    TextStyle,
)

logger = logging.getLogger(__name__)


class ExcelExtractor:
    """Extract text and tables from Excel files (.xlsx / .xls) using openpyxl."""

    def extract(
        self,
        data: bytes,
        filename: str,
        provenance: Provenance,
    ) -> Tuple[str, List[ParsedTable]]:
        import openpyxl

        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        text_parts: List[str] = []
        tables: List[ParsedTable] = []

        for sheet_idx, sheet in enumerate(wb.worksheets):
            rows: List[List[str]] = []
            for row in sheet.iter_rows(values_only=True):
                rows.append([str(c) if c is not None else "" for c in row])

            # Drop completely empty rows
            rows = [r for r in rows if any(v.strip() for v in r)]
            if not rows:
                continue

            # Flatten to text
            text_parts.append(f"[Sheet: {sheet.title}]")
            text_parts.extend(" | ".join(row) for row in rows)

            # Build ParsedTable
            chain = provenance.table_chain.copy()
            chain.append(TableRef(table_index=sheet_idx, row=None, col=None))
            table_prov = Provenance(
                page_id=provenance.page_id,
                page_title=provenance.page_title,
                page_date=provenance.page_date,
                table_chain=chain,
                attachment_id=provenance.attachment_id,
                attachment_type=provenance.attachment_type,
            )

            headers = rows[0] if rows else []
            cells_matrix: List[List[ParsedCell]] = []
            for row_idx, row in enumerate(rows):
                row_cells: List[ParsedCell] = []
                for col_idx, cell_value in enumerate(row):
                    text = cell_value.strip()
                    cell_prov = Provenance(
                        page_id=provenance.page_id,
                        page_title=provenance.page_title,
                        page_date=provenance.page_date,
                        table_chain=chain.copy(),
                        row=row_idx,
                        col=col_idx,
                        attachment_id=provenance.attachment_id,
                        attachment_type=provenance.attachment_type,
                    )
                    chunks = (
                        [TextChunk(content=text, style=TextStyle.NORMAL, provenance=cell_prov)]
                        if text else []
                    )
                    row_cells.append(ParsedCell(
                        row=row_idx, col=col_idx,
                        text_chunks=chunks,
                        provenance=cell_prov,
                    ))
                cells_matrix.append(row_cells)

            tables.append(ParsedTable(
                table_index=sheet_idx,
                headers=headers,
                cells=cells_matrix,
                provenance=table_prov,
                raw_html=f"[Excel sheet: {sheet.title}]",
            ))

        wb.close()
        return "\n".join(text_parts), tables
