"""
core/fallback_client.py — Fallback client using Mistral and OpenRouter.

Provides seamless backup when Gemini or Groq hit rate limits.
Uses the standard AsyncOpenAI client since both Mistral and OpenRouter are OpenAI-compatible.
"""

import logging
from typing import Optional

from openai import AsyncOpenAI

from app.core.config import settings

logger = logging.getLogger(__name__)


class FallbackClient:
    def __init__(self):
        self.openrouter_client = None
        self.mistral_client = None

        if settings.openrouter_api_key:
            self.openrouter_client = AsyncOpenAI(
                api_key=settings.openrouter_api_key,
                base_url="https://openrouter.ai/api/v1",
            )

        if settings.mistral_api_key:
            self.mistral_client = AsyncOpenAI(
                api_key=settings.mistral_api_key,
                base_url="https://api.mistral.ai/v1",
            )

    async def fallback_chat(
        self,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.1,
        max_tokens: int = 8192,
        json_mode: bool = False,
        provider: str = "openrouter",
    ) -> str:
        """
        Calls the specified fallback provider.
        provider: "openrouter" (heavy) or "mistral" (light).
        """
        if provider == "mistral":
            if not self.mistral_client:
                raise Exception("Mistral fallback requested but MISTRAL_API_KEY is not set.")
            
            client = self.mistral_client
            model = "mistral-small-latest"
        else:
            if not self.openrouter_client:
                raise Exception("OpenRouter fallback requested but OPENROUTER_API_KEY is not set.")
            
            client = self.openrouter_client
            model = "meta-llama/llama-3.1-8b-instruct"

        kwargs = {
            "model": model,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }

        if json_mode and provider == "mistral":
            kwargs["response_format"] = {"type": "json_object"}
        elif json_mode and provider == "openrouter":
            # OpenRouter supports json_object for many models, including llama 3
            kwargs["response_format"] = {"type": "json_object"}

        logger.info(f"Initiating fallback request to {provider} ({model})")
        completion = await client.chat.completions.create(**kwargs)
        
        content = completion.choices[0].message.content or ""
        return content.strip()

# Singleton
_fallback_client: Optional[FallbackClient] = None

def get_fallback_client() -> FallbackClient:
    global _fallback_client
    if _fallback_client is None:
        _fallback_client = FallbackClient()
    return _fallback_client
