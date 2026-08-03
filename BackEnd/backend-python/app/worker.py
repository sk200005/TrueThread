"""
worker.py — BullMQ workers for the research and query job queues.

Creates two BullMQ Workers that consume jobs from Redis:
  - 'research' queue → runs the ingestion pipeline (research_graph.py)
  - 'query' queue → runs the query-time pipeline (query_graph.py)

Workers are started via FastAPI's lifespan hook in main.py, so they
run in the same process as the /health endpoint. This keeps things
simple: one process, one event loop, liveness probes still work.

Usage:
    Workers start automatically when the FastAPI app boots.
    No separate process or CLI needed.
"""

from __future__ import annotations

import logging

import asyncpg
from bullmq import Worker

from app.core.config import settings

logger = logging.getLogger(__name__)

_workers: list[Worker] = []


# ── Redis connection config ──────────────────────────────────────────────

def _redis_opts() -> dict:
    """Return the BullMQ connection options dict."""
    return {
        "connection": {
            "host": settings.redis_host,
            "port": settings.redis_port,
        }
    }


# ── Postgres helpers ─────────────────────────────────────────────────────
# The worker updates the `queries` table directly so that Node's
# getJobStatus endpoint (which reads from Postgres) reflects the real
# status. The old REST-based flow never did this — it's a fix.

async def _update_query_status(
    job_id: str,
    status: str,
    *,
    sources_failed: list[str] | None = None,
) -> None:
    """Best-effort update of the queries row in Postgres."""
    try:
        dsn = settings.database_url.replace("+asyncpg", "")
        conn = await asyncpg.connect(dsn)
        try:
            if status == "done":
                await conn.execute(
                    "UPDATE queries SET status = $1, completed_at = now(), "
                    "sources_failed = $2 WHERE id = $3",
                    status, sources_failed, job_id,
                )
            elif status == "error":
                await conn.execute(
                    "UPDATE queries SET status = $1 WHERE id = $2",
                    status, job_id,
                )
            else:
                # "running", etc.
                await conn.execute(
                    "UPDATE queries SET status = $1 WHERE id = $2",
                    status, job_id,
                )
        finally:
            await conn.close()
    except Exception as e:
        # Non-fatal — the job can still complete even if the DB update fails.
        logger.warning("Failed to update DB status for job %s: %s", job_id, e)


# ── Job processors ───────────────────────────────────────────────────────

async def process_research_job(job, token):
    """Process an ingestion job from the 'research' queue."""
    from app.graphs.research_graph import run_pipeline

    job_id = job.data.get("jobId", job.id)
    logger.info("Processing research job %s", job_id)

    await _update_query_status(job_id, "running")
    try:
        result = await run_pipeline(job)
        sources_failed = result.get("sources_failed", [])
        await _update_query_status(
            job_id, "done", sources_failed=sources_failed or None,
        )
        return result
    except Exception:
        await _update_query_status(job_id, "error")
        raise


async def process_query_job(job, token):
    """Process a query-time analysis job from the 'query' queue."""
    from app.graphs.query_graph import run_query_pipeline

    job_id = job.data.get("jobId", job.id)
    logger.info("Processing query job %s", job_id)

    await _update_query_status(job_id, "running")
    try:
        result = await run_query_pipeline(job)
        await _update_query_status(job_id, "done")
        return result
    except Exception:
        await _update_query_status(job_id, "error")
        raise


# ── Lifecycle ─────────────────────────────────────────────────────────────

async def start_workers() -> None:
    """Create and start BullMQ workers for both queues."""
    opts = _redis_opts()

    research_worker = Worker("research", process_research_job, opts)
    query_worker = Worker("query", process_query_job, opts)

    _workers.extend([research_worker, query_worker])

    logger.info(
        "BullMQ workers started (Redis %s:%s) — listening on queues: research, query",
        settings.redis_host,
        settings.redis_port,
    )


async def stop_workers() -> None:
    """Gracefully shut down all workers."""
    for w in _workers:
        await w.close()
    _workers.clear()
    logger.info("BullMQ workers stopped")
