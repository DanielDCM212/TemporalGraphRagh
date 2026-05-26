from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime

from fastapi import APIRouter, HTTPException, status

from ...ingestion.batch_ingestor import BatchIngestor
from ...ingestion.confluence_client import ConfluenceClient
from ...ingestion.config import IngestionConfig
from ...ingestion.ingestion_log import IngestionLog
from ...ingestion.models import IngestionStatus
from ..deps import AdapterDep, JobsDep, PipelineDep
from ..schemas import (
    IngestionLogResponse,
    JobResponse,
    PageIngestRequest,
    SpaceIngestRequest,
)

router = APIRouter(prefix="/ingest", tags=["ingestion"])
logger = logging.getLogger(__name__)


# ── Background job runner ─────────────────────────────────────────────────────

async def _run_batch(
    jobs: dict,
    job_id: str,
    request: SpaceIngestRequest | PageIngestRequest,
    pipeline,
) -> None:
    cfg    = IngestionConfig()
    client = ConfluenceClient(cfg)
    log    = IngestionLog(cfg)
    stats  = {"total": 0, "ok": 0, "errors": 0, "skipped": 0}

    try:
        ingestor = BatchIngestor(cfg, client, log)

        if isinstance(request, PageIngestRequest):
            await ingestor.run_pages(
                space_key=request.space_key,
                pipeline=pipeline,
                page_ids=request.page_ids,
            )
        else:
            await ingestor.run(
                space_key=request.space_key,
                pipeline=pipeline,
                start_date=request.start_date,
                end_date=request.end_date,
            )

        jobs[job_id].update(
            status="done",
            finished_at=datetime.utcnow(),
            stats=stats,
        )
    except Exception as exc:
        logger.error("Ingestion job %s failed: %s", job_id, exc, exc_info=True)
        jobs[job_id].update(
            status="error",
            finished_at=datetime.utcnow(),
            error=str(exc),
        )
    finally:
        await client.close()


# ── Routes ────────────────────────────────────────────────────────────────────

@router.post("/space", response_model=JobResponse, status_code=status.HTTP_202_ACCEPTED)
async def ingest_space(
    req: SpaceIngestRequest,
    pipeline: PipelineDep,
    jobs: JobsDep,
) -> JobResponse:
    """Trigger batch ingestion for an entire Confluence space (runs in background)."""
    job_id = str(uuid.uuid4())
    entry  = {
        "job_id": job_id,
        "status": "running",
        "started_at": datetime.utcnow(),
        "finished_at": None,
        "stats": None,
        "error": None,
    }
    jobs[job_id] = entry
    asyncio.create_task(_run_batch(jobs, job_id, req, pipeline))
    return JobResponse(**entry)


@router.post("/pages", response_model=JobResponse, status_code=status.HTTP_202_ACCEPTED)
async def ingest_pages(
    req: PageIngestRequest,
    pipeline: PipelineDep,
    jobs: JobsDep,
) -> JobResponse:
    """Trigger ingestion for a specific list of Confluence page IDs (runs in background)."""
    job_id = str(uuid.uuid4())
    entry  = {
        "job_id": job_id,
        "status": "running",
        "started_at": datetime.utcnow(),
        "finished_at": None,
        "stats": None,
        "error": None,
    }
    jobs[job_id] = entry
    asyncio.create_task(_run_batch(jobs, job_id, req, pipeline))
    return JobResponse(**entry)


@router.get("/jobs/{job_id}", response_model=JobResponse)
async def get_job(job_id: str, jobs: JobsDep) -> JobResponse:
    """Poll the status of a running or completed ingestion job."""
    entry = jobs.get(job_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")
    return JobResponse(**entry)


@router.get("/jobs", response_model=list[JobResponse])
async def list_jobs(jobs: JobsDep) -> list[JobResponse]:
    """List all ingestion jobs (in-memory, resets on server restart)."""
    return [JobResponse(**v) for v in jobs.values()]


@router.delete("/page/{page_id}", status_code=status.HTTP_204_NO_CONTENT)
async def soft_delete_page(page_id: str, pipeline: PipelineDep) -> None:
    """Soft-delete all graph nodes owned by a page."""
    await pipeline.soft_delete_page(page_id)


@router.get("/log/{page_id}", response_model=IngestionLogResponse)
async def get_log_entry(page_id: str) -> IngestionLogResponse:
    """Get the ingestion log entry for a single page."""
    cfg = IngestionConfig()
    log = IngestionLog(cfg)
    entry = await log.get(page_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"No log entry for page '{page_id}'")
    return IngestionLogResponse(
        page_id=entry.page_id,
        page_title=entry.page_title,
        page_date=entry.page_date,
        space_key=entry.space_key,
        status=entry.status,
        processed_at=entry.processed_at,
        attachment_count=entry.attachment_count,
        retry_count=entry.retry_count,
        error_message=entry.error_message,
    )


@router.get("/errors", response_model=list[str])
async def list_errors(max_retry: int = 3) -> list[str]:
    """List page IDs that failed ingestion and are below the retry limit."""
    cfg = IngestionConfig()
    log = IngestionLog(cfg)
    return await log.list_errors(max_retry=max_retry)
