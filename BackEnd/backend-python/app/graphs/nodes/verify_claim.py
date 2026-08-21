"""
graphs/nodes/verify_claim.py — Verification scoring node (Phase E).

LLM judge node that compares each claim against collected evidence (from
news, Wikipedia, or both) and produces a structured verdict.

Follows the same Pydantic + structured-output + tenacity-retry pattern
as extract_claims.py (is_sincere-style confidence scoring extended into
factual verification).

Entailment rubric (enforced in prompt — do NOT simplify):
    SUPPORTED    — evidence explicitly confirms the SAME SPECIFIC ASSERTION
                   (not just same topic). Confidence 0.8+.
    CONTRADICTED — evidence explicitly states something incompatible
                   (opposing facts/numbers/outcome). Confidence 0.8+.
    UNVERIFIED   — evidence is topically related but doesn't address the
                   specific assertion, or no evidence found.
    DISPUTED     — ONLY when route="both" AND news evidence and Wikipedia
                   evidence produce conflicting signals. Do not silently
                   pick one side.

Confidence grounding:
    0.8 – 1.0 : Direct, unambiguous match between claim and evidence
    0.5 – 0.8 : Requires inference or the evidence is indirect
    < 0.5     : Tangential evidence only

No verdict without a citation — the LLM must cite the specific evidence
snippet justifying the verdict.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from pydantic import BaseModel, Field, field_validator
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
)

from app.core.llm_client import get_llm_client
from app.graphs.state import (
    CitationDict,
    ClaimEvidenceDict,
    QueryState,
    VerifiedClaimDict,
)

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════
# Pydantic model for verification output
# ══════════════════════════════════════════════════════════════════════════

class VerificationCitation(BaseModel):
    """A single source citation backing the verdict."""

    url: str = ""
    title: str = ""
    snippet: str = ""


class VerificationResult(BaseModel):
    """Validated verification result for a single claim."""

    claim: str
    verdict: str = "unverified"
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    source_type: str = "news"
    citations: list[VerificationCitation] = Field(default_factory=list)
    justification: str = ""

    @field_validator("verdict")
    @classmethod
    def validate_verdict(cls, v: str) -> str:
        allowed = {"supported", "contradicted", "unverified", "disputed"}
        if v.lower() not in allowed:
            return "unverified"
        return v.lower()

    @field_validator("source_type")
    @classmethod
    def validate_source_type(cls, v: str) -> str:
        allowed = {"news", "wikipedia", "both"}
        if v not in allowed:
            return "news"
        return v


# ══════════════════════════════════════════════════════════════════════════
# System prompt — strict entailment rubric
# ══════════════════════════════════════════════════════════════════════════

VERIFY_SYSTEM_PROMPT = """You are a fact-checking judge. Given a claim and evidence text(s), produce a verification verdict.

Return a JSON object only — no prose, no markdown fences.

Schema:
{
  "claim": "the original claim text",
  "verdict": "supported" | "contradicted" | "unverified" | "disputed",
  "confidence": <float 0.0 to 1.0>,
  "source_type": "news" | "wikipedia" | "both",
  "citations": [{"url": "...", "title": "...", "snippet": "exact quote from evidence supporting your verdict"}],
  "justification": "1-2 sentence explanation of WHY this verdict was chosen, referencing specific evidence"
}

STRICT ENTAILMENT RUBRIC — follow exactly:

1. SUPPORTED — Use ONLY if the evidence explicitly confirms the SAME SPECIFIC ASSERTION as the claim (not just the same topic). The evidence must state the same fact, number, or outcome.
   Confidence: 0.8+ for direct match, 0.5-0.8 if inference required.

2. CONTRADICTED — Use ONLY if the evidence explicitly states something INCOMPATIBLE with the claim (opposing facts, different numbers, opposite outcome).
   Confidence: 0.8+ for direct contradiction, 0.5-0.8 if implied.

