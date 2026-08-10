"""
app/ingestion/embedder.py — Local sentence-transformers embeddings wrapper.

Provides a wrapper around the all-MiniLM-L6-v2 model to generate 384-dim
embeddings for chunks.  Runs locally — no API key required.
"""

import asyncio
import logging
from typing import List

from sentence_transformers import SentenceTransformer

from app.core.config import settings

logger = logging.getLogger(__name__)

# Max texts per batch call (keeps memory usage reasonable)
BATCH_SIZE = 256


class EmbeddingError(Exception):
    """Raised when embedding generation fails."""
    pass


# ── Lazy-loaded singleton model ───────────────────────────────────────────
_model: SentenceTransformer | None = None


def _get_model() -> SentenceTransformer:
    """Load the model once and reuse it."""
    global _model
    if _model is None:
        logger.info("Loading embedding model: %s", settings.embedding_model)
        _model = SentenceTransformer(settings.embedding_model)
    return _model


class Embedder:
    def __init__(self):
        self.model = _get_model()
        self.dimension = settings.embedding_dimension  # 384

    def _encode_sync(self, texts: List[str]) -> List[List[float]]:
        """Synchronous encode — called via asyncio.to_thread."""
        embeddings = self.model.encode(texts, show_progress_bar=False)
        return [emb.tolist() for emb in embeddings]

    async def embed_batch(self, texts: List[str]) -> List[List[float]]:
        """
        Generates 384-dim embeddings for a list of texts using all-MiniLM-L6-v2.
        Automatically chunks into smaller batches if the list is large.

        Args:
            texts: List of strings to embed.

        Returns:
            List of 384-dimensional float lists.
        """
        if not texts:
            return []

        all_embeddings: List[List[float]] = []

        # Split into batches of BATCH_SIZE
        for i in range(0, len(texts), BATCH_SIZE):
            batch_texts = texts[i:i + BATCH_SIZE]
            logger.debug(
                "Embedding batch %d (%d texts)",
                i // BATCH_SIZE + 1, len(batch_texts),
            )

            try:
                # Run the (CPU/GPU-bound) encode in a thread so we don't
                # block the async event loop.
                batch_embeddings = await asyncio.to_thread(
                    self._encode_sync, batch_texts,
                )
            except Exception as e:
                logger.error("Failed to embed batch: %s", e)
                raise EmbeddingError(f"Embedding failed: {e}")

            # Validate dimensions
            for j, emb in enumerate(batch_embeddings):
                if len(emb) != self.dimension:
                    raise EmbeddingError(
                        f"Expected {self.dimension} dimensions, got {len(emb)} "
                        f"for chunk {i + j}"
                    )

            all_embeddings.extend(batch_embeddings)

        return all_embeddings
