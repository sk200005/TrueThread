"""
graphs/nodes/extract_claims.py — LLM-based claim extraction (ported from JS).

This is a faithful port of BackEnd/reddit-collector/claim-extractor.js
translated to async Python for use as a LangGraph node.

The logic is preserved as-is from the JS version:
  1. Pre-filter chunks (skip empty, too short, pure questions)
  2. Batch 5 chunks per LLM call (amortizes system prompt cost)
  3. Use the exact same SYSTEM_PROMPT and JSON schema
  4. Validate with Pydantic (upgrade over JS — JS had no validation)
  5. Duplicate guard before DB insert

The one structural change: in JS this runs as a standalone CLI against
reddit_comments. Here it runs as a LangGraph node against retrieved_chunks
from the RAG pipeline. The extraction logic is identical.
"""

from __future__ import annotations

import json
import logging
import re      # Regular expression matching - splits text into sentences == re.split(r'[.!?]', text).
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator
from sqlalchemy import text
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
)

from app.core.database import async_session
from app.core.gemini_client import extract_json, get_gemini_client
from app.graphs.state import ExtractedClaimDict, QueryState

logger = logging.getLogger(__name__)

# ── Config (matching JS claim-extractor.js) ───────────────────────────────
BATCH_SIZE = 5           # chunks per LLM call (same as JS)
MIN_WORD_COUNT = 5       # skip chunks with fewer words (same as JS)


# ══════════════════════════════════════════════════════════════════════════
# Pydantic model for claim validation (upgrade over JS version)
# ══════════════════════════════════════════════════════════════════════════
 
class ExtractedClaim(BaseModel):       # This is the expected structure of every AI answer.
    """
    Validates a single claim returned by the LLM.

    Fields match the JSON schema in SYSTEM_PROMPT exactly:
        claim_text, entities, claim_type, confidence, source_comment_id
    """

    claim_text: str
    entities: list[str] = Field(default_factory=list)
    claim_type: str = "opinion"
    confidence: str = "medium"
    source_comment_id: str

    @field_validator("claim_type")
    @classmethod
    def validate_claim_type(cls, v: str) -> str:
        allowed = {"comparison", "effectiveness", "warning", "opinion", "factual"}
        if v not in allowed:
            # Don't crash — just default to "opinion" like the JS version would
            return "opinion"
        return v

    @field_validator("confidence")
    @classmethod
    def validate_confidence(cls, v: str) -> str:
        allowed = {"high", "medium", "low"}
        if v not in allowed:
            return "medium"
        return v


# ══════════════════════════════════════════════════════════════════════════
# Pre-filter (ported verbatim from JS preFilter function)
# ══════════════════════════════════════════════════════════════════════════

def pre_filter(text_content: str) -> tuple[bool, str]:
    """
    Cost gate — skip text that is virtually guaranteed to contain no claims.

    Returns:
        (pass, reason) — pass=True means "send to LLM", pass=False means "skip".

    Preserves the exact same heuristics as the JS version:
        1. Empty text → skip
        2. Under MIN_WORD_COUNT words → skip
        3. Pure question (every sentence ends with '?' or starts with question word) → skip
    """
    text_content = (text_content or "").strip()

    # Empty / null text
    if not text_content:
        return False, "empty text"

    # Under minimum word count
    words = [w for w in text_content.split() if w]
    if len(words) < MIN_WORD_COUNT:
        return False, f"too short ({len(words)} words)"

    # Pure question heuristic (same regex as JS)
    sentences = [s.strip() for s in re.split(r'[.!?\n]+', text_content) if s.strip()]

    question_words_pattern = re.compile(
        r'^(what|who|where|when|why|how|is|are|was|were|do|does|did|can|could|should|would|will)\b',
        re.IGNORECASE
    )

    if sentences:
        is_pure_question = all(
            s.endswith('?') or question_words_pattern.match(s)
            for s in sentences
        )
        if is_pure_question:
            return False, "pure question"

    return True, "ok"


# ══════════════════════════════════════════════════════════════════════════
# System prompt (ported verbatim from JS SYSTEM_PROMPT)
# ══════════════════════════════════════════════════════════════════════════

SYSTEM_PROMPT = (
    'Extract claims from Reddit comments. Return a JSON array only — no prose, no fences.\n'
    'A claim = declarative assertion about an entity (effectiveness, warning, opinion, factual, comparison).\n'
    'Skip: questions, greetings, filler, meta-commentary.\n'
    'Resolve pronouns using parent context if provided; omit claim if reference is unresolvable.\n'
    'Schema per claim: {"claim_text":string,"entities":string[],'
    '"claim_type":"comparison"|"effectiveness"|"warning"|"opinion"|"factual",'
    '"confidence":"high"|"medium"|"low","source_comment_id":string}\n'
    'confidence: high=no hedge, medium="I think"/"seems", low="might"/"could".\n'
    'If no claims, return [].'
)