3. UNVERIFIED — Use when:
   - Evidence is topically related but doesn't address the specific assertion
   - No evidence was provided or evidence is empty
   - Evidence is ambiguous and could support either side
   Confidence: should be < 0.5.

4. DISPUTED — Use ONLY when BOTH news AND Wikipedia evidence are provided AND they produce CONFLICTING signals (one supports, other contradicts). Do NOT silently pick one side. Explain the conflict in justification.
   This verdict is ONLY valid when source_type="both".

CRITICAL RULES:
- Every verdict MUST have at least one citation with a non-empty snippet from the evidence.
- If no evidence is provided, verdict MUST be "unverified" with confidence < 0.3.
- Do NOT inflate confidence. 0.8+ = direct unambiguous match. 0.5-0.8 = requires inference. <0.5 = tangential.
- "disputed" is ONLY for source_type="both" with genuinely conflicting evidence. Never use it for single-source verdicts.
"""


# ══════════════════════════════════════════════════════════════════════════
# LLM call with retry
# ══════════════════════════════════════════════════════════════════════════

@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=2, min=2, max=30),
    retry=retry_if_exception_type(Exception),
    reraise=True,
)
async def _call_llm_for_verification(user_message: str) -> str:
    """Call the LLM with the verification prompt. Retries with backoff."""
    client = get_llm_client()
    return await client.chat(
        system_prompt=VERIFY_SYSTEM_PROMPT,
        user_prompt=user_message,
        temperature=0.1,
        max_tokens=1024,
    )


def _build_verification_prompt(
    claim_text: str,
    news_evidence: ClaimEvidenceDict | None,
    wiki_evidence: ClaimEvidenceDict | None,
) -> tuple[str, str]:
    """
    Build the user message for verification and determine source_type.

    Returns:
        (user_prompt, source_type)
    """
    parts = [f"CLAIM: {claim_text}\n"]
    has_news = False
    has_wiki = False

    # Add news evidence if available
    if news_evidence and news_evidence.get("news_articles"):
        has_news = True
        parts.append("=== NEWS EVIDENCE ===")
        for article in news_evidence["news_articles"]:
            parts.append(f"[{article.get('source_name', 'Unknown')}] {article.get('title', '')}")
            parts.append(f"URL: {article.get('url', '')}")
            parts.append(f"Published: {article.get('published_at', 'Unknown')}")
            parts.append(f"Content: {article.get('snippet', '')}")
            parts.append("")

    # Add Wikipedia evidence if available
    if wiki_evidence and wiki_evidence.get("wiki_context"):
        has_wiki = True
        parts.append("=== WIKIPEDIA EVIDENCE ===")
        parts.append(f"Article: {wiki_evidence.get('wiki_title', 'Unknown')}")
        parts.append(f"URL: {wiki_evidence.get('wiki_url', '')}")
        parts.append(f"Content: {wiki_evidence.get('wiki_context', '')}")
        parts.append("")

    if not has_news and not has_wiki:
        parts.append("=== NO EVIDENCE FOUND ===")
        parts.append("No news articles or Wikipedia content was found for this claim.")

    # Determine source_type
    if has_news and has_wiki:
        source_type = "both"
    elif has_wiki:
        source_type = "wikipedia"
    else:
        source_type = "news"

    return "\n".join(parts), source_type


def _parse_verification_response(raw_response: str) -> VerificationResult | None:
    """Parse the LLM's JSON verification response."""
    if not raw_response or not raw_response.strip():
        return None

    cleaned = raw_response.strip()
    cleaned = re.sub(r'^```(?:json)?\s*', '', cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r'\s*```$', '', cleaned, flags=re.IGNORECASE)

    try:
        parsed = json.loads(cleaned.strip())
    except json.JSONDecodeError as e:
        logger.warning("Failed to parse verification JSON: %s", e)
        return None

    if not isinstance(parsed, dict):
        logger.warning("Expected dict from verification LLM, got %s", type(parsed).__name__)
        return None

    try:
        return VerificationResult.model_validate(parsed)
    except Exception as e:
        logger.warning("Verification result validation failed: %s", e)
        return None


