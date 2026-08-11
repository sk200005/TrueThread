"""
graphs/nodes/wiki_verify.py — Wikipedia verification path (Phase E).

For claims classified as "historical" or "wikipedia"-routed, queries
Wikipedia for the most relevant article/section matching the claim's
subject and extracts the relevant summary text as evidence context.

Mirrors the call pattern from wikipedia_node.py (opensearch API →
wikipedia-api page fetch) but scoped to specific claim subjects
instead of the full user query.

Known limitation: Wikipedia represents editorial consensus, which is
NOT infallible — especially for politically contested topics, recent
events still being updated, or subjects with active edit wars. This
module provides evidence, not ground truth. The verification scorer
(verify_claim.py) must weigh this accordingly.
"""

from __future__ import annotations

import logging
from typing import Any

import requests
import wikipediaapi

from app.graphs.state import ClaimEvidenceDict, QueryState

logger = logging.getLogger(__name__)

# Reuse the same user-agent as wikipedia_node.py for consistency
_wiki = wikipediaapi.Wikipedia(
    user_agent="ReSearchPlatform/0.1 (research-project; contact@example.com)",
    language="en",
)

# Max Wikipedia text to extract per claim (avoid feeding 50K-char articles to the scorer)
MAX_CONTEXT_CHARS = 3000


def _search_wikipedia_for_claim(claim_text: str, entities: list[str]) -> list[str]:
    """
    Search Wikipedia for article titles relevant to a specific claim.

    Strategy:
        1. Try searching with the most specific entity first
        2. Fall back to the full claim text if no entity-based results

    Mirrors the opensearch pattern from wikipedia_node.py.
    """
    url = "https://en.wikipedia.org/w/api.php"
    headers = {
        "User-Agent": "ReSearchPlatform/0.1 (research-project; contact@example.com)",
    }

    # Try entities first (more specific → better Wikipedia matches)
    for entity in entities[:3]:
        try:
            params = {
                "action": "opensearch",
                "search": entity,
                "limit": 3,
                "namespace": 0,
                "format": "json",
            }
            resp = requests.get(url, params=params, headers=headers, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            titles = data[1] if len(data) > 1 else []
            if titles:
                logger.info("Wikipedia search for entity %r returned: %s", entity, titles)
                return titles
        except Exception as exc:
            logger.warning("Wikipedia search failed for entity %r: %s", entity, exc)
            continue

    # Fallback: search with truncated claim text
    try:
        # Use first ~60 chars of claim as search query
        search_query = claim_text[:60].strip()
        params = {
            "action": "opensearch",
            "search": search_query,
            "limit": 3,
            "namespace": 0,
            "format": "json",
        }
        resp = requests.get(url, params=params, headers=headers, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        titles = data[1] if len(data) > 1 else []
        if titles:
            logger.info("Wikipedia fallback search for %r returned: %s", search_query, titles)
        return titles
    except Exception as exc:
        logger.warning("Wikipedia fallback search failed: %s", exc)
        return []


def _fetch_page_context(title: str) -> tuple[str, str, str] | None:
    """
    Fetch the Wikipedia page for the given title and extract relevant context.

    Returns:
        (text_excerpt, full_url, title) or None if page doesn't exist / is a disambiguation page.
    """
    try:
        page = _wiki.page(title)

        if not page.exists():
            logger.debug("Wikipedia page does not exist: %s", title)
            return None

        # Skip disambiguation pages (same check as wikipedia_node.py)
        if "disambiguation" in (page.summary or "").lower() and len(page.text) < 500:
            logger.debug("Skipping disambiguation page: %s", title)
            return None

        # Extract context: summary + first section, capped at MAX_CONTEXT_CHARS
        text = page.summary or ""
        if len(text) < MAX_CONTEXT_CHARS and page.text:
            # Add more text from the full article if summary is short
            text = page.text[:MAX_CONTEXT_CHARS]

        if not text or len(text.strip()) < 50:
            logger.debug("Insufficient content for page: %s", title)
            return None

        return text[:MAX_CONTEXT_CHARS], page.fullurl, title

    except Exception as exc:
        logger.warning("Failed to fetch Wikipedia page '%s': %s", title, exc)
        return None


# ══════════════════════════════════════════════════════════════════════════
# LangGraph Node
# ══════════════════════════════════════════════════════════════════════════

async def wiki_verify(state: QueryState) -> dict[str, Any]:
    """
    LangGraph node: fetch Wikipedia evidence for claims routed to "wikipedia" or "both".

    Reads from state:
        - classified_claims: list of ClassifiedClaimDict

    Returns:
        - wiki_evidence: list of ClaimEvidenceDict (one per wiki-routed claim)
        - status: "verifying"

    Only processes claims where route is "wikipedia" or "both". Skips the rest.
    """
    classified_claims = state.get("classified_claims", [])
    wiki_claims = [
        c for c in classified_claims
        if c.get("route") in ("wikipedia", "both")
    ]

    if not wiki_claims:
        logger.info("No claims routed to Wikipedia verification.")
        return {"wiki_evidence": [], "status": "verifying"}

    logger.info("Fetching Wikipedia evidence for %d claims...", len(wiki_claims))

    evidence_list: list[ClaimEvidenceDict] = []

    for claim in wiki_claims:
        claim_text = claim["claim_text"]
        entities = claim.get("entities", [])

        # Search Wikipedia for relevant articles
        import asyncio
        titles = await asyncio.to_thread(_search_wikipedia_for_claim, claim_text, entities)

        if not titles:
            logger.info("  No Wikipedia articles found for: %s", claim_text[:40])
            evidence: ClaimEvidenceDict = {
                "claim_text": claim_text,
                "route": claim["route"],
                "wiki_context": "",
                "wiki_url": "",
                "wiki_title": "",
            }
            evidence_list.append(evidence)
            continue

        # Fetch the best (first) matching page
        page_result = None
        for title in titles:
            page_result = await asyncio.to_thread(_fetch_page_context, title)
            if page_result:
                break

        if page_result:
            text_excerpt, page_url, page_title = page_result
            logger.info(
                "  Wikipedia evidence for %r: %s (%d chars)",
                claim_text[:40], page_title, len(text_excerpt),
            )
            evidence = {
                "claim_text": claim_text,
                "route": claim["route"],
                "wiki_context": text_excerpt,
                "wiki_url": page_url,
                "wiki_title": page_title,
            }
        else:
            logger.info("  No usable Wikipedia page for: %s", claim_text[:40])
            evidence = {
                "claim_text": claim_text,
                "route": claim["route"],
                "wiki_context": "",
                "wiki_url": "",
                "wiki_title": "",
            }

        evidence_list.append(evidence)

    logger.info("Wikipedia verification complete: %d claims processed.", len(evidence_list))

    return {
        "wiki_evidence": evidence_list,
    }
