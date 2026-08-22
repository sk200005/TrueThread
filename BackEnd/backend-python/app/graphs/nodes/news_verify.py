"""
graphs/nodes/news_verify.py — News verification path (Phase E).

Three sub-steps combined in one module:

1. extract_news_query — LLM converts raw claim text into a structured
   NewsAPI query (short keywords + boolean operators). Never passes the
   raw claim sentence directly to NewsAPI's `q` param.

2. fetch_news_articles — Calls NewsAPI /v2/everything with the structured
   query. Falls back to entity-only query on zero results.

3. filter_relevant_articles — Lightweight relevance filter: re-ranks
   returned articles against the original claim and drops irrelevant ones
   before passing to the verification scorer.

Known limitations (free tier):
    - 100 requests/day
    - ~1 month lookback (from today)
    - No access to /v2/top-headlines source filtering on free plan
    These are documented here so they're not silently papered over.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from pydantic import BaseModel, Field
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
)

from app.core.config import settings
from app.core.llm_client import get_llm_client
from app.graphs.state import (
    ClaimEvidenceDict,
    ClassifiedClaimDict,
    NewsArticleDict,
    QueryState,
)

logger = logging.getLogger(__name__)

# NewsAPI endpoint
NEWSAPI_BASE_URL = "https://newsapi.org/v2/everything"

# ── Free-tier constraint: lookback is ~1 month ────────────────────────────
# The free plan only returns articles from the last ~30 days.
# Paid plans support up to 5 years. This is a known limitation.
LOOKBACK_DAYS = 29  # Stay under 30-day limit to avoid edge-case failures


# ══════════════════════════════════════════════════════════════════════════
# Sub-step 1: Extract structured NewsAPI query from claim text
# ══════════════════════════════════════════════════════════════════════════

class NewsQuery(BaseModel):
    """Structured query object for NewsAPI."""

    keywords: str = Field(
        description="Short keyword phrase with optional AND/OR/NOT operators and quoted exact phrases"
    )
    date_range_hint: str = Field(
        default="last_month",
        description="Rough recency hint: last_week | last_month | last_year"
    )


QUERY_EXTRACTION_PROMPT = """You are a search query optimizer. Given a claim, produce a concise NewsAPI search query.

Return JSON only — no prose, no markdown fences.

Schema:
{
  "keywords": "string — short keyword phrase for NewsAPI q= param. Use AND/OR/NOT boolean operators and \"quoted phrases\" for precision. Max 5-6 words. Do NOT just copy the raw claim sentence.",
  "date_range_hint": "last_week" | "last_month" | "last_year"
}

Examples:
- Claim: "Tesla's Cybertruck has been recalled due to accelerator pedal issues"
  → {"keywords": "Tesla Cybertruck recall \"accelerator pedal\"", "date_range_hint": "last_month"}

- Claim: "Apple iPhone 16 Pro has overheating problems during video recording"
  → {"keywords": "iPhone 16 Pro overheating video", "date_range_hint": "last_month"}

- Claim: "Samsung Galaxy S24 Ultra outsells iPhone 15 in India"
  → {"keywords": "Samsung \"Galaxy S24 Ultra\" sales India", "date_range_hint": "last_month"}
