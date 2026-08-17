"""
graphs/state.py — LangGraph state schemas.

Two separate state schemas for two separate graphs:

1. ResearchState — Used by the INGESTION graph (fetch → store).
   Mirrors docs/BackEnd and DB.md §3.2. Unchanged from Milestone 2.

2. QueryState — Used by the QUERY-TIME graph (retrieve → extract → summarize).
   Introduced in Phase C for the RAG + summarization pipeline.
   Extended in Phase E with classification + verification fields.
"""

from __future__ import annotations

from typing import Any, Literal, Optional, TypedDict

import operator
from typing import Annotated

# ══════════════════════════════════════════════════════════════════════════
# Ingestion Graph State (existing — DO NOT MODIFY)
# ══════════════════════════════════════════════════════════════════════════

class SourceDoc(TypedDict, total=False):
    """A single fetched document, before chunking."""

    source: str                         # "wikipedia" | "reddit" | "youtube" | ...
    author: Optional[str]
    title: Optional[str]
    text: str
    url: str
    published_at: Optional[str]
    engagement_metrics: Optional[dict[str, Any]]
    metadata: Optional[dict[str, Any]]


class SourceResult(TypedDict, total=False):
    status: Literal["pending", "in_progress", "done", "failed"]
    documents: list[SourceDoc]
    error: str | None
    skipped: list[dict[str, Any]]

def merge_sources(a: dict[str, SourceResult], b: dict[str, SourceResult]) -> dict[str, SourceResult]:
    """Deep merge for the sources dict to support concurrent partial writes."""
    res = a.copy()
    for k, v in b.items():
        if k in res:
            res[k] = {**res[k], **v}
        else:
            res[k] = v
    return res

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
    sources: Annotated[dict[str, SourceResult], merge_sources]
    merged_documents: list[SourceDoc]
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


# ══════════════════════════════════════════════════════════════════════════
# Phase E — Verification Pipeline TypedDicts
# ══════════════════════════════════════════════════════════════════════════

class ClassifiedClaimDict(TypedDict):
    """Output of classify_claims: routing decision per claim."""

    claim_text: str
    entities: list[str]
    claim_type: str
    confidence: str
    source_comment_id: str
    verifiable: bool                    # False for pure opinion/recommendation
    time_nature: str                    # "current" | "historical" | "both"
    route: str                          # "news" | "wikipedia" | "both" | "skip"


class NewsArticleDict(TypedDict, total=False):
    """A single news article returned by NewsAPI, after relevance filtering."""

    title: str
    url: str
    source_name: str
    snippet: str                        # description or truncated content
    published_at: str                   # ISO-8601
    relevance_score: float              # 0-1, from relevance filter


class ClaimEvidenceDict(TypedDict, total=False):
    """Evidence gathered for a single claim (news, wiki, or both)."""

    claim_text: str
    route: str                          # The route this claim was classified to
    news_articles: list[NewsArticleDict]
    wiki_context: str                   # Wikipedia excerpt text
    wiki_url: str                       # Wikipedia article URL
    wiki_title: str                     # Wikipedia article title


class CitationDict(TypedDict):
    """A single source citation backing a verification verdict."""

    url: str
    title: str
    snippet: str


class VerifiedClaimDict(TypedDict):
    """Final verification result for a single claim."""

    claim: str
    verdict: str                        # "supported" | "contradicted" | "unverified" | "disputed"
    confidence: float                   # 0.0 - 1.0
    source_type: str                    # "news" | "wikipedia" | "both"
    citations: list[CitationDict]
    justification: str                  # LLM's reasoning for the verdict


class QueryState(TypedDict, total=False):
    """
    LangGraph state for the query-time pipeline.

    Flow (Phase E):
        START → rag_retrieve → extract_claims → classify_claims
              → [news_verify | wiki_verify | both | skip]
              → verify_claim → summarize → END

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

    # ── Produced by classify_claims (Phase E) ─────────────────────────────
    classified_claims: list[ClassifiedClaimDict]

    # ── Produced by news_verify / wiki_verify (Phase E) ───────────────────
    news_evidence: list[ClaimEvidenceDict]
    wiki_evidence: list[ClaimEvidenceDict]

    # ── Produced by verify_claim (Phase E) ────────────────────────────────
    verified_claims: list[VerifiedClaimDict]

    # ── Produced by summarize ─────────────────────────────────────────────
    final_report: dict[str, Any]        # The JSON report: sentiment, themes, summary

    # ── Bookkeeping ───────────────────────────────────────────────────────
    status: Literal["pending", "retrieving", "extracting", "classifying", "verifying", "summarizing", "done", "error"]
