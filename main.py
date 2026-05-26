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

from confluence_graphrag.entity_extraction import ExtractionConfig
from confluence_graphrag.graph import GraphConfig
from confluence_graphrag.ingestion.batch_ingestor import BatchIngestor
from confluence_graphrag.ingestion.confluence_client import ConfluenceClient
from confluence_graphrag.ingestion.config import IngestionConfig
from confluence_graphrag.ingestion.incremental_watcher import IncrementalWatcher
from confluence_graphrag.ingestion.models import PagePipeline
from confluence_graphrag.ingestion.upsert_handler import UpsertHandler
from confluence_graphrag.ingestion.ingestion_log import IngestionLog
from confluence_graphrag.pipeline import FullPagePipeline, build_pipeline

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


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

    await pipeline.close()
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
    await pipeline.close()
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