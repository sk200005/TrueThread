"""
graphs/nodes/summarize.py — Summarization node for the query-time graph.

Takes retrieved_chunks (and optionally extracted_claims) from state,
sends them to the LLM with a structured prompt, and produces a JSON
report containing:
    - overall_sentiment: positive | negative | mixed | neutral
    - themes: array of { theme, supporting_chunk_ids }
    - summary: 2-3 sentence overview

The result is written to the `reports` table (JSONB columns) and stored
in state as `final_report`.

Defensive parsing: strips markdown code fences, wraps JSON.loads in
try/except, returns a fallback report on malformed output instead of crashing.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from typing import Any

from sqlalchemy import text

from app.core.database import async_session
from app.core.llm_client import get_llm_client
from app.graphs.state import QueryState

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════
# Prompt
# ══════════════════════════════════════════════════════════════════════════

SUMMARIZE_SYSTEM_PROMPT = """You are a research analyst. Analyze the following text chunks retrieved from various sources and produce a structured JSON report.

Return STRICT JSON only — no markdown fences, no prose before or after the JSON.

Required JSON schema:
{
  "overall_sentiment": "positive" | "negative" | "mixed" | "neutral",
  "themes": [
    {
      "theme": "string — a short theme label",
      "supporting_chunk_ids": ["chunk_id_1", "chunk_id_2"]
    }
  ],
  "summary": "string — 2-3 sentence overview of findings"
}

