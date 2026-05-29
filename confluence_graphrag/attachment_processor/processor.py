from __future__ import annotations

import asyncio
import logging
from typing import List, Optional, Tuple

from ..ingestion.confluence_client import ConfluenceClient
from ..ingestion.models import AttachmentMetadata
from ..parser.models import AttachmentRef, AttachmentType, Provenance
from .config import AttachmentConfig
from .extractors.docx import DocxExtractor
from .extractors.excel import ExcelExtractor
from .extractors.image import ImageExtractor
from .extractors.pdf import PdfExtractor
from .extractors.pptx import PptxExtractor
from .models import ExtractedAttachment

logger = logging.getLogger(__name__)

# Attachment types this processor handles
_SUPPORTED_TYPES = {
    AttachmentType.PDF,
    AttachmentType.EXCEL,
    AttachmentType.DOCX,
    AttachmentType.PPTX,
    AttachmentType.IMAGE,
}


class AttachmentProcessor:
    """
    Stage 3A — downloads and extracts content from page and table-level
    attachments.

    Each call to *process()* receives a reconciled list of
    (AttachmentRef, AttachmentMetadata | None) pairs.  AttachmentRef carries
    the full provenance (table_chain / row / col if the file lives inside a
    table cell).  AttachmentMetadata supplies the real download_url and
    size_bytes.

    Extraction runs concurrently under a semaphore; per-file failures are
    caught and surfaced as ExtractedAttachment.error so the pipeline never
    blocks on a bad attachment.
    """

    def __init__(
        self,
        client: ConfluenceClient,
        config: Optional[AttachmentConfig] = None,
    ) -> None:
        self._client = client
        self._config = config or AttachmentConfig()
        self._semaphore = asyncio.Semaphore(self._config.attachment_concurrency)

        # Build vision extractor first — shared by PDF, PPTX, and standalone images
        self._image_extractor = ImageExtractor(
            model=self._config.vision_model,
            gcp_project=self._config.gcp_project,
            gcp_location=self._config.gcp_location,
        ) if self._config.enable_vision else None

        # Build file extractors; PDF and PPTX receive the vision extractor so
        # they can process embedded images on pages/slides internally.
        self._pdf_extractor   = PdfExtractor(image_extractor=self._image_extractor)
        self._excel_extractor = ExcelExtractor()
        self._docx_extractor  = DocxExtractor()
        self._pptx_extractor  = PptxExtractor(image_extractor=self._image_extractor)

    async def process(
        self,
        refs: List[Tuple[AttachmentRef, Optional[AttachmentMetadata]]],
    ) -> List[ExtractedAttachment]:
        """
        Process all attachment refs concurrently.

        *refs* is a list of (AttachmentRef, AttachmentMetadata | None).
        AttachmentRef carries the location inside the page; AttachmentMetadata
        supplies the download URL and size.  Both come from the pipeline wiring
        that reconciles the parsed ContentTree with metadata.attachments.
        """
        tasks = [self._process_one(ref, meta) for ref, meta in refs]
        results = await asyncio.gather(*tasks, return_exceptions=False)
        return [r for r in results if r is not None]

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _process_one(
        self,
        ref: AttachmentRef,
        meta: Optional[AttachmentMetadata],
    ) -> Optional[ExtractedAttachment]:
        att_type = ref.attachment_type

        if att_type not in _SUPPORTED_TYPES:
            logger.debug("Skipping unsupported attachment type '%s': %s", att_type, ref.filename)
            return None

        if att_type == AttachmentType.IMAGE and not self._image_extractor:
            logger.debug("Vision disabled, skipping image: %s", ref.filename)
            return None

        # Resolve download URL: prefer the API metadata, fall back to the
        # parsed href (works for direct-link attachments)
        download_url = (meta.download_url if meta else None) or ref.url
        if not download_url:
            logger.warning("No download URL for '%s', skipping", ref.filename)
            return None

        # Guard against oversized files
        if meta and meta.size_bytes > self._config.max_attachment_size_bytes:
            logger.warning(
                "Skipping '%s' — size %d bytes exceeds limit %d",
                ref.filename, meta.size_bytes, self._config.max_attachment_size_bytes,
            )
            return None

        # Derive the full provenance for this attachment
        attachment_id = meta.attachment_id if meta else ref.attachment_id
        prov = Provenance(
            page_id=ref.provenance.page_id,
            page_title=ref.provenance.page_title,
            page_date=ref.provenance.page_date,
            table_chain=ref.provenance.table_chain.copy(),
            row=ref.provenance.row,
            col=ref.provenance.col,
            attachment_id=attachment_id,
            attachment_type=att_type,
        )

        async with self._semaphore:
            try:
                data = await self._client.download_attachment(download_url)
            except Exception as exc:
                logger.error("Download failed for '%s': %s", ref.filename, exc)
                return ExtractedAttachment(
                    attachment_id=attachment_id,
                    filename=ref.filename,
                    attachment_type=att_type,
                    provenance=prov,
                    error=f"download_failed: {exc}",
                )

            try:
                text, tables, source = await self._run_extractor(data, ref.filename, att_type, prov)
            except Exception as exc:
                logger.error("Extraction failed for '%s': %s", ref.filename, exc)
                return ExtractedAttachment(
                    attachment_id=attachment_id,
                    filename=ref.filename,
                    attachment_type=att_type,
                    provenance=prov,
                    error=f"extraction_failed: {exc}",
                )

        logger.debug(
            "Extracted '%s' — %d chars, %d tables (source=%s)",
            ref.filename, len(text), len(tables), source,
        )
        return ExtractedAttachment(
            attachment_id=attachment_id,
            filename=ref.filename,
            attachment_type=att_type,
            provenance=prov,
            text=text,
            tables=tables,
            source=source,
        )

    async def _run_extractor(
        self,
        data: bytes,
        filename: str,
        att_type: AttachmentType,
        prov: Provenance,
    ) -> Tuple[str, list, str]:
        """Dispatch to the right extractor and run it in a thread."""
        if att_type == AttachmentType.PDF:
            extractor = self._pdf_extractor
            source = "text"
        elif att_type == AttachmentType.EXCEL:
            extractor = self._excel_extractor
            source = "text"
        elif att_type == AttachmentType.DOCX:
            extractor = self._docx_extractor
            source = "text"
        elif att_type == AttachmentType.PPTX:
            extractor = self._pptx_extractor
            source = "text"
        elif att_type == AttachmentType.IMAGE:
            extractor = self._image_extractor
            source = "vision"
        else:
            return "", [], "text"

        text, tables = await asyncio.to_thread(extractor.extract, data, filename, prov)
        return text, tables, source
