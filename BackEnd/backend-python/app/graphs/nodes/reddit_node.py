"""
graphs/nodes/reddit_node.py — Fetch Reddit content for a research query.
"""

from __future__ import annotations

import logging
from typing import Any
from datetime import datetime

from app.graphs.state import ResearchState, SourceDoc
from app.ingestion import reddit_client

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
        results = await reddit_client.scrape(query)
        
        for post in results:
            text_parts = []
            if post.get("title"):
                text_parts.append(f"Title: {post['title']}")
            if post.get("body"):
                text_parts.append(f"Body:\n{post['body']}")
                
            comments = post.get("comments", [])
            if comments:
                comments_text = "\n".join([f"- {c['author']}: {c['text']}" for c in comments if c.get("text")])
                text_parts.append(f"Comments:\n{comments_text}")
                
            full_text = "\n\n".join(text_parts)
            
            # published_at handling (reddit DOM often returns iso format in <time datetime="... ">)
            # but sometimes just relative string. 
            # We'll just pass None if it's not a valid ISO date, or let DB handle it as string if allowed.
            pub_date = None
            if comments and comments[0].get("published_date"):
                try:
                    # try parsing just to validate, or store as string
                    pd = comments[0]["published_date"]
                    # If it's something like "2023-01-01T00:00:00.000Z", fromisoformat works without Z
                    if pd.endswith("Z"):
                        pd = pd[:-1] + "+00:00"
                    pub_date = datetime.fromisoformat(pd)
                except ValueError:
                    pass
            
            doc: SourceDoc = {
                "source": "reddit",
                "author": post.get("subreddit") or "Unknown",
                "text": full_text,
                "url": post.get("url"),
                "published_at": pub_date,
                "engagement_metrics": {
                    "upvotes": post.get("upvotes")
                }
            }
            documents.append(doc)

        if not documents:
            logger.warning("No Reddit posts were successfully collected.")
        else:
            logger.info("Successfully fetched %d Reddit posts.", len(documents))

    except Exception as exc:
        logger.exception("Reddit fetch failed entirely: %s", exc)
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