# ══════════════════════════════════════════════════════════════════════════
# LangGraph Node
# ══════════════════════════════════════════════════════════════════════════

async def verify_claim(state: QueryState) -> dict[str, Any]:
    """
    LangGraph node: score each verifiable claim against collected evidence.

    Reads from state:
        - classified_claims: list of ClassifiedClaimDict
        - news_evidence: list of ClaimEvidenceDict (from news_verify)
        - wiki_evidence: list of ClaimEvidenceDict (from wiki_verify)

    Returns:
        - verified_claims: list of VerifiedClaimDict
        - status: "verifying"

    Claims routed to "skip" are OMITTED from verified_claims entirely
    (no placeholder entries). Only verifiable claims get scored.
    """
    classified_claims = state.get("classified_claims", [])
    news_evidence_list = state.get("news_evidence", [])
    wiki_evidence_list = state.get("wiki_evidence", [])

    # Build lookup maps: claim_text → evidence
    news_by_claim: dict[str, ClaimEvidenceDict] = {}
    for ev in news_evidence_list:
        news_by_claim[ev.get("claim_text", "")] = ev

    wiki_by_claim: dict[str, ClaimEvidenceDict] = {}
    for ev in wiki_evidence_list:
        wiki_by_claim[ev.get("claim_text", "")] = ev

    # Filter to only verifiable claims (skip = omitted entirely)
    verifiable_claims = [
        c for c in classified_claims
        if c.get("route") != "skip"
    ]

    if not verifiable_claims:
        logger.info("No verifiable claims to score.")
        return {"verified_claims": [], "status": "verifying"}

    logger.info("Scoring %d verifiable claims...", len(verifiable_claims))

    verified_claims: list[VerifiedClaimDict] = []

    for claim in verifiable_claims:
        claim_text = claim["claim_text"]
        route = claim.get("route", "skip")

        # Find matching evidence
        news_ev = news_by_claim.get(claim_text) if route in ("news", "both") else None
        wiki_ev = wiki_by_claim.get(claim_text) if route in ("wikipedia", "both") else None

        # Build prompt
        user_prompt, source_type = _build_verification_prompt(
            claim_text, news_ev, wiki_ev,
        )

        try:
            raw_response = await _call_llm_for_verification(user_prompt)
            result = _parse_verification_response(raw_response)
        except Exception as exc:
            logger.error("Verification LLM call failed for claim %r: %s", claim_text[:40], exc)
            result = None
            
        import asyncio
        await asyncio.sleep(4)

        if result:
            # Enforce: "disputed" only valid for source_type="both"
            if result.verdict == "disputed" and source_type != "both":
                logger.warning(
                    "LLM returned 'disputed' for single-source claim, forcing 'unverified': %s",
                    claim_text[:40],
                )
                result.verdict = "unverified"

            verified: VerifiedClaimDict = {
                "claim": result.claim or claim_text,
                "verdict": result.verdict,
                "confidence": result.confidence,
                "source_type": source_type,
                "citations": [
                    {"url": c.url, "title": c.title, "snippet": c.snippet}
                    for c in result.citations
                ],
                "justification": result.justification,
            }
        else:
            # Fallback: unverified with low confidence
            verified = {
                "claim": claim_text,
                "verdict": "unverified",
                "confidence": 0.2,
                "source_type": source_type,
                "citations": [],
                "justification": "Verification scoring failed due to LLM error.",
            }

        verified_claims.append(verified)
        logger.info(
            "  [%s] (%.2f) %s — %s",
            verified["verdict"],
            verified["confidence"],
            claim_text[:50],
            verified["justification"][:60],
        )

    logger.info("Verification complete: %d claims scored.", len(verified_claims))

    return {
        "verified_claims": verified_claims,
        "status": "verifying",
    }
