"""
graphs/nodes/reddit_node.py — Fetch Reddit content for a research query.

Placeholder implementation for Milestone 4 (Parallel fetching).
"""

from __future__ import annotations

import logging
from typing import Any

from app.graphs.state import ResearchState, SourceDoc

logger = logging.getLogger(__name__)


async def reddit_fetch(state: ResearchState) -> dict[str, Any]:
    """
    LangGraph node: fetch Reddit posts for the query.

    Populates state["sources"]["reddit"] with SourceDoc dicts.
    """
    query = state.get("query", "")
    sources = state.get("sources", {})
    reddit_state = sources.get("reddit", {})

    if reddit_state.get("status") == "done":
        logger.info("Reddit fetch already done, skipping.")
        return {"sources": {"reddit": reddit_state}}

    logger.info("Reddit fetch starting for query: %r", query)
    documents: list[SourceDoc] = []

    try:
        # TODO: Implement actual Reddit API fetching
        # For now, simulate a successful fetch with a dummy document
        doc: SourceDoc = {
            "source": "reddit",
            "author": "u/placeholder",
            "text": f"Reddit discussion about {query} (Placeholder text)",
            "url": f"https://reddit.com/search?q={query}",
            "published_at": None,
            "engagement_metrics": {"upvotes": 100},
        }
        documents.append(doc)
        logger.info("Fetched Reddit post (placeholder).")

    except Exception as exc:
        logger.error("Reddit fetch failed entirely: %s", exc)
        return {
            "sources": {
                "reddit": {
                    "status": "failed",
                    "documents": [],
                    "error": str(exc)
                }
            }
        }

    return {
        "sources": {
            "reddit": {
                "status": "done",
                "documents": documents,
                "error": None
            }
        }
    }