Rules:
- overall_sentiment must be exactly one of: positive, negative, mixed, neutral
- Each theme should be a distinct topic or pattern found across the chunks
- supporting_chunk_ids must reference actual chunk IDs from the input
- summary should be concise and factual, synthesizing the key findings
- If claims are provided, incorporate them into the themes and summary
- If the text is insufficient for analysis, return neutral sentiment with a single theme"""


def _build_user_prompt(
    query: str,
    chunks: list[dict[str, Any]],
    claims: list[dict[str, Any]] | None = None,
) -> str:
    """
    Build the user message with all the context for the LLM.

    Includes:
        - The original user query
        - Each retrieved chunk with its ID (so the LLM can reference them)
        - Any extracted claims (if available)
    """
    parts = [f"User's research query: \"{query}\"\n"]

    # Add retrieved chunks
    parts.append("=== RETRIEVED TEXT CHUNKS ===\n")
    for i, chunk in enumerate(chunks):
        chunk_id = chunk.get("chunk_id", f"chunk_{i}")
        similarity = chunk.get("similarity", 0.0)
        text_content = chunk.get("chunk_text", "")
        parts.append(f"[Chunk ID: {chunk_id}] (relevance: {similarity:.3f})")
        parts.append(text_content)
        parts.append("")  # blank line separator

    # Add claims if available
    if claims:
        parts.append("\n=== EXTRACTED CLAIMS ===\n")
        for claim in claims:
            parts.append(
                f"- [{claim.get('claim_type', 'unknown')}] "
                f"{claim.get('claim_text', '')} "
                f"(confidence: {claim.get('confidence', 'unknown')}, "
                f"entities: {claim.get('entities', [])})"
            )

    return "\n".join(parts)


def _parse_report_response(raw_response: str) -> dict[str, Any] | None:
    """
    Parse the LLM's JSON response defensively.

    Returns the parsed dict on success, None on failure.
    Handles:
        - Markdown code fences (```json ... ```)
        - Empty responses
        - Malformed JSON
    """
    if not raw_response or not raw_response.strip():
        logger.warning("Empty LLM response for summarization.")
        return None

    # Strip markdown code fences
    cleaned = raw_response.strip()
    cleaned = re.sub(r'^```(?:json)?\s*', '', cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r'\s*```$', '', cleaned, flags=re.IGNORECASE)
    cleaned = cleaned.strip()

    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError as e:
        logger.warning("Failed to parse summarization JSON: %s", e)
        logger.debug("Raw response was: %s", raw_response[:500])
        return None

    if not isinstance(parsed, dict):
        logger.warning("Expected dict from LLM, got %s", type(parsed).__name__)
        return None

    return parsed


def _make_fallback_report(query: str) -> dict[str, Any]:
    """
    Return a safe fallback report when the LLM produces invalid output.
    This ensures the pipeline never crashes on bad LLM responses.
    """
    return {
        "overall_sentiment": "neutral",
        "themes": [
            {
                "theme": "Unable to generate analysis",
                "supporting_chunk_ids": [],
            }
        ],
        "summary": (
            f"The analysis for \"{query}\" could not be completed due to "
            "an error in processing the LLM response. Please retry."
        ),
    }


# ══════════════════════════════════════════════════════════════════════════
# DB persistence
# ══════════════════════════════════════════════════════════════════════════

async def _save_report_to_db(query_id: str, report: dict[str, Any]) -> str | None:
    """
    Insert the report into the reports table.

    Uses the JSONB columns: sentiment_summary, themes, verified_claims.
    Returns the new report UUID on success, None on failure.
    """
    report_id = str(uuid.uuid4())

    try:
        async with async_session() as session:
            await session.execute(
                text("""
                    INSERT INTO reports (id, query_id, sentiment_summary, themes, verified_claims)
                    VALUES (:id, :query_id, :sentiment_summary, :themes, :verified_claims)
                    ON CONFLICT (query_id) DO UPDATE SET
                        sentiment_summary = EXCLUDED.sentiment_summary,
                        themes = EXCLUDED.themes,
                        verified_claims = EXCLUDED.verified_claims
                """),
                {
                    "id": report_id,
                    "query_id": query_id,
                    "sentiment_summary": json.dumps({
                        "overall": report.get("overall_sentiment", "neutral"),
                    }),
                    "themes": json.dumps(report.get("themes", [])),
                    "verified_claims": json.dumps(report.get("verified_claims", [])),
                },
            )
            await session.commit()
            logger.info("Report %s saved to DB for query %s.", report_id, query_id)
            return report_id

    except Exception as exc:
        logger.error("Failed to save report to DB: %s", exc)
        return None


# ══════════════════════════════════════════════════════════════════════════
# LangGraph Node
# ══════════════════════════════════════════════════════════════════════════

async def summarize(state: QueryState) -> dict[str, Any]:
    """
    LangGraph node: generate a structured report from retrieved chunks and claims.

    Reads from state:
        - query (str): The user's query
        - query_id (str, optional): For saving the report to the right query
        - retrieved_chunks: List of RetrievedChunk dicts
        - extracted_claims (optional): List of ExtractedClaimDict dicts
        - verified_claims (optional): List of VerifiedClaimDict dicts

    Returns:
        - final_report: The JSON report dict
        - status: "summarizing"
    """
    query = state.get("query", "")
    query_id = state.get("query_id")
    chunks = state.get("retrieved_chunks", [])
    claims = state.get("extracted_claims", [])
    verified_claims = state.get("verified_claims", [])

    if not chunks:
        logger.info("No chunks to summarize.")
        fallback = _make_fallback_report(query)
        fallback["verified_claims"] = []
        if query_id:
            await _save_report_to_db(query_id, fallback)
        return {"final_report": fallback, "status": "summarizing"}

    logger.info(
        "Summarizing %d chunks, %d claims, and %d verified claims for query: %r",
        len(chunks), len(claims), len(verified_claims), query,
    )

    # ── Build prompt and call LLM ────────────────────────────────────────
    user_prompt = _build_user_prompt(query, chunks, claims if claims else None)

    try:
        client = get_llm_client()
        raw_response = await client.chat(
            system_prompt=SUMMARIZE_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            temperature=0.2,      # Slightly higher than extraction for natural summaries
            max_tokens=2048,      # Reports can be longer than claim extraction
        )
    except Exception as exc:
        logger.error("LLM call failed for summarization: %s", exc)
        fallback = _make_fallback_report(query)
        fallback["verified_claims"] = []
        if query_id:
            await _save_report_to_db(query_id, fallback)
        return {"final_report": fallback, "status": "error"}

    # ── Parse response ───────────────────────────────────────────────────
    report = _parse_report_response(raw_response)

    if report is None:
        logger.warning("Using fallback report due to parse failure.")
        report = _make_fallback_report(query)

    # Add the raw query and verified claims for context
    report["query"] = query
    report["verified_claims"] = verified_claims

    logger.info("Report generated:")
    logger.info("  Sentiment : %s", report.get("overall_sentiment", "unknown"))
    logger.info("  Themes    : %d", len(report.get("themes", [])))
    logger.info("  Summary   : %s", report.get("summary", "")[:100])
    logger.info("  Verified Claims: %d", len(report.get("verified_claims", [])))

    # ── Save to DB ───────────────────────────────────────────────────────
    if query_id:
        await _save_report_to_db(query_id, report)
    else:
        logger.info("No query_id in state — skipping DB save (test mode).")

    return {
        "final_report": report,
        "status": "summarizing",
    }
