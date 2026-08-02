"""
graphs/state.py — LangGraph state schemas.

Two separate state schemas for two separate graphs:

1. ResearchState — Used by the INGESTION graph (fetch → store).
   Mirrors docs/BackEnd and DB.md §3.2. Unchanged from Milestone 2.

2. QueryState — Used by the QUERY-TIME graph (retrieve → extract → summarize).
   Introduced in Phase C for the RAG + summarization pipeline.
"""

from __future__ import annotations

from typing import Any, Literal, Optional, TypedDict


# ══════════════════════════════════════════════════════════════════════════
# Ingestion Graph State (existing — DO NOT MODIFY)
# ══════════════════════════════════════════════════════════════════════════

class SourceDoc(TypedDict):
    """A single fetched document, before chunking."""

    source: str                         # "wikipedia" | "reddit" | "youtube" | ...
    author: Optional[str]
    text: str
    url: str
    published_at: Optional[str]
    engagement_metrics: Optional[dict[str, Any]]


class ResearchState(TypedDict, total=False):
    """
    LangGraph state flowing through the research pipeline.

    All fields are optional (total=False) so nodes only need to set the
    fields they produce. LangGraph merges partial updates into the
    accumulated state automatically.
    """

    job_id: str
    query: str
    sources_to_fetch: list[str]
    raw_documents: list[SourceDoc]
    failed_sources: list[str]
    status: Literal["pending", "fetching", "storing", "done", "error"]
    results: dict[str, Any]


# ══════════════════════════════════════════════════════════════════════════
# Query-Time Graph State (Phase C — NEW)
# ══════════════════════════════════════════════════════════════════════════

class RetrievedChunk(TypedDict):
    """A single chunk returned by RAG cosine-similarity search."""

    chunk_id: str                       # UUID of the document_chunks row
    chunk_text: str                     # The actual text content
    source_document_id: str             # UUID of the parent source_documents row
    similarity: float                   # 1 - cosine_distance (higher = more similar)


class ExtractedClaimDict(TypedDict):
    """A single claim extracted by the LLM, as a plain dict."""

    claim_text: str
    entities: list[str]
    claim_type: str                     # comparison | effectiveness | warning | opinion | factual
    confidence: str                     # high | medium | low
    source_comment_id: str              # chunk_id or platform-native comment ID


class QueryState(TypedDict, total=False):
    """
    LangGraph state for the query-time pipeline.

    Flow: START → rag_retrieve → extract_claims → summarize → END

    All fields are optional (total=False). Each node reads the fields it
    needs and returns a partial dict with the fields it produces.
    """

    # ── Input (set at graph invocation) ───────────────────────────────────
    job_id: str                         # Same job_id as the ingestion run
    query: str                          # The user's natural-language query
    query_id: str                       # UUID of the queries table row (for scoped search)
    top_k: int                          # Number of chunks to retrieve (default: 8)

    # ── Produced by rag_retrieve ──────────────────────────────────────────
    retrieved_chunks: list[RetrievedChunk]

    # ── Produced by extract_claims ────────────────────────────────────────
    extracted_claims: list[ExtractedClaimDict]

    # ── Produced by summarize ─────────────────────────────────────────────
    final_report: dict[str, Any]        # The JSON report: sentiment, themes, summary

    # ── Bookkeeping ───────────────────────────────────────────────────────
    status: Literal["pending", "retrieving", "extracting", "summarizing", "done", "error"]
