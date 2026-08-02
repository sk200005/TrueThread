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

SSE events are emitted at each node boundary so the Node gateway can
stream live progress to the frontend client.
"""

from __future__ import annotations

import logging
from typing import Any

from langgraph.graph import END, START, StateGraph

from app.graphs.nodes.extract_claims import extract_claims
from app.graphs.nodes.retrieve import rag_retrieve
from app.graphs.nodes.summarize import summarize
from app.graphs.state import QueryState
from app.services.job_manager import JobState

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


async def run_query_pipeline(job: JobState) -> None:
    """
    Execute the query-time graph for a job. Called as a BackgroundTask
    by the jobs router.

    Emits SSE events to the job's event queue as each phase executes.
    Event format matches what pythonServiceClient.js expects to parse:
        { type, jobId, source, status, counts, error, timestamp }

    Args:
        job: The JobState object from the job manager. Contains the query
             text and job_id needed to run the pipeline.
    """
    job.status = "running"
    job.emit_event({"type": "connected", "status": "connected"})

    try:
        compiled_graph = build_query_graph()

        # Initial state for the query-time pipeline
        initial_state: QueryState = {
            "job_id": job.job_id,
            "query": job.query_text,
            # query_id could be passed from the job if needed for scoped search.
            # For now, we search all chunks (cross-query retrieval).
            "top_k": 8,
        }

        # ── Phase 1: RAG Retrieval ───────────────────────────────────────
        job.emit_event({
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
        job.emit_event({
            "type": "progress",
            "source": "rag_retrieve",
            "status": "done",
            "counts": {"chunksRetrieved": len(retrieved_chunks)},
        })

        job.emit_event({
            "type": "progress",
            "source": "extract_claims",
            "status": "done",
            "counts": {"claimsExtracted": len(extracted_claims)},
        })

        job.emit_event({
            "type": "progress",
            "source": "summarize",
            "status": "done",
        })

        # ── Update job state ─────────────────────────────────────────────
        job.results = {
            "report": final_report,
            "chunksRetrieved": len(retrieved_chunks),
            "claimsExtracted": len(extracted_claims),
        }
        job.status = "done"

        # Emit terminal event — Node destroys the stream on receiving this
        job.emit_event({
            "type": "done",
            "status": "done",
            "results": job.results,
        })

        logger.info(
            "Query pipeline complete for job %s: %d chunks, %d claims",
            job.job_id, len(retrieved_chunks), len(extracted_claims),
        )

    except Exception as exc:
        job.status = "error"
        error_msg = str(exc)

        # Emit terminal error event
        job.emit_event({
            "type": "error",
            "status": "error",
            "error": error_msg,
        })

        logger.exception("Query pipeline failed for job %s: %s", job.job_id, exc)
