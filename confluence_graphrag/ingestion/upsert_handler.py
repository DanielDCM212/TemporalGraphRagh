from __future__ import annotations

import logging

from .confluence_client import ConfluenceClient
from .config import IngestionConfig
from .ingestion_log import IngestionLog
from .models import PagePipeline

logger = logging.getLogger(__name__)


class UpsertHandler:
    """
    Re-ingests a single page that has been updated in Confluence.

    Flow (doc 04):
      1. Mark log entry as PROCESSING
      2. Soft-delete the page's subgraph in the graph store
      3. Fetch fresh content + attachments from Confluence
      4. Run the full pipeline on the new content
      5. Mark log entry as DONE (or ERROR on failure)
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

    async def process(self, page_id: str, pipeline: PagePipeline) -> None:
        logger.info("Upsert started for page '%s'", page_id)
        await self._log.mark_processing(page_id)

        try:
            metadata = await self._client.get_page(page_id)
            metadata.attachments = await self._client.get_attachments(page_id)

            # Remove the old subgraph before writing the new one
            await pipeline.soft_delete_page(page_id)

            await pipeline.ingest(metadata)
            await self._log.mark_done(page_id, len(metadata.attachments))
            logger.info(
                "Upsert complete for page '%s' ('%s')", page_id, metadata.title
            )
        except Exception as exc:
            await self._log.mark_error(page_id, str(exc))
            logger.error(
                "Upsert failed for page '%s': %s", page_id, exc, exc_info=True
            )
            raise
