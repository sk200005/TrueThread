"""
graphs/query_graph.py — Builds and runs the query-time LangGraph pipeline.

This is a SEPARATE graph from the ingestion graph (research_graph.py).
The ingestion graph fetches and stores data. This graph retrieves and
analyzes already-stored data in response to a user query.

Graph structure:
    START → rag_retrieve → extract_claims → summarize → END

Why a separate graph (not merged into research_graph.py):
    1. Different lifecycle — ingestion runs when data is scraped;
       query runs when a user asks a question
    2. Different state shape — ingestion needs raw_documents, sources_to_fetch;
       query needs retrieved_chunks, extracted_claims, final_report
    3. Different trigger — ingestion is triggered by job submission;
       query is triggered by a user query against existing data
    4. Cleaner code — each graph stays focused and readable

Progress events are reported via BullMQ job.updateProgress() so the
Node gateway can stream them to the frontend via QueueEvents + SSE.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from langgraph.graph import END, START, StateGraph

from app.graphs.nodes.extract_claims import extract_claims
from app.graphs.nodes.retrieve import rag_retrieve
from app.graphs.nodes.summarize import summarize
from app.graphs.state import QueryState

# @deprecated — JobState no longer used. Workers now receive BullMQ Job objects.
# from app.services.job_manager import JobState

logger = logging.getLogger(__name__)


def build_query_graph() -> Any:
    """Construct and compile the query-time LangGraph StateGraph."""
    graph = StateGraph(QueryState)

    # Add nodes
    graph.add_node("rag_retrieve", rag_retrieve)
    graph.add_node("extract_claims", extract_claims)
    graph.add_node("summarize", summarize)

    # Wire edges: START → retrieve → extract → summarize → END
    graph.add_edge(START, "rag_retrieve")
    graph.add_edge("rag_retrieve", "extract_claims")
    graph.add_edge("extract_claims", "summarize")
    graph.add_edge("summarize", END)

    return graph.compile()


async def _emit(job, event: dict[str, Any]) -> None:
    """Emit a progress event via BullMQ, auto-adding jobId and timestamp.

    Mirrors the old JobState.emit_event() interface so event payloads stay
    identical to what the Node SSE layer expects.
    """
    event.setdefault("jobId", job.data.get("jobId", job.id))
    event.setdefault("timestamp", datetime.now(timezone.utc).isoformat())
    await job.updateProgress(event)


async def run_query_pipeline(job) -> dict:
    """
    Execute the query-time graph for a BullMQ job.

    Reports progress via job.updateProgress() at the same points where
    the old JobState.emit_event() was called — keeps the same event
    shape so the Node SSE layer doesn't need to change.

    Args:
        job: BullMQ Job object. job.data contains:
             { jobId, userId, queryText, sources }

    Returns:
        dict: The final results, stored by BullMQ as the job's return value.
    """
    job_id = job.data.get("jobId", job.id)
    query_text = job.data["queryText"]

    await _emit(job, {"type": "connected", "status": "connected"})

    try:
        compiled_graph = build_query_graph()

        # Initial state for the query-time pipeline
        initial_state: QueryState = {
            "job_id": job_id,
            "query": query_text,
            # query_id could be passed from the job if needed for scoped search.
            # For now, we search all chunks (cross-query retrieval).
            "top_k": 8,
        }

        # ── Phase 1: RAG Retrieval ───────────────────────────────────────
        await _emit(job, {
            "type": "progress",
            "source": "rag_retrieve",
            "status": "started",
        })

        # Run the full graph (all three nodes execute in sequence)
        final_state = await compiled_graph.ainvoke(initial_state)

        # ── Collect results ──────────────────────────────────────────────
        retrieved_chunks = final_state.get("retrieved_chunks", [])
        extracted_claims = final_state.get("extracted_claims", [])
        final_report = final_state.get("final_report", {})

        # Emit progress events for each completed phase
        await _emit(job, {
            "type": "progress",
            "source": "rag_retrieve",
            "status": "done",
            "counts": {"chunksRetrieved": len(retrieved_chunks)},
        })

        await _emit(job, {
            "type": "progress",
            "source": "extract_claims",
            "status": "done",
            "counts": {"claimsExtracted": len(extracted_claims)},
        })

        await _emit(job, {
            "type": "progress",
            "source": "summarize",
            "status": "done",
        })

        # ── Build results ────────────────────────────────────────────────
        results = {
            "report": final_report,
            "chunksRetrieved": len(retrieved_chunks),
            "claimsExtracted": len(extracted_claims),
        }

        # Emit terminal event — Node closes the SSE stream on receiving this
        await _emit(job, {
            "type": "done",
            "status": "done",
            "results": results,
        })

        logger.info(
            "Query pipeline complete for job %s: %d chunks, %d claims",
            job_id, len(retrieved_chunks), len(extracted_claims),
        )

        return results

    except Exception as exc:
        error_msg = str(exc)

        # Emit terminal error event before re-raising
        await _emit(job, {
            "type": "error",
            "status": "error",
            "error": error_msg,
        })

        logger.exception("Query pipeline failed for job %s: %s", job_id, exc)
        raise  # Let BullMQ mark the job as failed
