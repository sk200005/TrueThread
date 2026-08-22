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

import json
import logging
import re
from typing import Any

import wikipediaapi

from app.graphs.state import ResearchState, SourceDoc

logger = logging.getLogger(__name__)

MAX_ARTICLES = 5

# wikipedia-api requires a descriptive user-agent per MediaWiki policy
_wiki = wikipediaapi.Wikipedia(
    user_agent="SwayamsResearchApp/1.0 (swayam-test-app; swayam@example.com)",
    language="en",
)


import re

def extract_keywords(query: str) -> str:
    """Strips conversational words so Wikipedia's strict search engine can find results."""
    stopwords = {
        "what", "when", "where", "why", "who", "how", "is", "are", "was", "were", 
        "do", "does", "did", "happen", "happened", "in", "on", "at", "to", "the", 
        "a", "an", "of", "for", "about", "tell", "me", "explain", "details", "give"
    }
    words = query.split()
    keywords = []
    for w in words:
        w_clean = re.sub(r'[^\w\s]', '', w)
        if w_clean.lower() not in stopwords and len(w_clean) > 0:
            keywords.append(w_clean)
    return " ".join(keywords)

def _search_wikipedia(query: str, limit: int = MAX_ARTICLES) -> list[str]:
    """
    Search Wikipedia for article titles matching the query.

    Extracts keywords from conversational queries and uses the robust 
    full-text 'srsearch' API for better matching.
    """
    import requests

    search_query = extract_keywords(query)
    # If stripping leaves it empty, fallback to original query
    if not search_query.strip():
        search_query = query
        
    url = "https://en.wikipedia.org/w/api.php"
    params = {
        "action": "query",
        "list": "search",
        "srsearch": search_query,
        "format": "json",
        "srlimit": limit,
    }
    try:
        headers = {
            "User-Agent": "SwayamsResearchApp/1.0 (swayam-test-app; swayam@example.com)",
        }
        resp = requests.get(url, params=params, headers=headers, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        
        results = data.get("query", {}).get("search", [])
        return [item["title"] for item in results]
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

        # ── Keep top 3 articles ───────────────────────────────
        filtered_titles = titles[:3]
        logger.info("Kept top 3 titles: %s", filtered_titles)

        def _fetch_page(title: str) -> SourceDoc | None:
            page = _wiki.page(title)
            if not page.exists():
                logger.debug("Wikipedia page does not exist: %s", title)
                return None
            
            # Skip disambiguation pages — they're lists, not content
            if "disambiguation" in (page.summary or "").lower() and len(page.text) < 500:
                logger.debug("Skipping disambiguation page: %s", title)
                return None

            text = page.summary
            if not text or len(text.strip()) < 50:
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

        for title in filtered_titles:
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
