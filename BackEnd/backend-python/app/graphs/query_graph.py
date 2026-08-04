"""
graphs/query_graph.py — Builds and runs the query-time LangGraph pipeline.

This is a SEPARATE graph from the ingestion graph (research_graph.py).
The ingestion graph fetches and stores data. This graph retrieves and
analyzes already-stored data in response to a user query.

Graph structure (Phase E):
    START → rag_retrieve → extract_claims → classify_claims → conditional_edge:
        "news"      → news_verify → verify_claim → summarize → END
        "wikipedia" → wiki_verify → verify_claim → summarize → END
        "both"      → [news_verify, wiki_verify] (parallel) → verify_claim → summarize → END
        "skip"      → summarize → END

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
from app.graphs.nodes.classify_claim import classify_claims
from app.graphs.nodes.news_verify import news_verify
from app.graphs.nodes.wiki_verify import wiki_verify
from app.graphs.nodes.verify_claim import verify_claim
from app.graphs.state import QueryState


logger = logging.getLogger(__name__)


def route_verification(state: QueryState) -> list[str]:
    """
    Conditional routing edge function.

    Inspects classified_claims in state and routes to news_verify,
    wiki_verify, both (parallel), or directly to summarize (skip).
    """
    classified_claims = state.get("classified_claims", [])
    
    has_news = any(c.get("route") in ("news", "both") for c in classified_claims)
    has_wiki = any(c.get("route") in ("wikipedia", "both") for c in classified_claims)

    routes = []
    if has_news:
        routes.append("news_verify")
    if has_wiki:
        routes.append("wiki_verify")

    if not routes:
        return ["summarize"]
    return routes


def build_query_graph() -> Any:
    """Construct and compile the query-time LangGraph StateGraph."""
    graph = StateGraph(QueryState)

    # Add nodes
    graph.add_node("rag_retrieve", rag_retrieve)
    graph.add_node("extract_claims", extract_claims)
    graph.add_node("classify_claims", classify_claims)
    graph.add_node("news_verify", news_verify)
    graph.add_node("wiki_verify", wiki_verify)
    graph.add_node("verify_claim", verify_claim)
    graph.add_node("summarize", summarize)

    # Wire static edges
    graph.add_edge(START, "rag_retrieve")
    graph.add_edge("rag_retrieve", "extract_claims")
    graph.add_edge("extract_claims", "classify_claims")

    # Wire conditional edge after claim classification
    graph.add_conditional_edges(
        "classify_claims",
        route_verification,
        {
            "news_verify": "news_verify",
            "wiki_verify": "wiki_verify",
            "summarize": "summarize",
        }
    )

    # Wire verification outputs to the scorer
    graph.add_edge("news_verify", "verify_claim")
    graph.add_edge("wiki_verify", "verify_claim")

    # Wire verification scorer to summarization
    graph.add_edge("verify_claim", "summarize")
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
            "top_k": 8,
        }

        # ── Phase 1: RAG Retrieval ───────────────────────────────────────
        await _emit(job, {
            "type": "progress",
            "source": "rag_retrieve",
            "status": "started",
        })

        # Run the full graph
        final_state = await compiled_graph.ainvoke(initial_state)

        # ── Collect results ──────────────────────────────────────────────
        retrieved_chunks = final_state.get("retrieved_chunks", [])
        extracted_claims = final_state.get("extracted_claims", [])
        classified_claims = final_state.get("classified_claims", [])
        news_evidence = final_state.get("news_evidence", [])
        wiki_evidence = final_state.get("wiki_evidence", [])
        verified_claims = final_state.get("verified_claims", [])
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
            "source": "classify_claims",
            "status": "done",
            "counts": {"claimsClassified": len(classified_claims)},
        })

        if news_evidence:
            await _emit(job, {
                "type": "progress",
                "source": "news_verify",
                "status": "done",
                "counts": {"newsEvidenceCount": len(news_evidence)},
            })

        if wiki_evidence:
            await _emit(job, {
                "type": "progress",
                "source": "wiki_verify",
                "status": "done",
                "counts": {"wikiEvidenceCount": len(wiki_evidence)},
            })

        if verified_claims:
            await _emit(job, {
                "type": "progress",
                "source": "verify_claim",
                "status": "done",
                "counts": {"claimsVerified": len(verified_claims)},
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
            "claimsClassified": len(classified_claims),
            "claimsVerified": len(verified_claims),
        }

        # Emit terminal event — Node closes the SSE stream on receiving this
        await _emit(job, {
            "type": "done",
            "status": "done",
            "results": results,
        })

        logger.info(
            "Query pipeline complete for job %s: %d chunks, %d claims, %d verified",
            job_id, len(retrieved_chunks), len(extracted_claims), len(verified_claims),
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
