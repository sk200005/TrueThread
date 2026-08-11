"""
graphs/nodes/wikipedia_node.py — Fetch Wikipedia content for a research query.

Uses the `wikipedia-api` package (MediaWiki REST API wrapper) to:
  1. Search for articles matching the query
  2. Fetch full article text for the top results
  3. Handle disambiguation pages and missing articles gracefully

Rate limiting: wikipedia-api respects MediaWiki's rate limits internally.
We limit to 5 articles per query to keep fetch times reasonable.
"""

from __future__ import annotations

import logging
from typing import Any

import wikipediaapi

from app.graphs.state import ResearchState, SourceDoc

logger = logging.getLogger(__name__)

MAX_ARTICLES = 5

# wikipedia-api requires a descriptive user-agent per MediaWiki policy
_wiki = wikipediaapi.Wikipedia(
    user_agent="ReSearchPlatform/0.1 (research-project; contact@example.com)",
    language="en",
)


def _search_wikipedia(query: str, limit: int = MAX_ARTICLES) -> list[str]:
    """
    Search Wikipedia for article titles matching the query.

    Uses the MediaWiki opensearch API via a lightweight requests call,
    since wikipedia-api doesn't expose search directly.
    """
    import requests

    url = "https://en.wikipedia.org/w/api.php"
    params = {
        "action": "opensearch",
        "search": query,
        "limit": limit,
        "namespace": 0,
        "format": "json",
    }
    try:
        headers = {
            "User-Agent": "ReSearchPlatform/0.1 (research-project; contact@example.com)",
        }
        resp = requests.get(url, params=params, headers=headers, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        # opensearch returns [query, [titles], [descriptions], [urls]]
        return data[1] if len(data) > 1 else []
    except Exception as exc:
        logger.warning("Wikipedia search failed: %s", exc)
        return []


async def wikipedia_fetch(state: ResearchState) -> dict[str, Any]:
    """
    LangGraph node: fetch Wikipedia articles for the query.

    Populates `raw_documents` with SourceDoc dicts for each article found.
    Appends "wikipedia" to `failed_sources` if the entire fetch fails.
    """
    query = state.get("query", "")
    sources = state.get("sources", {})
    wiki_state = sources.get("wikipedia", {})

    if wiki_state.get("status") == "done":
        logger.info("Wikipedia fetch already done, skipping.")
        return {"sources": {"wikipedia": wiki_state}}

    logger.info("Wikipedia fetch starting for query: %r", query)
    documents: list[SourceDoc] = []


    try:
        import asyncio
        loop = asyncio.get_running_loop()
        
        titles = await asyncio.to_thread(_search_wikipedia, query)
        logger.info("Wikipedia search returned %d titles: %s", len(titles), titles)

        if not titles:
            logger.warning("No Wikipedia articles found for query: %r", query)
            return {
                "sources": {
                    "wikipedia": {
                        "status": "done",
                        "documents": documents,
                        "error": None
                    }
                }
            }

        def _fetch_page(title: str) -> SourceDoc | None:
            page = _wiki.page(title)
            if not page.exists():
                logger.debug("Wikipedia page does not exist: %s", title)
                return None
            
            # Skip disambiguation pages — they're lists, not content
            if "disambiguation" in (page.summary or "").lower() and len(page.text) < 500:
                logger.debug("Skipping disambiguation page: %s", title)
                return None

            text = page.text
            if not text or len(text.strip()) < 100:
                logger.debug("Skipping page with insufficient content: %s", title)
                return None

            return {
                "source": "wikipedia",
                "author": None,
                "text": text,
                "url": page.fullurl,
                "published_at": None,
                "engagement_metrics": None,
            }

        for title in titles[:MAX_ARTICLES]:
            try:
                doc = await asyncio.to_thread(_fetch_page, title)
                if doc:
                    documents.append(doc)
                    logger.info("Fetched Wikipedia article: %s (%d chars)", title, len(doc["text"]))
            except Exception as exc:
                logger.warning("Failed to fetch Wikipedia page '%s': %s", title, exc)
                continue

    except Exception as exc:
        logger.error("Wikipedia fetch failed entirely: %s", exc)
        return {
            "sources": {
                "wikipedia": {
                    "status": "failed",
                    "documents": [],
                    "error": str(exc)
                }
            }
        }

    return {
        "sources": {
            "wikipedia": {
                "status": "done",
                "documents": documents,
                "error": None
            }
        }
    }
