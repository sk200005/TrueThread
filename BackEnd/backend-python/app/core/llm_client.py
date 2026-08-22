"""
core/llm_client.py — Async wrapper for Groq's OpenAI-compatible LLM API.

Provides a single reusable client for all LLM calls in the Python service
(summarization, claim extraction). Uses the Groq API through the standard
OpenAI Python SDK, since Groq's API is OpenAI-compatible.

Usage:
    from app.core.llm_client import get_llm_client

    client = get_llm_client()
    response_text = await client.chat(
        system_prompt="You are a helpful assistant.",
        user_prompt="Summarize this text...",
    )
"""

from __future__ import annotations

import logging
from typing import Optional

from openai import AsyncOpenAI

from app.core.config import settings

logger = logging.getLogger(__name__)

# Groq made its API compatible with the OpenAI API. — just point base_url at Groq
GROQ_BASE_URL = "https://api.groq.com/openai/v1"


class LLMClient:
    """
    Thin async wrapper around Groq's OpenAI-compatible chat completions API.

    This is NOT responsible for retries — callers should wrap calls with
    tenacity or their own retry logic as needed.
    """

    def __init__(self):
        if not settings.groq_api_key:
            logger.warning("GROQ_API_KEY is not set. LLM calls will fail.")

        # AsyncOpenAI pointed at Groq's endpoint
        self.client = AsyncOpenAI(
            api_key=settings.groq_api_key,
            base_url=GROQ_BASE_URL,
        )
        self.model = settings.groq_model

    async def chat(
        self,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.1,
        max_tokens: int = 1024,
        model: Optional[str] = None,
    ) -> str:
        """
        Send a chat completion request and return the raw text response.

        Args:
            system_prompt: The system message (instructions for the LLM).
            user_prompt:   The user message (the actual content to process).
            temperature:   Controls randomness (0.0 = deterministic, 1.0 = creative).
            max_tokens:    Maximum tokens in the response.
            model:         Override the default model if needed.

        Returns:
            The raw text content of the LLM's response.

        Raises:
            Exception: Any API error (rate limit, timeout, etc.) — caller handles retries.
        """
        try:
            completion = await self.client.chat.completions.create(
                model=model or self.model,
                temperature=temperature,
                max_tokens=max_tokens,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
            )
        except Exception as exc:
            err_str = str(exc).lower()
            if "429" in err_str or "quota" in err_str or "rate limit" in err_str or "resourceexhausted" in err_str:
                logger.warning("Groq rate limit hit, falling back to Mistral...")
                try:
                    from app.core.fallback_client import get_fallback_client
                    fallback_client = get_fallback_client()
                    return await fallback_client.fallback_chat(
                        system_prompt=system_prompt,
                        user_prompt=user_prompt,
                        temperature=temperature,
                        max_tokens=max_tokens,
                        json_mode=False,
                        provider="mistral"
                    )
                except Exception as fallback_exc:
                    logger.error("Fallback to Mistral failed: %s", fallback_exc)
                    raise exc
            raise exc

        # Extract the text from the first choice
        content = completion.choices[0].message.content or ""
        return content.strip()


# ── Singleton ─────────────────────────────────────────────────────────────
# Lazy-loaded so the OpenAI client isn't created at import time.
            #object is created only when it is first needed, not when the module is imported.
_llm_client: Optional[LLMClient] = None      


def get_llm_client() -> LLMClient:        # creates singleton instance of LLMClient
    """Return a shared LLMClient instance (created on first call)."""
    global _llm_client
    if _llm_client is None:
        _llm_client = LLMClient()        # returns the singleton instance to ensures that only one instance of LLMClient is created and reused
                                         # This avoids repeated initialization and gives every part of 
    return _llm_client                   # the application a shared connection object.
