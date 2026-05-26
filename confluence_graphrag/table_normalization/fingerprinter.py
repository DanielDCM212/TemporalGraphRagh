from __future__ import annotations

from typing import List

from confluence_graphrag.parser.models import ParsedTable

from .models import TableFingerprint

_MAX_SAMPLE_ROWS = 3


class TableFingerprinter:
    """
    Converts a ParsedTable into a TableFingerprint — a lightweight descriptor
    used by the clusterer and LLM classifier.
    """

    def fingerprint(self, table: ParsedTable) -> TableFingerprint:
        headers = table.headers if table.headers else self._infer_headers(table)
        return TableFingerprint(
            table_id=f"{table.provenance.page_id}_table_{table.table_index}",
            page_id=table.provenance.page_id,
            page_date=table.provenance.page_date,
            raw_headers=headers,
            col_count=len(headers),
            row_count=len(table.cells),
            sample_values=self._extract_samples(table),
        )

    def fingerprint_many(self, tables: List[ParsedTable]) -> List[TableFingerprint]:
        return [self.fingerprint(t) for t in tables]

    # ── Helpers ──────────────────────────────────────────────────────────────

    @staticmethod
    def _infer_headers(table: ParsedTable) -> List[str]:
        """If no <th> row, treat the first row as implicit headers."""
        if not table.cells:
            return []
        return [
            cell.text_chunks[0].content if cell.text_chunks else ""
            for cell in table.cells[0]
        ]

    @staticmethod
    def _extract_samples(table: ParsedTable) -> List[List[str]]:
        start = 1 if table.headers else 0
        samples = []
        for row in table.cells[start : start + _MAX_SAMPLE_ROWS]:
            samples.append([
                cell.text_chunks[0].content if cell.text_chunks else ""
                for cell in row
            ])
        return samples
