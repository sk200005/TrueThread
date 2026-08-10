"""
graphs/research_graph.py — Builds and runs the LangGraph research pipeline.

Graph structure (Milestone 4 — Parallel Fetch):
    START → wikipedia_fetch ──────┐
    START → reddit_fetch    ──────┼→ store_documents → END
    START → youtube_fetch   ──────┘

Progress events are reported via BullMQ job.updateProgress() so the
Node gateway can stream them to the frontend via QueueEvents + SSE.
The event format matches docs/python-service-contract.md exactly.
"""

from __future__ import annotations    # Allows Python to delay evaluating type hints.

import logging
from datetime import datetime, timezone
from typing import Any

from langgraph.graph import END, START, StateGraph

from app.graphs.nodes.store_node import store_documents
from app.graphs.nodes.wikipedia_node import wikipedia_fetch
from app.graphs.nodes.reddit_node import reddit_fetch
from app.graphs.nodes.youtube_node import youtube_fetch
from app.graphs.state import ResearchState


logger = logging.getLogger(__name__)


def build_graph() -> Any:
    """Construct and compile the LangGraph StateGraph for the research pipeline."""
    graph = StateGraph(ResearchState)

    graph.add_node("wikipedia_fetch", wikipedia_fetch)
    graph.add_node("reddit_fetch", reddit_fetch)
    graph.add_node("youtube_fetch", youtube_fetch)
    graph.add_node("store_documents", store_documents)

    # Fan-out: All fetch nodes start simultaneously
    graph.add_edge(START, "wikipedia_fetch")
    graph.add_edge(START, "reddit_fetch")
    graph.add_edge(START, "youtube_fetch")

    # Fan-in: All fetch nodes converge at store_documents
    graph.add_edge("wikipedia_fetch", "store_documents")
    graph.add_edge("reddit_fetch", "store_documents")
    graph.add_edge("youtube_fetch", "store_documents")
    
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

    await _emit(job, {"type": "connected", "status": "connected"})     # internally - job.updateProgress(...)

    # Wire the resume path: if a source is NOT in `sources` (which means it either
    # succeeded in a prior run or wasn't requested), we pre-populate its state
    # as "done" with empty documents. The fetch nodes will see "done" and short-circuit.
    # store_documents will merge [] for them, which is correct because their
    # actual documents were already stored in the DB during the previous run.
    all_possible_sources = ["wikipedia", "reddit", "youtube"]
    initial_sources_state = {}
    for s in all_possible_sources:
        if s not in sources:
            initial_sources_state[s] = {"status": "done", "documents": [], "error": None}

    try:
        compiled_graph = build_graph()       # Nothing executes yet. Just builds the workflow.

        initial_state: ResearchState = {
            "job_id": job_id,
            "query": query_text,
            "sources_to_fetch": sources,
            "sources": initial_sources_state,
            "merged_documents": [],
            "status": "pending",
            "results": {},
        }

        # Emit: wikipedia fetch started
        await _emit(job, {"type": "progress", "source": "wikipedia", "status": "started"})

        # Run the full graph
        final_state = await compiled_graph.ainvoke(initial_state)   # Starts executing this graph using the provided state.

        # Collect results from the final state
        results = final_state.get("results", {})
        docs_count = results.get("docsInserted", 0)
        chunks_count = results.get("chunksInserted", 0)
        
        final_sources = final_state.get("sources", {}) 
        failed = [s for s, res in final_sources.items() if res.get("status") == "failed"]  #appends failed sources to `failed` list if any
        
        # Emit terminal progress event for each source that was fetched in this run
        for source in sources:
            src_res = final_sources.get(source, {})
            evt = {
                "type": "progress",
                "source": source,
                "status": src_res.get("status", "error"),
            }
            if src_res.get("error"):
                evt["error"] = src_res["error"]
            await _emit(job, evt)

        # Build results dict (what gets returned to BullMQ)
        job_results = {}
        for s in all_possible_sources:
            s_res = final_sources.get(s, {})
            job_results[s] = {
                "status": s_res.get("status", "pending"),
            }
        job_results["total_docs_inserted"] = docs_count
        job_results["total_chunks_inserted"] = chunks_count

        # Node.js listens for queryQueue's done event now.
        # Do not emit a terminal event here so the SSE stream stays open.
        # await _emit(job, {"type": "done", "status": "done", "results": job_results})

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
