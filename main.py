"""
Confluence GraphRAG — full pipeline runner (Stage 1 → Stage 5).

Usage:
    # Historical batch ingestion
    python main.py --mode batch --space MY_SPACE

    # With date range
    python main.py --mode batch --space MY_SPACE --start 2022-01-01 --end 2024-12-31

    # Incremental watcher (runs until Ctrl-C, polls every 24 h)
    python main.py --mode incremental --space MY_SPACE

All configuration is read from .env (see .env.example).
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import signal
from datetime import datetime
from typing import List, Optional

import motor.motor_asyncio

from confluence_graphrag.entity_extraction import ExtractionConfig, PageEntityExtractor
from confluence_graphrag.graph import GraphConfig, TemporalGraphBuilder, create_adapter
from confluence_graphrag.ingestion.batch_ingestor import BatchIngestor
from confluence_graphrag.ingestion.confluence_client import ConfluenceClient
from confluence_graphrag.ingestion.config import IngestionConfig
from confluence_graphrag.ingestion.incremental_watcher import IncrementalWatcher
from confluence_graphrag.ingestion.models import PageMetadata, PagePipeline
from confluence_graphrag.ingestion.upsert_handler import UpsertHandler
from confluence_graphrag.ingestion.ingestion_log import IngestionLog
from confluence_graphrag.parser.html_parser import HTMLContentParser
from confluence_graphrag.table_normalization.classifier import TableClassifier
from confluence_graphrag.table_normalization.normalizer import IncrementalNormalizer
from confluence_graphrag.table_normalization.schema_store import CanonicalSchemaStore

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Full pipeline — implements the PagePipeline protocol
# ---------------------------------------------------------------------------

class FullPagePipeline:
    """
    Wires Stage 2 → 3B → 4 → 5 into a single ingest() call.

    Stage 1 (BatchIngestor / IncrementalWatcher) calls ingest() for each page.

    Stage 3B note: IncrementalNormalizer is used for both batch and incremental
    modes.  It creates AUTO_APPROVED schemas on first encounter of a new table
    type.  If you need the two-step PENDING_APPROVAL workflow for historical
    data, run BatchNormalizer.prepare_schemas() separately before ingestion.
    """

    def __init__(
        self,
        normalizer: IncrementalNormalizer,
        extractor: PageEntityExtractor,
        builder: TemporalGraphBuilder,
    ) -> None:
        self._normalizer = normalizer
        self._extractor  = extractor
        self._builder    = builder

    async def ingest(self, metadata: PageMetadata) -> None:
        # Stage 2 — HTML → ContentTree
        parser = HTMLContentParser(metadata.page_id, metadata.title)
        content_tree = parser.parse(metadata.html_content)

        # Stage 3B — normalize each table (returns NormalizedRow list or None)
        for table in content_tree.tables:
            await self._normalizer.normalize_table(table)

        # Stage 4 — entity extraction → EntitySet
        entity_set = await self._extractor.extract(content_tree)

        # Stage 5 — build / update temporal graph
        await self._builder.ingest_page(entity_set, content_tree)

    async def soft_delete_page(self, page_id: str) -> None:
        await self._builder._adapter.soft_delete_page(page_id)


# ---------------------------------------------------------------------------
# Dependency wiring
# ---------------------------------------------------------------------------

def build_pipeline(
    ingestion_cfg: IngestionConfig,
    extraction_cfg: ExtractionConfig,
    graph_cfg: GraphConfig,
    db: motor.motor_asyncio.AsyncIOMotorDatabase,
) -> FullPagePipeline:
    normalizer = IncrementalNormalizer(
        config=ingestion_cfg,
        classifier=TableClassifier(google_api_key=graph_cfg.google_api_key),
        store=CanonicalSchemaStore(ingestion_cfg),
    )

    extractor = PageEntityExtractor(config=extraction_cfg, db=db)

    adapter = create_adapter(graph_cfg)
    builder = TemporalGraphBuilder(adapter=adapter, config=graph_cfg)

    return FullPagePipeline(normalizer, extractor, builder)


# ---------------------------------------------------------------------------
# Entrypoints
# ---------------------------------------------------------------------------

async def run_batch(
    space_key: str,
    start_date: Optional[datetime],
    end_date: Optional[datetime],
    page_ids: Optional[List[str]] = None,
) -> None:
    ingestion_cfg  = IngestionConfig()
    extraction_cfg = ExtractionConfig()
    graph_cfg      = GraphConfig()

    mongo = motor.motor_asyncio.AsyncIOMotorClient(ingestion_cfg.mongodb_uri)
    db    = mongo[ingestion_cfg.mongodb_db]

    client   = ConfluenceClient(ingestion_cfg)
    log      = IngestionLog(ingestion_cfg)
    pipeline = build_pipeline(ingestion_cfg, extraction_cfg, graph_cfg, db)
    ingestor = BatchIngestor(ingestion_cfg, client, log)

    if page_ids:
        await ingestor.run_pages(space_key=space_key, pipeline=pipeline, page_ids=page_ids)
    else:
        await ingestor.run(
            space_key=space_key,
            pipeline=pipeline,
            start_date=start_date,
            end_date=end_date,
        )

    await pipeline._extractor.close()
    await pipeline._builder.close()
    await client.close()
    mongo.close()
    logger.info("Batch ingestion complete.")


async def run_incremental(space_key: str) -> None:
    ingestion_cfg  = IngestionConfig()
    extraction_cfg = ExtractionConfig()
    graph_cfg      = GraphConfig()

    mongo = motor.motor_asyncio.AsyncIOMotorClient(ingestion_cfg.mongodb_uri)
    db    = mongo[ingestion_cfg.mongodb_db]

    client         = ConfluenceClient(ingestion_cfg)
    log            = IngestionLog(ingestion_cfg)
    pipeline       = build_pipeline(ingestion_cfg, extraction_cfg, graph_cfg, db)
    upsert_handler = UpsertHandler(ingestion_cfg, client, log)
    watcher        = IncrementalWatcher(ingestion_cfg, client, log, upsert_handler)

    watcher.start(space_key, pipeline)

    # Wait until SIGINT / SIGTERM
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_event.set)

    logger.info("Incremental watcher running — press Ctrl-C to stop")
    await stop_event.wait()

    watcher.stop()
    await pipeline._extractor.close()
    await pipeline._builder.close()
    await client.close()
    mongo.close()
    logger.info("Incremental watcher stopped.")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_date(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d")


def main() -> None:
    parser = argparse.ArgumentParser(description="Confluence GraphRAG pipeline runner")
    parser.add_argument(
        "--mode", choices=["batch", "incremental"], required=True,
        help="batch: historical one-shot; incremental: continuous polling",
    )
    parser.add_argument("--space", required=True, help="Confluence space key")
    parser.add_argument("--start", type=_parse_date, metavar="YYYY-MM-DD",
                        help="[batch only] ingest pages on or after this date")
    parser.add_argument("--end", type=_parse_date, metavar="YYYY-MM-DD",
                        help="[batch only] ingest pages on or before this date")
    parser.add_argument("--pages", metavar="ID1,ID2,...",
                        help="[batch only] comma-separated Confluence page IDs to ingest; "
                             "skips full space iteration")
    args = parser.parse_args()

    page_ids: Optional[List[str]] = (
        [p.strip() for p in args.pages.split(",") if p.strip()]
        if args.pages else None
    )

    if args.mode == "batch":
        asyncio.run(run_batch(args.space, args.start, args.end, page_ids))
    else:
        if page_ids:
            parser.error("--pages is only supported with --mode batch")
        asyncio.run(run_incremental(args.space))


if __name__ == "__main__":
    main()
    # asyncio.run(run_batch("space"))