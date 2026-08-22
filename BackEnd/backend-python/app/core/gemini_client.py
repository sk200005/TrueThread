"""
core/gemini_client.py — Async wrapper for Google Gemini API.

Provides a reusable client for heavy-reasoning LLM calls (claim extraction,
verification scoring, summarization, chat). Uses the google-genai SDK.

Shares the same chat() interface as LLMClient so callers can swap trivially.

Usage:
    from app.core.gemini_client import get_gemini_client

    client = get_gemini_client()
    response_text = await client.chat(
        system_prompt="You are a helpful assistant.",
        user_prompt="Summarize this text...",
    )
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from google import genai
from google.genai import types

from app.core.config import settings

logger = logging.getLogger(__name__)


def _response_text(response: Any) -> str:
    """Pull visible text from a Gemini response without raising on empty parts."""
    try:
        text = response.text
        if text:
            return text.strip()
    except Exception as exc:
        logger.warning("response.text unavailable: %s", exc)

    parts: list[str] = []
    for candidate in getattr(response, "candidates", None) or []:
        content = getattr(candidate, "content", None)
        for part in getattr(content, "parts", None) or []:
            part_text = getattr(part, "text", None)
            if part_text:
                parts.append(part_text)
    return "".join(parts).strip()


def extract_json(raw: str) -> Any:
    """Parse JSON from an LLM string, stripping fences and surrounding prose."""
    import json
    import re

    if not raw or not raw.strip():
        raise json.JSONDecodeError("empty response", raw or "", 0)

    cleaned = raw.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned, flags=re.IGNORECASE)
    cleaned = cleaned.strip()

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    for opener, closer in (("[", "]"), ("{", "}")):
        start = cleaned.find(opener)
        end = cleaned.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(cleaned[start : end + 1])
            except json.JSONDecodeError:
                continue

    return json.loads(cleaned)


class GeminiClient:
    """
    Thin async wrapper around Google Gemini's chat completions API.

    Same interface as LLMClient (Groq) — callers just swap the import.
    """

    def __init__(self):
        if not settings.gemini_api_key:
            logger.warning("GEMINI_API_KEY is not set. Gemini LLM calls will fail.")

        self.client = genai.Client(api_key=settings.gemini_api_key)
        self.model = settings.gemini_model

    async def chat(
        self,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.1,
        max_tokens: int = 8192,
        model: Optional[str] = None,
        json_mode: bool = False,
    ) -> str:
        """
        Send a chat completion request to Gemini and return the raw text response.

        Args:
            system_prompt: The system message (instructions for the LLM).
            user_prompt:   The user message (the actual content to process).
            temperature:   Controls randomness (0.0 = deterministic, 1.0 = creative).
            max_tokens:    Maximum tokens in the response (output, not thinking).
            model:         Override the default model if needed.
            json_mode:     Request application/json so the model returns parseable JSON.

        Returns:
            The raw text content of the LLM's response.

        Raises:
            Exception: Any API error — caller handles retries.
        """
        config_kwargs: dict = {
            "system_instruction": system_prompt,
            "temperature": temperature,
            "max_output_tokens": max_tokens,
            "automatic_function_calling": types.AutomaticFunctionCallingConfig(
                disable=True,
            ),
        }
        if json_mode:
            config_kwargs["response_mime_type"] = "application/json"

        # gemini-3.x thinking models spend output budget on hidden reasoning,
        # which truncated JSON and produced fallback reports. Disable thinking
        # when the model allows it; fall back if the API rejects budget=0.
        thinking_attempts = (
            {"thinking_config": types.ThinkingConfig(thinking_budget=0)},
            {},
        )

        last_exc: Exception | None = None
        response = None
        for extra in thinking_attempts:
            try:
                response = await self.client.aio.models.generate_content(
                    model=model or self.model,
                    contents=user_prompt,
                    config=types.GenerateContentConfig(**config_kwargs, **extra),
                )
                last_exc = None
                break
            except Exception as exc:
                last_exc = exc
                logger.warning("Gemini generate_content failed (%s); retrying config.", exc)
        if last_exc is not None:
            err_str = str(last_exc).lower()
            if "429" in err_str or "quota" in err_str or "rate limit" in err_str or "resourceexhausted" in err_str:
                logger.warning("Gemini rate limit hit, falling back to OpenRouter...")
                try:
                    from app.core.fallback_client import get_fallback_client
                    fallback_client = get_fallback_client()
                    return await fallback_client.fallback_chat(
                        system_prompt=system_prompt,
                        user_prompt=user_prompt,
                        temperature=temperature,
                        max_tokens=max_tokens,
                        json_mode=json_mode,
                        provider="openrouter"
                    )
                except Exception as fallback_exc:
                    logger.error("Fallback to OpenRouter failed: %s", fallback_exc)
                    # raise original exception if fallback fails
                    raise last_exc
            raise last_exc

        finish_reason = None
        if response.candidates:
            finish_reason = getattr(response.candidates[0], "finish_reason", None)
        usage = getattr(response, "usage_metadata", None)
        logger.info(
            "Gemini %s finish_reason=%s usage=%s",
            model or self.model,
            finish_reason,
            usage,
        )

        content = _response_text(response)
        if finish_reason and str(finish_reason) not in ("FinishReason.STOP", "STOP", "1"):
            logger.warning(
                "Gemini finished with %s; response may be truncated (%d chars).",
                finish_reason,
                len(content),
            )
        return content


# ── Singleton ─────────────────────────────────────────────────────────────
_gemini_client: Optional[GeminiClient] = None


def get_gemini_client() -> GeminiClient:
    """Return a shared GeminiClient instance (created on first call)."""
    global _gemini_client
    if _gemini_client is None:
        _gemini_client = GeminiClient()
    return _gemini_client
