from __future__ import annotations

import logging
from contextlib import asynccontextmanager

import motor.motor_asyncio
from fastapi import FastAPI

from ..entity_extraction import ExtractionConfig
from ..graph import EmbeddingService, GraphConfig, SemanticRetriever, create_adapter
from ..graph.mongodb_adapter import MongoDBAdapter
from ..ingestion.config import IngestionConfig
from ..pipeline import build_pipeline
from .routes.ingestion import router as ingestion_router
from .routes.retrieval import router as retrieval_router

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    ingestion_cfg  = IngestionConfig()
    extraction_cfg = ExtractionConfig()
    graph_cfg      = GraphConfig()

    mongo  = motor.motor_asyncio.AsyncIOMotorClient(ingestion_cfg.mongodb_uri)
    db     = mongo[ingestion_cfg.mongodb_db]

    # Shared adapter (MongoDB) — used by both pipeline and retrieval routes
    adapter = MongoDBAdapter(ingestion_cfg.mongodb_uri, ingestion_cfg.mongodb_db)
    await adapter.ensure_indexes()

    pipeline = build_pipeline(ingestion_cfg, extraction_cfg, graph_cfg, db)

    # Retriever is optional — requires GOOGLE_API_KEY
    retriever = None
    if graph_cfg.google_api_key:
        embedder  = EmbeddingService(google_api_key=graph_cfg.google_api_key)
        retriever = SemanticRetriever(adapter=adapter, embedder=embedder)
        logger.info("SemanticRetriever enabled (text-embedding-004)")
    else:
        logger.warning("GOOGLE_API_KEY not set — /search endpoint disabled")

    app.state.pipeline  = pipeline
    app.state.adapter   = adapter
    app.state.retriever = retriever
    app.state.jobs      = {}   # job_id -> dict (in-memory, resets on restart)

    logger.info("GraphRAG API started")
    yield

    await pipeline.close()
    await adapter.close()
    mongo.close()
    logger.info("GraphRAG API shut down")


def create_app() -> FastAPI:
    app = FastAPI(
        title="Confluence GraphRAG",
        description="Ingest Confluence meeting minutes and query the temporal knowledge graph.",
        version="0.1.0",
        lifespan=lifespan,
    )

    app.include_router(ingestion_router)
    app.include_router(retrieval_router)

    @app.get("/health", tags=["meta"])
    async def health():
        return {"status": "ok"}

    return app


# Entry point for `uvicorn confluence_graphrag.api.app:app`
app = create_app()
