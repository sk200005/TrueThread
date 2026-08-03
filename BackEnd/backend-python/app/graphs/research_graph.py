"""
graphs/research_graph.py — Builds and runs the LangGraph research pipeline.

Graph structure (Milestone 2 — Wikipedia only):
    START → wikipedia_fetch → store_documents → END

Future milestones will add parallel fetch nodes for Reddit, YouTube, News
and downstream summarize/verify nodes.

Progress events are reported via BullMQ job.updateProgress() so the
Node gateway can stream them to the frontend via QueueEvents + SSE.
The event format matches docs/python-service-contract.md exactly.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from langgraph.graph import END, START, StateGraph

from app.graphs.nodes.store_node import store_documents
from app.graphs.nodes.wikipedia_node import wikipedia_fetch
from app.graphs.state import ResearchState

# @deprecated — JobState no longer used. Workers now receive BullMQ Job objects.
# from app.services.job_manager import JobState

logger = logging.getLogger(__name__)


def build_graph() -> Any:
    """Construct and compile the LangGraph StateGraph for the research pipeline."""
    graph = StateGraph(ResearchState)

    graph.add_node("wikipedia_fetch", wikipedia_fetch)
    graph.add_node("store_documents", store_documents)

    graph.add_edge(START, "wikipedia_fetch")
    graph.add_edge("wikipedia_fetch", "store_documents")
    graph.add_edge("store_documents", END)

    return graph.compile()


async def _emit(job, event: dict[str, Any]) -> None:
    """Emit a progress event via BullMQ, auto-adding jobId and timestamp.

    Mirrors the old JobState.emit_event() interface so event payloads stay
    identical to what the Node SSE layer expects.
    """
    event.setdefault("jobId", job.data.get("jobId", job.id))
    event.setdefault("timestamp", datetime.now(timezone.utc).isoformat())
    await job.updateProgress(event)


async def run_pipeline(job) -> dict:
    """
    Execute the research graph for a BullMQ job.

    Reports progress via job.updateProgress() at the same points where
    the old JobState.emit_event() was called — keeps the same event
    shape so the Node SSE layer doesn't need to change.

    Args:
        job: BullMQ Job object. job.data contains:
             { jobId, userId, queryText, sources }

    Returns:
        dict: Results including per-source status and sources_failed list.
              Stored by BullMQ as the job's return value.
    """
    job_id = job.data.get("jobId", job.id)
    query_text = job.data["queryText"]
    sources = job.data.get("sources", [])

    await _emit(job, {"type": "connected", "status": "connected"})

    try:
        compiled_graph = build_graph()

        initial_state: ResearchState = {
            "job_id": job_id,
            "query": query_text,
            "sources_to_fetch": sources,
            "raw_documents": [],
            "failed_sources": [],
            "status": "pending",
            "results": {},
        }

        # Emit: wikipedia fetch started
        await _emit(job, {"type": "progress", "source": "wikipedia", "status": "started"})

        # Run the full graph
        final_state = await compiled_graph.ainvoke(initial_state)

        # Collect results from the final state
        results = final_state.get("results", {})
        docs_count = results.get("docsInserted", 0)
        chunks_count = results.get("chunksInserted", 0)
        failed = final_state.get("failed_sources", [])

        # Emit: wikipedia fetch completed
        await _emit(job, {
            "type": "progress",
            "source": "wikipedia",
            "status": "done" if "wikipedia" not in failed else "error",
            "counts": {"docsInserted": docs_count, "chunksInserted": chunks_count},
        })

        # Build results dict
        job_results = {
            "wikipedia": {
                "status": "done" if "wikipedia" not in failed else "error",
                "docsInserted": docs_count,
                "chunksInserted": chunks_count,
            }
        }

        # Emit terminal event — Node closes the SSE stream on receiving this
        await _emit(job, {"type": "done", "status": "done", "results": job_results})

        logger.info(
            "Job %s completed: %d docs, %d chunks, failed=%s",
            job_id, docs_count, chunks_count, failed,
        )

        # Return both results and failed sources so the worker can update Postgres
        return {"results": job_results, "sources_failed": failed}

    except Exception as exc:
        error_msg = str(exc)

        # Emit terminal error event before re-raising
        await _emit(job, {
            "type": "error",
            "status": "error",
            "error": error_msg,
        })

        logger.exception("Job %s failed: %s", job_id, exc)
        raise  # Let BullMQ mark the job as failed
