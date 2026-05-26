from __future__ import annotations

import logging
from datetime import datetime
from typing import List, Optional

from .confluence_client import ConfluenceClient
from .config import IngestionConfig
from .ingestion_log import IngestionLog
from .models import IngestionLogEntry, IngestionStatus, PagePipeline

logger = logging.getLogger(__name__)


class BatchIngestor:
    """
    One-shot historical ingestion of all pages in a Confluence space.

    Pages are sorted oldest-first (by date in title) so that Graphiti builds
    the temporal graph in chronological order.  Already-ingested pages are
    skipped, making repeated runs safe (idempotent).
    """

    def __init__(
        self,
        config: IngestionConfig,
        client: ConfluenceClient,
        log: IngestionLog,
    ) -> None:
        self._config = config
        self._client = client
        self._log = log

    async def run(
        self,
        space_key: str,
        pipeline: PagePipeline,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
    ) -> None:
        logger.info(
            "Batch ingestion started — space='%s' start=%s end=%s",
            space_key, start_date, end_date,
        )
        total = ok = errors = skipped = 0

        async for metadata in self._client.iter_pages(space_key, start_date, end_date):
            total += 1

            # Idempotency: skip pages that are already done and unchanged
            if not await self._log.needs_upsert(metadata.page_id, metadata.last_modified):
                logger.debug("Skipping '%s' (already ingested, unchanged)", metadata.title)
                skipped += 1
                continue

            # Flag pages whose title has no parseable date
            if metadata.page_date == datetime.min:
                await self._log.mark_needs_review(
                    metadata.page_id,
                    reason="No parseable date in title",
                )
                logger.warning("NEEDS_REVIEW '%s' — no date in title", metadata.title)
                continue

            # Fetch attachments (non-blocking on failure)
            try:
                metadata.attachments = await self._client.get_attachments(metadata.page_id)
            except Exception as exc:
                logger.warning(
                    "Could not fetch attachments for '%s': %s", metadata.page_id, exc
                )

            # Write log entry before calling the pipeline
            await self._log.upsert(IngestionLogEntry(
                page_id=metadata.page_id,
                page_title=metadata.title,
                page_date=metadata.page_date,
                space_key=space_key,
                confluence_last_modified=metadata.last_modified,
                processed_at=datetime.utcnow(),
                status=IngestionStatus.PROCESSING,
                attachment_count=len(metadata.attachments),
            ))

            try:
                await pipeline.ingest(metadata)
                await self._log.mark_done(metadata.page_id, len(metadata.attachments))
                ok += 1
                logger.info("[%d] Done: '%s'", total, metadata.title)
            except Exception as exc:
                await self._log.mark_error(metadata.page_id, str(exc))
                errors += 1
                logger.error(
                    "Error ingesting '%s': %s", metadata.title, exc, exc_info=True
                )

        logger.info(
            "Batch ingestion complete — space='%s' total=%d ok=%d errors=%d skipped=%d",
            space_key, total, ok, errors, skipped,
        )

    async def run_pages(
        self,
        space_key: str,
        pipeline: PagePipeline,
        page_ids: List[str],
    ) -> None:
        """
        Ingest a specific list of page IDs from a Confluence space.

        Pages are fetched individually, then sorted oldest-first (same as run())
        so Graphiti builds the timeline in chronological order.
        Idempotency, date-missing, and error handling follow the same rules as run().
        """
        logger.info(
            "Page-list ingestion started — space='%s' pages=%d",
            space_key, len(page_ids),
        )
        total = ok = errors = skipped = 0

        # Fetch all requested pages first so we can sort chronologically
        pages = []
        for page_id in page_ids:
            try:
                metadata = await self._client.get_page(page_id)
                # space_key may be empty when fetched by ID; fill it in
                if not metadata.space_key:
                    metadata.space_key = space_key
                pages.append(metadata)
            except Exception as exc:
                logger.error("Could not fetch page '%s': %s", page_id, exc)
                errors += 1

        # Oldest-first so temporal graph is built in chronological order
        pages.sort(key=lambda p: p.page_date if p.page_date != datetime.min else datetime.max)

        for metadata in pages:
            total += 1

            if not await self._log.needs_upsert(metadata.page_id, metadata.last_modified):
                logger.debug("Skipping '%s' (already ingested, unchanged)", metadata.title)
                skipped += 1
                continue

            if metadata.page_date == datetime.min:
                await self._log.mark_needs_review(
                    metadata.page_id,
                    reason="No parseable date in title",
                )
                logger.warning("NEEDS_REVIEW '%s' — no date in title", metadata.title)
                continue

            try:
                metadata.attachments = await self._client.get_attachments(metadata.page_id)
            except Exception as exc:
                logger.warning(
                    "Could not fetch attachments for '%s': %s", metadata.page_id, exc
                )

            await self._log.upsert(IngestionLogEntry(
                page_id=metadata.page_id,
                page_title=metadata.title,
                page_date=metadata.page_date,
                space_key=space_key,
                confluence_last_modified=metadata.last_modified,
                processed_at=datetime.utcnow(),
                status=IngestionStatus.PROCESSING,
                attachment_count=len(metadata.attachments),
            ))

            try:
                await pipeline.ingest(metadata)
                await self._log.mark_done(metadata.page_id, len(metadata.attachments))
                ok += 1
                logger.info("[%d] Done: '%s'", total, metadata.title)
            except Exception as exc:
                await self._log.mark_error(metadata.page_id, str(exc))
                errors += 1
                logger.error(
                    "Error ingesting '%s': %s", metadata.title, exc, exc_info=True
                )

        logger.info(
            "Page-list ingestion complete — space='%s' total=%d ok=%d errors=%d skipped=%d",
            space_key, total, ok, errors, skipped,
        )
