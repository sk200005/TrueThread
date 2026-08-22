"""
graphs/nodes/classify_claim.py — Claim classifier for verification routing (Phase E).

Takes the full list of extracted_claims from QueryState and classifies each one
in a SINGLE batched LLM call. For each claim, outputs:
    - verifiable: bool — false for pure opinion/recommendation/subjective claims
    - time_nature: "current" | "historical" | "both"
    - route: "news" | "wikipedia" | "both" | "skip"

Routing logic:
    - verifiable=False → route="skip" (don't force verification on opinions)
    - time_nature="current" → route="news"
    - time_nature="historical" → route="wikipedia"
    - time_nature="both" → route="both"

Follows the same Pydantic + structured-output + tenacity-retry pattern
as extract_claims.py.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from pydantic import BaseModel, field_validator
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
)

from app.core.llm_client import get_llm_client
from app.graphs.state import ClassifiedClaimDict, QueryState

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════
# Pydantic model for classification validation
# ══════════════════════════════════════════════════════════════════════════

class ClaimClassification(BaseModel):
    """Validates a single claim classification returned by the LLM."""

    claim_index: int                    # Index into the input claims array
    verifiable: bool
    time_nature: str = "current"
    route: str = "skip"

    @field_validator("time_nature")
    @classmethod
    def validate_time_nature(cls, v: str) -> str:
        allowed = {"current", "historical", "both"}
        if v not in allowed:
            return "current"
        return v

    @field_validator("route")
    @classmethod
    def validate_route(cls, v: str) -> str:
        allowed = {"news", "wikipedia", "both", "skip"}
        if v not in allowed:
            return "skip"
        return v


# ══════════════════════════════════════════════════════════════════════════
# System prompt
# ══════════════════════════════════════════════════════════════════════════

CLASSIFY_SYSTEM_PROMPT = """You are a claim classification engine. Given a list of claims, classify each one for fact-checking routing.

Return a JSON array only — no prose, no markdown fences.

For each claim, output:
{
  "claim_index": <int — 0-based index matching the input list>,
  "verifiable": <bool — false for pure opinion, recommendation, subjective preference, or value judgment>,
  "time_nature": "current" | "historical" | "both",
  "route": "news" | "wikipedia" | "both" | "skip"
}

Classification rules:
- verifiable=false → route MUST be "skip". Do NOT force verification on subjective claims.
- verifiable=true AND time_nature="current" → route="news" (recent events, ongoing trends, product releases within last ~6 months)
- verifiable=true AND time_nature="historical" → route="wikipedia" (established facts, historical events, scientific consensus)
- verifiable=true AND time_nature="both" → route="both" (claims spanning both recent and historical context)

Examples of NON-verifiable claims (route="skip"):
- "I think Product X is better than Product Y" — subjective preference
- "You should try using Product X" — recommendation
- "Product X feels premium" — subjective experience

Examples of verifiable claims:
- "Product X was released in 2024" — historical, factual → route="wikipedia"
- "Product X has been recalled due to safety issues" — current event → route="news"
- "Product X uses technology invented in 1990 and was updated last month" → route="both"

