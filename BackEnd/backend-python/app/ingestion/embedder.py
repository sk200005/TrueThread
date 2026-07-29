"""
app/ingestion/embedder.py — OpenAI embeddings wrapper with backoff.

Provides a thin wrapper around OpenAI's API to generate embeddings for chunks.
Handles batching, retries, and dimension validation.
"""

import logging
import asyncio
from typing import List

from openai import AsyncOpenAI
import openai

from app.core.config import settings

logger = logging.getLogger(__name__)

# Max texts per API call (OpenAI allows large batches, but keeping it reasonable prevents timeouts)
BATCH_SIZE = 100

# Retries
MAX_RETRIES = 5
INITIAL_BACKOFF = 2  # seconds


class EmbeddingError(Exception):
    """Raised when embedding generation fails permanently."""
    pass


class Embedder:
    def __init__(self):
        # We assume OPENAI_API_KEY is available in settings/env
        if not settings.openai_api_key:
            logger.warning("OPENAI_API_KEY is not set. Embeddings will fail.")
        
        self.client = AsyncOpenAI(api_key=settings.openai_api_key)
        self.model = settings.openai_embedding_model

    async def _embed_with_retry(self, texts: List[str], retries: int = MAX_RETRIES) -> List[List[float]]:
        """Calls the OpenAI API with exponential backoff on transient errors."""
        backoff = INITIAL_BACKOFF
        for attempt in range(1, retries + 1):
            try:
                response = await self.client.embeddings.create(
                    input=texts,
                    model=self.model,
                    # text-embedding-3-small outputs 1536 dims by default.
                    # explicitly passing dimensions is optional for this model, but we validate later
                )
                
                # OpenAI returns embeddings in the same order as the input
                # Ensure they are sorted by index just in case
                sorted_data = sorted(response.data, key=lambda x: x.index)
                return [d.embedding for d in sorted_data]
                
            except (openai.RateLimitError, openai.APIConnectionError, openai.APITimeoutError) as e:
                if attempt == retries:
                    logger.error(f"Failed to embed batch after {retries} attempts. Last error: {e}")
                    raise EmbeddingError(f"Transient error persisted after {retries} retries: {e}")
                
                logger.warning(f"Embedding transient error (attempt {attempt}/{retries}): {e}. Retrying in {backoff}s...")
                await asyncio.sleep(backoff)
                backoff *= 2
                
            except openai.APIError as e:
                # Some API errors (5xx) might be transient
                if e.status_code and e.status_code >= 500 and attempt < retries:
                    logger.warning(f"OpenAI 5xx error (attempt {attempt}/{retries}): {e}. Retrying in {backoff}s...")
                    await asyncio.sleep(backoff)
                    backoff *= 2
                else:
                    logger.error(f"Fatal OpenAI API error: {e}")
                    raise EmbeddingError(f"Fatal API error: {e}")
                    
            except Exception as e:
                logger.error(f"Unexpected error during embedding: {e}")
                raise EmbeddingError(f"Unexpected error: {e}")

    async def embed_batch(self, texts: List[str]) -> List[List[float]]:
        """
        Generates 1536-dim embeddings for a list of texts.
        Automatically chunks into smaller API requests if the list is too large.
        
        Args:
            texts: List of strings to embed.
            
        Returns:
            List of 1536-dimensional float lists.
        """
        if not texts:
            return []

        all_embeddings = []
        
        # Split into batches of BATCH_SIZE
        for i in range(0, len(texts), BATCH_SIZE):
            batch_texts = texts[i:i + BATCH_SIZE]
            logger.debug(f"Requesting embeddings for batch {i//BATCH_SIZE + 1} ({len(batch_texts)} texts)")
            
            batch_embeddings = await self._embed_with_retry(batch_texts)
            
            # Validate dimensions (1536)
            for j, emb in enumerate(batch_embeddings):
                if len(emb) != 1536:
                    raise EmbeddingError(f"Expected 1536 dimensions, got {len(emb)} for chunk {i+j}")
                    
            all_embeddings.extend(batch_embeddings)
            
        return all_embeddings