# ══════════════════════════════════════════════════════════════════════════
# LLM call with retry (tenacity)
# ══════════════════════════════════════════════════════════════════════════

@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=2, min=2, max=30),
    retry=retry_if_exception_type(Exception),
    reraise=True,
)
async def _call_llm_for_claims(user_message: str) -> str:
    """
    Call the LLM with the claim extraction prompt.
    Wrapped with tenacity for automatic retry with exponential backoff.
    """
    client = get_gemini_client()
    return await client.chat(
        system_prompt=SYSTEM_PROMPT,
        user_prompt=user_message,
        temperature=0.1,
        max_tokens=8192,
        json_mode=True,
    )


def _build_user_message(batch: list[dict[str, str]]) -> str:
    """
    Build the multi-chunk user message for the LLM.

    Format matches the JS version: each chunk gets [ID:xxx] followed by its text,
    separated by '---' dividers.

    Args:
        batch: List of dicts with 'id' and 'text' keys.
    """
    entries = []
    for item in batch:
        entry = f"[ID:{item['id']}]\n{item['text']}"
        entries.append(entry)
    return "\n---\n".join(entries)


def _parse_claims_response(raw_response: str) -> tuple[list[ExtractedClaim], Optional[str]]:
    """
    Parse the LLM's JSON response into validated ExtractedClaim objects.

    Returns:
        (claims, parse_error) — parse_error is None on success, error message on failure.

    Handles the same edge cases as the JS version:
        - Strips markdown code fences (```json ... ```)
        - Handles empty responses
        - Validates each claim individually (skip invalid ones instead of failing all)
    """
    if not raw_response or not raw_response.strip():
        return [], "empty response"

    try:
        parsed = extract_json(raw_response)
    except json.JSONDecodeError as e:
        return [], str(e)

    if isinstance(parsed, dict):
        # OpenRouter's json_object format forces a dict, e.g. {"claims": [...]}
        # Find the first list value inside the dict and use that.
        for value in parsed.values():
            if isinstance(value, list):
                parsed = value
                break

    if not isinstance(parsed, list):
        return [], f"expected array, got {type(parsed).__name__}"

    # Validate each claim with Pydantic — skip invalid ones instead of failing
    claims: list[ExtractedClaim] = []
    for i, raw_claim in enumerate(parsed):
        try:
            claim = ExtractedClaim.model_validate(raw_claim)
            claims.append(claim)
        except Exception as e:
            logger.warning("Skipping invalid claim at index %d: %s", i, e)
            continue

    return claims, None


# ══════════════════════════════════════════════════════════════════════════
# DB persistence (matching JS saveClaimsToDb)
# ══════════════════════════════════════════════════════════════════════════

async def _save_claims_to_db(
    source_comment_id: str,
    claims: list[ExtractedClaim],
    source_platform: str,
    raw_llm_response: str,
) -> tuple[int, bool]:
    """
    Persist claims for a single source_comment_id to the extracted_claims table.

    Duplicate guard: if any row already exists for this source_comment_id,
    skip ALL inserts for that comment. Safe to re-run.

    Returns:
        (inserted_count, was_skipped)
    """
    async with async_session() as session:
        # Duplicate guard — check if already persisted
        result = await session.execute(
            text("SELECT 1 FROM extracted_claims WHERE source_comment_id = :scid LIMIT 1"),
            {"scid": source_comment_id},
        )
        if result.scalar_one_or_none() is not None:
            logger.info("  [DB] %s already persisted, skipping.", source_comment_id)
            return 0, True

        if not claims:
            return 0, False

        # Insert all claims in one transaction
        for claim in claims:
            # Safely serialize raw_llm_response as JSONB
            try:
                raw_json = json.dumps(json.loads(raw_llm_response)) if raw_llm_response else None
            except (json.JSONDecodeError, TypeError):
                raw_json = None

            await session.execute(
                text("""
                    INSERT INTO extracted_claims
                        (claim_text, entities, claim_type, direction, confidence,
                         is_sincere, source_comment_id, source_platform, raw_llm_response)
                    VALUES (:claim_text, :entities, :claim_type, :direction, :confidence,
                            :is_sincere, :source_comment_id, :source_platform, :raw_llm_response)
                """),
                {
                    "claim_text": claim.claim_text,
                    "entities": json.dumps(claim.entities),
                    "claim_type": claim.claim_type,
                    "direction": None,        # Not in the current LLM schema
                    "confidence": claim.confidence,
                    "is_sincere": True,
                    "source_comment_id": source_comment_id,
                    "source_platform": source_platform,
                    "raw_llm_response": raw_json,
                },
            )

        await session.commit()
        logger.info("  [DB] Inserted %d claim(s) for %s.", len(claims), source_comment_id)
        return len(claims), False