If no claims are provided or all are empty, return [].
"""


# ══════════════════════════════════════════════════════════════════════════
# LLM call with retry (tenacity)
# ══════════════════════════════════════════════════════════════════════════

@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=2, min=2, max=30),
    retry=retry_if_exception_type(Exception),
    reraise=True,
)
async def _call_llm_for_classification(user_message: str) -> str:
    """Call the LLM with the classification prompt. Retries with backoff."""
    client = get_llm_client()
    return await client.chat(
        system_prompt=CLASSIFY_SYSTEM_PROMPT,
        user_prompt=user_message,
        temperature=0.1,
        max_tokens=1024,
    )


def _build_user_message(claims: list[dict[str, Any]]) -> str:
    """
    Build the user message listing all claims to classify.

    Each claim gets an index so the LLM can reference them in the output.
    """
    parts = ["Classify the following claims:\n"]
    for i, claim in enumerate(claims):
        claim_text = claim.get("claim_text", "")
        claim_type = claim.get("claim_type", "unknown")
        entities = claim.get("entities", [])
        parts.append(
            f"[{i}] (type={claim_type}, entities={entities}) {claim_text}"
        )
    return "\n".join(parts)


def _parse_classification_response(
    raw_response: str,
    num_claims: int,
) -> tuple[list[ClaimClassification], str | None]:
    """
    Parse the LLM's JSON response into validated ClaimClassification objects.

    Returns:
        (classifications, parse_error) — parse_error is None on success.
    """
    if not raw_response or not raw_response.strip():
        return [], "empty response"

    # Strip markdown code fences (same pattern as extract_claims.py)
    cleaned = raw_response.strip()
    cleaned = re.sub(r'^```(?:json)?\s*', '', cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r'\s*```$', '', cleaned, flags=re.IGNORECASE)
    cleaned = cleaned.strip()

    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError as e:
        return [], str(e)

    if isinstance(parsed, dict):
        for value in parsed.values():
            if isinstance(value, list):
                parsed = value
                break

    if not isinstance(parsed, list):
        return [], f"expected array, got {type(parsed).__name__}"

    classifications: list[ClaimClassification] = []
    for i, raw_item in enumerate(parsed):
        try:
            classification = ClaimClassification.model_validate(raw_item)
            # Enforce: if not verifiable, route must be skip
            if not classification.verifiable:
                classification.route = "skip"
            classifications.append(classification)
        except Exception as e:
            logger.warning("Skipping invalid classification at index %d: %s", i, e)
            continue

    return classifications, None


# ══════════════════════════════════════════════════════════════════════════
# LangGraph Node
# ══════════════════════════════════════════════════════════════════════════

async def classify_claims(state: QueryState) -> dict[str, Any]:
    """
    LangGraph node: classify all extracted claims for verification routing.

    Reads from state:
        - extracted_claims: list of ExtractedClaimDict from extract_claims

    Returns:
        - classified_claims: list of ClassifiedClaimDict (original fields + routing)
        - status: "classifying"
    """
    extracted_claims = state.get("extracted_claims", [])

    if not extracted_claims:
        logger.info("No extracted claims to classify.")
        return {"classified_claims": [], "status": "classifying"}

    logger.info("Classifying %d extracted claims...", len(extracted_claims))

    # ── Build message and call LLM ───────────────────────────────────────
    user_message = _build_user_message(extracted_claims)

    try:
        raw_response = await _call_llm_for_classification(user_message)
    except Exception as exc:
        err_str = str(exc).lower()
        if "429" in err_str or "quota" in err_str or "rate limit" in err_str or "resourceexhausted" in err_str:
            raise Exception("LLM API rate limit reached. Please wait and try again.") from exc
            
        logger.error("LLM classification failed after retries: %s", exc)
        # Fallback: route everything to "skip" so pipeline doesn't crash
        classified: list[ClassifiedClaimDict] = []
        for claim in extracted_claims:
            classified.append({
                **claim,
                "verifiable": False,
                "time_nature": "current",
                "route": "skip",
            })
        return {"classified_claims": classified, "status": "classifying"}

    # ── Parse response ───────────────────────────────────────────────────
    classifications, parse_error = _parse_classification_response(
        raw_response, len(extracted_claims)
    )

    if parse_error:
        logger.warning("Classification parse error: %s", parse_error)

    # ── Merge classifications back into claim dicts ───────────────────────
    # Build a lookup by claim_index
    classification_map: dict[int, ClaimClassification] = {}
    for c in classifications:
        classification_map[c.claim_index] = c

    classified_claims: list[ClassifiedClaimDict] = []
    for i, claim in enumerate(extracted_claims):
        classification = classification_map.get(i)

        if classification:
            classified_claim: ClassifiedClaimDict = {
                "claim_text": claim["claim_text"],
                "entities": claim.get("entities", []),
                "claim_type": claim.get("claim_type", "opinion"),
                "confidence": claim.get("confidence", "medium"),
                "source_comment_id": claim.get("source_comment_id", ""),
                "verifiable": classification.verifiable,
                "time_nature": classification.time_nature,
                "route": classification.route,
            }
        else:
            # LLM missed this claim — default to skip
            classified_claim = {
                "claim_text": claim["claim_text"],
                "entities": claim.get("entities", []),
                "claim_type": claim.get("claim_type", "opinion"),
                "confidence": claim.get("confidence", "medium"),
                "source_comment_id": claim.get("source_comment_id", ""),
                "verifiable": False,
                "time_nature": "current",
                "route": "skip",
            }

        classified_claims.append(classified_claim)

    # ── Cap verifiable claims ────────────────────────────────────────────
    MAX_CLAIMS_TO_VERIFY = 3
    
    def sort_key(c: dict[str, Any]) -> int:
        conf = c.get("confidence", "low").lower()
        if conf == "high": return 3
        if conf == "medium": return 2
        return 1

    verifiable_indices = [
        i for i, c in enumerate(classified_claims) 
        if c.get("route") != "skip"
    ]
    verifiable_indices.sort(key=lambda i: sort_key(classified_claims[i]), reverse=True)
    
    if len(verifiable_indices) > MAX_CLAIMS_TO_VERIFY:
        logger.info("Capping verifiable claims to %d (was %d)", MAX_CLAIMS_TO_VERIFY, len(verifiable_indices))
        for idx in verifiable_indices[MAX_CLAIMS_TO_VERIFY:]:
            classified_claims[idx]["route"] = "skip"
            classified_claims[idx]["verifiable"] = False
            logger.info("  Capped claim (set to skip): %s", classified_claims[idx]["claim_text"][:60])

    # ── Log summary ──────────────────────────────────────────────────────
    route_counts: dict[str, int] = {}
    for c in classified_claims:
        route_counts[c["route"]] = route_counts.get(c["route"], 0) + 1

    logger.info("Classification summary: %s", route_counts)
    for c in classified_claims:
        logger.info(
            "  [%s] %s (verifiable=%s, time=%s)",
            c["route"], c["claim_text"][:60], c["verifiable"], c["time_nature"],
        )

    return {
        "classified_claims": classified_claims,
        "status": "classifying",
    }
