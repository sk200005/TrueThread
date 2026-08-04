"""
graphs/nodes/youtube_node.py — Fetch YouTube content for a research query.

Placeholder implementation for Milestone 4 (Parallel fetching).
"""

from __future__ import annotations

import logging
from typing import Any

from app.graphs.state import ResearchState, SourceDoc

logger = logging.getLogger(__name__)


async def youtube_fetch(state: ResearchState) -> dict[str, Any]:
    """
    LangGraph node: fetch YouTube transcripts for the query.

    Populates state["sources"]["youtube"] with SourceDoc dicts.
    """
    query = state.get("query", "")
    sources = state.get("sources", {})
    youtube_state = sources.get("youtube", {})

    if youtube_state.get("status") == "done":
        logger.info("YouTube fetch already done, skipping.")
        return {"sources": {"youtube": youtube_state}}

    logger.info("YouTube fetch starting for query: %r", query)
    documents: list[SourceDoc] = []

    try:
        # TODO: Implement actual YouTube API fetching/transcripts
        # For now, simulate a successful fetch with a dummy document
        doc: SourceDoc = {
            "source": "youtube",
            "author": "Placeholder Channel",
            "text": f"YouTube video transcript discussing {query} (Placeholder text)",
            "url": f"https://youtube.com/results?search_query={query}",
            "published_at": None,
            "engagement_metrics": {"views": 1000},
        }
        documents.append(doc)
        logger.info("Fetched YouTube transcript (placeholder).")

    except Exception as exc:
        logger.error("YouTube fetch failed entirely: %s", exc)
        return {
            "sources": {
                "youtube": {
                    "status": "failed",
                    "documents": [],
                    "error": str(exc)
                }
            }
        }

    return {
        "sources": {
            "youtube": {
                "status": "done",
                "documents": documents,
                "error": None
            }
        }
    }
