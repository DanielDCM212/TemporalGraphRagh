from __future__ import annotations

from typing import Annotated

from fastapi import Depends, HTTPException, Request, status

from ..graph import EmbeddingService, SemanticRetriever
from ..graph.mongodb_adapter import MongoDBAdapter
from ..pipeline import FullPagePipeline


def _get_pipeline(request: Request) -> FullPagePipeline:
    return request.app.state.pipeline


def _get_adapter(request: Request) -> MongoDBAdapter:
    return request.app.state.adapter


def _get_retriever(request: Request) -> SemanticRetriever:
    retriever: SemanticRetriever | None = request.app.state.retriever
    if retriever is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Retriever not available — set GOOGLE_API_KEY to enable embeddings",
        )
    return retriever


def _get_jobs(request: Request) -> dict:
    return request.app.state.jobs


PipelineDep  = Annotated[FullPagePipeline,   Depends(_get_pipeline)]
AdapterDep   = Annotated[MongoDBAdapter,      Depends(_get_adapter)]
RetrieverDep = Annotated[SemanticRetriever,   Depends(_get_retriever)]
JobsDep      = Annotated[dict,                Depends(_get_jobs)]
