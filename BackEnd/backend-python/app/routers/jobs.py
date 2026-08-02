"""
routers/jobs.py — FastAPI routes implementing the Node-Python contract.

Routes (from docs/python-service-contract.md):
    POST   /api/v1/jobs              — Submit a job
    GET    /api/v1/jobs/{jobId}/status — Poll job status
    GET    /api/v1/jobs/{jobId}/stream — SSE progress stream
    GET    /api/v1/jobs/{jobId}/result — Final result

The Node client (pythonServiceClient.js) is already built against these
exact paths and response shapes. Do NOT change without updating the contract.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, HTTPException
from starlette.responses import StreamingResponse

from app.schemas.job import (
    JobAcceptedResponse,
    JobResultResponse,
    JobStatusResponse,
    JobSubmitRequest,
)
from app.services.job_manager import create_job, get_job

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/jobs", tags=["jobs"])


@router.post("", status_code=202, response_model=JobAcceptedResponse)
async def submit_job(payload: JobSubmitRequest, background_tasks: BackgroundTasks):
    """
    Accept a job from the Node gateway and launch the LangGraph pipeline
    as a background task. Returns 202 immediately.
    """
    sources = payload.sources or ["wikipedia"]

    # Guard against duplicate job IDs
    if get_job(payload.jobId):
        raise HTTPException(status_code=409, detail="Job already exists")

    job = create_job(payload.jobId, payload.userId, payload.queryText, sources)

    # Import here to avoid circular imports — graph module is heavy
    from app.graphs.research_graph import run_pipeline

    background_tasks.add_task(run_pipeline, job)

    return JobAcceptedResponse(jobId=payload.jobId)


@router.post("/query", status_code=202, response_model=JobAcceptedResponse)
async def submit_query(payload: JobSubmitRequest, background_tasks: BackgroundTasks):
    """
    Submit a query-time analysis job.

    Unlike submit_job (which runs the INGESTION pipeline to fetch + store data),
    this endpoint runs the QUERY pipeline:
        rag_retrieve → extract_claims → summarize

    It searches against already-ingested data in document_chunks and produces
    a structured report. Use this AFTER data has been ingested.
    """
    # Guard against duplicate job IDs
    if get_job(payload.jobId):
        raise HTTPException(status_code=409, detail="Job already exists")

    job = create_job(payload.jobId, payload.userId, payload.queryText, payload.sources or [])

    # Import here to avoid circular imports — graph module is heavy
    from app.graphs.query_graph import run_query_pipeline

    background_tasks.add_task(run_query_pipeline, job)

    return JobAcceptedResponse(jobId=payload.jobId)


@router.get("/{job_id}/status", response_model=JobStatusResponse)
async def job_status(job_id: str):
    """Poll the current status of a job."""
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    return JobStatusResponse(
        jobId=job.job_id,
        status=job.status,
        sources_failed=job.sources_failed or None,
        results=job.results,
    )


@router.get("/{job_id}/stream")
async def stream_job_progress(job_id: str):
    """
    SSE endpoint — streams progress events for a job.

    The Node parser (pythonServiceClient.js L70-87) expects each chunk as:
        data: <JSON>\\n\\n

    It splits on '\\n', checks line.startsWith('data: '), then JSON.parse(line.slice(6)).
    Terminal events (type="done" or type="error") cause Node to destroy the stream.
    """
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    async def event_generator():
        # Replay any events that already fired before this client connected
        for event in list(job.events):
            yield f"data: {json.dumps(event)}\n\n"
            # If we replayed a terminal event, stop immediately
            if event.get("type") in ("done", "error"):
                return

        # Stream new events as they arrive
        while True:
            try:
                event = await asyncio.wait_for(job.event_queue.get(), timeout=60.0)
                yield f"data: {json.dumps(event)}\n\n"
                if event.get("type") in ("done", "error"):
                    return
            except asyncio.TimeoutError:
                # Send a keep-alive comment to prevent proxy/client timeout
                yield ": keepalive\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/{job_id}/result", response_model=JobResultResponse)
async def job_result(job_id: str):
    """Return the final report for a completed job."""
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    if job.status != "done" or job.results is None:
        raise HTTPException(status_code=404, detail="Job not found or not yet complete")

    return JobResultResponse(jobId=job.job_id, report=job.results)
