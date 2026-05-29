from __future__ import annotations

import io
import logging
from typing import Any, List, Optional, Tuple

from ...parser.models import (
    ParsedCell,
    ParsedTable,
    Provenance,
    TableRef,
    TextChunk,
    TextStyle,
)

logger = logging.getLogger(__name__)

# PDF image filters that produce valid JPEG bytes we can send directly to Gemini
_JPEG_FILTERS = {"DCTDecode", "DCT"}


class PdfExtractor:
    """
    Extract text and tables from PDF files using pdfplumber.

    When *image_extractor* is provided (ENABLE_VISION=true):
      - Embedded images on each page are sent to Gemini Vision and their
        descriptions are appended to the page text.
      - Pages that return empty text (scanned / fully rasterized pages) are
        also rendered via Gemini Vision so their content is not lost.
    """

    def __init__(self, image_extractor: Optional[Any] = None) -> None:
        self._image_extractor = image_extractor

    def extract(
        self,
        data: bytes,
        filename: str,
        provenance: Provenance,
    ) -> Tuple[str, List[ParsedTable]]:
        import pdfplumber

        text_parts: List[str] = []
        tables: List[ParsedTable] = []
        table_index_offset = 0

        with pdfplumber.open(io.BytesIO(data)) as pdf:
            for page_num, page in enumerate(pdf.pages):
                page_text = page.extract_text() or ""

                # ── Embedded images on this page ──────────────────────────
                if self._image_extractor:
                    for img_dict in (page.images or []):
                        description = self._describe_pdf_image(img_dict, filename, provenance)
                        if description:
                            text_parts.append(
                                f"[Image on page {page_num + 1}]: {description}"
                            )

                # ── Scanned page fallback ─────────────────────────────────
                # If pdfplumber found no text AND no embedded image descriptions
                # were already added for this page, render the whole page via vision.
                if not page_text.strip() and self._image_extractor:
                    rendered = self._render_page_vision(page, page_num, filename, provenance)
                    if rendered:
                        text_parts.append(f"[Scanned page {page_num + 1}]: {rendered}")
                elif page_text.strip():
                    text_parts.append(page_text)

                # ── Tables ────────────────────────────────────────────────
                for tbl in page.extract_tables() or []:
                    parsed = _pdfplumber_table_to_parsed(
                        tbl, table_index_offset, provenance, page_num
                    )
                    if parsed:
                        tables.append(parsed)
                        table_index_offset += 1

        return "\n".join(text_parts), tables

    # ------------------------------------------------------------------
    # Vision helpers
    # ------------------------------------------------------------------

    def _describe_pdf_image(
        self,
        img_dict: dict,
        filename: str,
        provenance: Provenance,
    ) -> str:
        """Extract and describe a single embedded PDF image."""
        img_bytes, mime_type = _pdf_image_to_bytes(img_dict)
        if not img_bytes:
            return ""
        try:
            # Reuse ImageExtractor.extract() — it returns (description, [])
            # We create a throwaway provenance since we're not building an AttachmentRef here
            description, _ = self._image_extractor.extract(
                data=img_bytes,
                filename=filename,
                provenance=provenance,
            )
            return description
        except Exception as exc:
            logger.warning("Vision failed for embedded PDF image in '%s': %s", filename, exc)
            return ""

    def _render_page_vision(
        self,
        page,
        page_num: int,
        filename: str,
        provenance: Provenance,
    ) -> str:
        """Render a full PDF page to an image and describe it via vision."""
        try:
            # pdfplumber.Page.to_image() returns a PageImage whose .original
            # is a PIL Image object.
            pil_img = page.to_image(resolution=150).original
            buf = io.BytesIO()
            pil_img.save(buf, format="PNG")
            img_bytes = buf.getvalue()
        except Exception as exc:
            logger.warning(
                "Could not render scanned page %d of '%s' for vision: %s",
                page_num + 1, filename, exc,
            )
            return ""

        try:
            description, _ = self._image_extractor.extract(
                data=img_bytes,
                filename=filename,
                provenance=provenance,
            )
            return description
        except Exception as exc:
            logger.warning(
                "Vision failed for scanned page %d of '%s': %s",
                page_num + 1, filename, exc,
            )
            return ""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _pdf_image_to_bytes(img_dict: dict) -> Tuple[Optional[bytes], str]:
    """
    Extract raw image bytes from a pdfplumber image dict.

    Returns (bytes, mime_type) for JPEG images, or (None, '') for formats
    we cannot handle directly.  Other formats are skipped to avoid sending
    raw compressed pixel data that Gemini cannot interpret.
    """
    stream = img_dict.get("stream")
    if not stream:
        return None, ""

    img_filter = img_dict.get("filter", "")
    # filter can be a list (chained filters) or a string
    if isinstance(img_filter, list):
        img_filter = img_filter[0] if img_filter else ""

    if img_filter in _JPEG_FILTERS:
        return stream, "image/jpeg"

    # For other formats, attempt PIL conversion
    try:
        from PIL import Image
        buf_in = io.BytesIO(stream)
        pil_img = Image.open(buf_in)
        buf_out = io.BytesIO()
        pil_img.save(buf_out, format="PNG")
        return buf_out.getvalue(), "image/png"
    except Exception:
        return None, ""


def _pdfplumber_table_to_parsed(
    raw: List[List],
    table_index: int,
    parent_provenance: Provenance,
    page_num: int,
) -> Optional[ParsedTable]:
    if not raw:
        return None

    chain = parent_provenance.table_chain.copy()
    chain.append(TableRef(table_index=table_index, row=None, col=None))
    table_prov = Provenance(
        page_id=parent_provenance.page_id,
        page_title=parent_provenance.page_title,
        page_date=parent_provenance.page_date,
        table_chain=chain,
        attachment_id=parent_provenance.attachment_id,
        attachment_type=parent_provenance.attachment_type,
    )

    headers: List[str] = []
    cells_matrix: List[List[ParsedCell]] = []

    for row_idx, row in enumerate(raw):
        row_cells: List[ParsedCell] = []
        for col_idx, cell_value in enumerate(row):
            text = str(cell_value).strip() if cell_value is not None else ""
            cell_prov = Provenance(
                page_id=parent_provenance.page_id,
                page_title=parent_provenance.page_title,
                page_date=parent_provenance.page_date,
                table_chain=chain.copy(),
                row=row_idx,
                col=col_idx,
                attachment_id=parent_provenance.attachment_id,
                attachment_type=parent_provenance.attachment_type,
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

        if row_idx == 0:
            headers = [
                cell.text_chunks[0].content if cell.text_chunks else ""
                for cell in row_cells
            ]
        cells_matrix.append(row_cells)

    return ParsedTable(
        table_index=table_index,
        headers=headers,
        cells=cells_matrix,
        provenance=table_prov,
        raw_html=f"[PDF page {page_num + 1} table]",
    )
