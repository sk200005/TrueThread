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

from app.core.llm_client import get_llm_client
from app.graphs.state import ResearchState, SourceDoc

logger = logging.getLogger(__name__)

MAX_ARTICLES = 5

# wikipedia-api requires a descriptive user-agent per MediaWiki policy
_wiki = wikipediaapi.Wikipedia(
    user_agent="SwayamsResearchApp/1.0 (swayam-test-app; swayam@example.com)",
    language="en",
)

# ── LLM-based title filter ────────────────────────────────────────────────────

_TITLE_FILTER_SYSTEM = (
    "You are a search result filter. Given a user's research query and a list "
    "of Wikipedia article titles returned by a search engine, return ONLY the "
    "titles that are genuinely about the query subject. Exclude results that "
    "share a similar name but refer to a different person, place, or concept "
    "(e.g. exclude 'Georges Sorel' when the query is 'George Soros'). "
    "Respond with ONLY a JSON array of strings — exact title strings, no commentary, "
    "no markdown fences, no explanation."
)


async def filter_relevant_titles(
    query: str,
    titles: list[str],
    top_n: int = 3,
) -> list[str]:
    """
    Use the Groq LLM to select only the most relevant Wikipedia titles for ``query``.

    Called after the Wikipedia search and BEFORE fetching full article text, so
    only genuinely relevant articles are fetched, chunked, and embedded.

    Falls back to ``titles[:top_n]`` if the LLM call fails or returns an
    unparseable / empty response so the pipeline is never broken by this step.
    """
    if not titles:
        return []

    fallback = titles[:top_n]

    user_prompt = (
        f'Research query: "{query}"\n'
        f'Wikipedia search results: {json.dumps(titles)}\n'
        f'Return a JSON array of the titles that are genuinely relevant to the query. '
        f'Limit to at most {top_n} titles. '
        f'Respond with ONLY the JSON array — no prose, no markdown.'
    )

    try:
        client = get_llm_client()
        raw = await client.chat(
            system_prompt=_TITLE_FILTER_SYSTEM,
            user_prompt=user_prompt,
            temperature=0.0,
            max_tokens=256,
        )
    except Exception as exc:
        logger.warning("LLM title filter failed (%s). Using top-%d fallback.", exc, top_n)
        return fallback

    # Parse defensively — strip markdown fences if the model adds them
    cleaned = raw.strip()
    cleaned = re.sub(r'^```(?:json)?\s*', '', cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r'\s*```$', '', cleaned, flags=re.IGNORECASE)
    cleaned = cleaned.strip()

    try:
        parsed = json.loads(cleaned)
        if not isinstance(parsed, list) or not parsed:
            raise ValueError(f"unexpected shape: {type(parsed).__name__}")
        result = [t for t in parsed if isinstance(t, str)][:top_n]
        if not result:
            raise ValueError("no string titles in parsed list")
        logger.info("LLM title filter kept %d/%d titles: %s", len(result), len(titles), result)
        return result
    except Exception as exc:
        logger.warning("LLM title filter parse failed (%s). Using top-%d fallback.", exc, top_n)
        return fallback


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

        # ── LLM title filter: keep only genuinely relevant articles ──────────────
        filtered_titles = await filter_relevant_titles(query, titles, top_n=3)
        logger.info("LLM filter: %d titles → %d: %s", len(titles), len(filtered_titles), filtered_titles)

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
