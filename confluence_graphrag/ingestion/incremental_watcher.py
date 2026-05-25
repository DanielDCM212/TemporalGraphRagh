from __future__ import annotations

import logging
from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from .config import IngestionConfig
from .confluence_client import ConfluenceClient
from .ingestion_log import IngestionLog
from .models import PagePipeline
from .upsert_handler import UpsertHandler

logger = logging.getLogger(__name__)


class IncrementalWatcher:
    """
    Polls Confluence every N hours (D5: 24h, configurable via
    INCREMENTAL_POLL_INTERVAL_HOURS) and upserts any page whose
    Confluence last_modified timestamp is newer than ingestion_log.

    Uses APScheduler with the asyncio backend — compatible with
    the FastAPI event loop used in Stage 6.
    """

    def __init__(
        self,
        config: IngestionConfig,
        client: ConfluenceClient,
        log: IngestionLog,
        upsert_handler: UpsertHandler,
    ) -> None:
        self._config = config
        self._client = client
        self._log = log
        self._upsert_handler = upsert_handler
        self._scheduler = AsyncIOScheduler()

    def start(self, space_key: str, pipeline: PagePipeline) -> None:
        hours = self._config.incremental_poll_interval_hours

        self._scheduler.add_job(
            self._poll,
            trigger='interval',
            hours=hours,
            args=[space_key, pipeline],
            id='incremental_poll',
            replace_existing=True,
            next_run_time=datetime.now(),   # run one poll immediately on startup
        )
        self._scheduler.start()
        logger.info(
            "Incremental watcher started — space='%s', interval=%dh",
            space_key, hours,
        )

    def stop(self) -> None:
        self._scheduler.shutdown(wait=False)
        logger.info("Incremental watcher stopped")

    async def _poll(self, space_key: str, pipeline: PagePipeline) -> None:
        logger.info("Polling Confluence for changes in space '%s'", space_key)
        changed = errors = 0

        async for metadata in self._client.iter_pages(space_key):
            if not await self._log.needs_upsert(metadata.page_id, metadata.last_modified):
                continue
            try:
                await self._upsert_handler.process(metadata.page_id, pipeline)
                changed += 1
            except Exception as exc:
                errors += 1
                logger.error(
                    "Poll upsert failed for '%s': %s", metadata.page_id, exc
                )

        logger.info(
            "Poll complete — space='%s' changed=%d errors=%d",
            space_key, changed, errors,
        )