"""


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=2, min=2, max=30),
    retry=retry_if_exception_type(Exception),
    reraise=True,
)
async def _extract_news_query(claim_text: str) -> NewsQuery:
    """LLM call to convert a claim into a structured search query."""
    client = get_llm_client()
    raw_response = await client.chat(
        system_prompt=QUERY_EXTRACTION_PROMPT,
        user_prompt=f"Claim: {claim_text}",
        temperature=0.1,
        max_tokens=256,
    )

    # Parse response
    cleaned = raw_response.strip()
    cleaned = re.sub(r'^```(?:json)?\s*', '', cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r'\s*```$', '', cleaned, flags=re.IGNORECASE)

    try:
        parsed = json.loads(cleaned.strip())
        return NewsQuery.model_validate(parsed)
    except Exception as e:
        logger.warning("Failed to parse news query, falling back to entity extraction: %s", e)
        # Fallback: use the first few meaningful words
        words = [w for w in claim_text.split() if len(w) > 3][:4]
        return NewsQuery(keywords=" ".join(words), date_range_hint="last_month")


# ══════════════════════════════════════════════════════════════════════════
# Sub-step 2: Fetch articles from NewsAPI
# ══════════════════════════════════════════════════════════════════════════

async def _fetch_news_articles(
    query: NewsQuery,
    entities: list[str] | None = None,
) -> list[dict[str, Any]]:
    """
    Call NewsAPI /v2/everything with the structured query.

    On zero results, falls back to a simpler entity-only query
    (extracts named entities from the original claim and retries).

    Returns raw NewsAPI article dicts.
    """
    if not settings.news_api_key:
        logger.warning("NEWS_API_KEY not set — skipping news fetch.")
        return []

    # Calculate date range based on hint
    now = datetime.now(timezone.utc)
    if query.date_range_hint == "last_week":
        from_date = (now - timedelta(days=7)).strftime("%Y-%m-%d")
    elif query.date_range_hint == "last_year":
        # Free tier caps at ~1 month, but we set the param anyway
        from_date = (now - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    else:  # last_month (default)
        from_date = (now - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%d")

    params = {
        "q": query.keywords,
        "from": from_date,
        "sortBy": "relevancy",
        "language": "en",
        "pageSize": 10,
        "apiKey": settings.news_api_key,
    }

    async with httpx.AsyncClient(timeout=15) as client:
        try:
            resp = await client.get(NEWSAPI_BASE_URL, params=params)
            resp.raise_for_status()
            data = resp.json()

            articles = data.get("articles", [])

            # ── Fallback: entity-only query on zero results ──────────────
            if not articles and entities:
                fallback_query = " OR ".join(f'"{e}"' for e in entities[:3])
                logger.info(
                    "Zero results for %r, retrying with entity-only query: %s",
                    query.keywords, fallback_query,
                )
                params["q"] = fallback_query
                resp = await client.get(NEWSAPI_BASE_URL, params=params)
                resp.raise_for_status()
                data = resp.json()
                articles = data.get("articles", [])

            logger.info(
                "NewsAPI returned %d articles for query: %s",
                len(articles), query.keywords,
            )
            return articles

        except httpx.HTTPStatusError as e:
            logger.error("NewsAPI HTTP error: %s — %s", e.response.status_code, e.response.text[:200])
            return []
        except Exception as e:
            logger.error("NewsAPI request failed: %s", e)
            return []


# ══════════════════════════════════════════════════════════════════════════
# Sub-step 3: Lightweight relevance filter
# ══════════════════════════════════════════════════════════════════════════

async def _filter_relevant_articles(
    claim_text: str,
    articles: list[dict[str, Any]],
) -> list[NewsArticleDict]:
    """
    Re-rank and filter articles for relevance to the specific claim using keyword overlap.
    Drops articles that are topically adjacent but not about the claim.
    """
    if not articles:
        return []

    # Extract words from claim text for simple matching
    claim_words = set(re.findall(r'\w+', claim_text.lower()))

    filtered: list[NewsArticleDict] = []
    
    for article in articles:
        title = article.get("title", "") or ""
        desc = article.get("description", "") or ""
        
        # Combine title and description for matching
        text_to_search = (title + " " + desc).lower()
        article_words = set(re.findall(r'\w+', text_to_search))
        
        # Calculate simple overlap score
        overlap = len(claim_words.intersection(article_words))
        score = overlap / len(claim_words) if claim_words else 0.5
        
        # Base filter: at least some overlap
        if score > 0.1 or not claim_words:
            filtered.append({
                "title": title,
                "url": article.get("url", ""),
                "source_name": article.get("source", {}).get("name", ""),
                "snippet": desc or article.get("content", "")[:300],
                "published_at": article.get("publishedAt", ""),
                "relevance_score": float(score),
            })
            
    # Sort by relevance score descending, keep top 5
    filtered.sort(key=lambda x: x.get("relevance_score", 0), reverse=True)
    return filtered[:5]


# ══════════════════════════════════════════════════════════════════════════
# LangGraph Node
# ══════════════════════════════════════════════════════════════════════════

async def news_verify(state: QueryState) -> dict[str, Any]:
    """
    LangGraph node: fetch and filter news evidence for claims routed to "news" or "both".

    Reads from state:
        - classified_claims: list of ClassifiedClaimDict

    Returns:
        - news_evidence: list of ClaimEvidenceDict (one per news-routed claim)
        - status: "verifying"

    Only processes claims where route is "news" or "both". Skips the rest.
    """
    classified_claims = state.get("classified_claims", [])
    news_claims = [
        c for c in classified_claims
        if c.get("route") in ("news", "both")
    ]

    if not news_claims:
        logger.info("No claims routed to news verification.")
        return {"news_evidence": [], "status": "verifying"}

    logger.info("Fetching news evidence for %d claims...", len(news_claims))

    evidence_list: list[ClaimEvidenceDict] = []

    for claim in news_claims:
        claim_text = claim["claim_text"]
        entities = claim.get("entities", [])

        # Sub-step 1: Extract structured query
        news_query = await _extract_news_query(claim_text)
        logger.info("  Query for %r: %s", claim_text[:40], news_query.keywords)

        # Sub-step 2: Fetch articles
        raw_articles = await _fetch_news_articles(news_query, entities)

        # Sub-step 3: Filter for relevance
        filtered_articles = await _filter_relevant_articles(claim_text, raw_articles)
        logger.info(
            "  %d/%d articles passed relevance filter for: %s",
            len(filtered_articles), len(raw_articles), claim_text[:40],
        )

        evidence: ClaimEvidenceDict = {
            "claim_text": claim_text,
            "route": claim["route"],
            "news_articles": filtered_articles,
        }
        evidence_list.append(evidence)

    logger.info("News verification complete: %d claims processed.", len(evidence_list))

    return {
        "news_evidence": evidence_list,
    }