# ══════════════════════════════════════════════════════════════════════════
# LangGraph Node
# ══════════════════════════════════════════════════════════════════════════

async def extract_claims(state: QueryState) -> dict[str, Any]:
    """
    LangGraph node: extract claims from retrieved chunks.

    Reads from state:
        - retrieved_chunks: List of RetrievedChunk dicts from rag_retrieve

    Returns:
        - extracted_claims: List of ExtractedClaimDict dicts
        - status: "extracting"

    Flow (matching JS claim-extractor.js):
        1. Pre-filter each chunk
        2. Batch eligible chunks (5 per LLM call)
        3. Call LLM, parse response, validate with Pydantic
        4. Save to DB with duplicate guard
        5. Return all extracted claims in state
    """
    retrieved_chunks = state.get("retrieved_chunks", [])

    if not retrieved_chunks:
        logger.info("No retrieved chunks to extract claims from.")
        return {"extracted_claims": [], "status": "extracting"}

    logger.info("Starting claim extraction on %d retrieved chunks...", len(retrieved_chunks))

    # ── Pre-filter pass ──────────────────────────────────────────────────
    eligible: list[dict[str, str]] = []
    filtered_count = 0

    for chunk in retrieved_chunks:
        passes, reason = pre_filter(chunk["chunk_text"])
        if not passes:
            logger.info("SKIP [%s] -- %s", chunk["chunk_id"][:8], reason)
            filtered_count += 1
        else:
            eligible.append({
                "id": chunk["chunk_id"],
                "text": chunk["chunk_text"],
            })

    logger.info(
        "%d chunks pass pre-filter (skipped %d). Sending in batches of %d...",
        len(eligible), filtered_count, BATCH_SIZE,
    )

    # ── Batch LLM calls ─────────────────────────────────────────────────
    all_claims: list[ExtractedClaimDict] = []
    total_inserted = 0
    total_skipped_dup = 0
    parse_errors = 0

    for i in range(0, len(eligible), BATCH_SIZE):
        batch = eligible[i : i + BATCH_SIZE]
        batch_num = i // BATCH_SIZE + 1
        total_batches = (len(eligible) + BATCH_SIZE - 1) // BATCH_SIZE

        logger.info(
            "Batch %d/%d — chunks: %s",
            batch_num, total_batches,
            [item["id"][:8] for item in batch],
        )

        # Build user message and call LLM
        user_message = _build_user_message(batch)

        try:
            raw_response = await _call_llm_for_claims(user_message)
        except Exception as exc:
            err_str = str(exc).lower()
            if "429" in err_str or "quota" in err_str or "rate limit" in err_str or "resourceexhausted" in err_str:
                raise Exception("LLM API rate limit reached. Please wait and try again.") from exc
                
            logger.error("LLM call failed after retries for batch %d: %s", batch_num, exc)
            parse_errors += 1
            continue

        # Parse and validate
        claims, parse_error = _parse_claims_response(raw_response)

        if parse_error:
            logger.warning("Parse error in batch %d: %s", batch_num, parse_error)
            parse_errors += 1
        elif not claims:
            logger.info("No claims extracted in batch %d.", batch_num)
        else:
            logger.info("%d claim(s) extracted in batch %d.", len(claims), batch_num)

            # Convert to state dicts and save to DB
            # Group by source_comment_id (same pattern as JS)
            by_comment: dict[str, list[ExtractedClaim]] = {}
            for claim in claims:
                cid = claim.source_comment_id
                if cid not in by_comment:
                    by_comment[cid] = []
                by_comment[cid].append(claim)

            for cid, comment_claims in by_comment.items():
                inserted, skipped = await _save_claims_to_db(
                    cid, comment_claims, "wikipedia", raw_response,
                )
                total_inserted += inserted
                if skipped:
                    total_skipped_dup += len(comment_claims)


            # Add to state output
            for claim in claims:
                claim_dict: ExtractedClaimDict = {
                    "claim_text": claim.claim_text,
                    "entities": claim.entities,
                    "claim_type": claim.claim_type,
                    "confidence": claim.confidence,
                    "source_comment_id": claim.source_comment_id,
                }
                all_claims.append(claim_dict)

    # ── Summary ──────────────────────────────────────────────────────────
    logger.info("=" * 60)
    logger.info("Claim Extraction Summary:")
    logger.info("  Total chunks retrieved    : %d", len(retrieved_chunks))
    logger.info("  Skipped by pre-filter     : %d", filtered_count)
    logger.info("  Parse errors              : %d", parse_errors)
    logger.info("  Total claims extracted    : %d", len(all_claims))
    logger.info("  Claims newly inserted     : %d", total_inserted)
    logger.info("  Claims skipped (duplicate): %d", total_skipped_dup)
    logger.info("=" * 60)

    return {
        "extracted_claims": all_claims,
        "status": "extracting",
    }
