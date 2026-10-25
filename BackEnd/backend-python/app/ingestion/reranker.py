"""
app/ingestion/reranker.py — Cross-encoder reranker for two-stage RAG retrieval.

Uses sentence-transformers CrossEncoder (ms-marco-MiniLM-L-6-v2 by default)
to rerank candidate chunks retrieved by HNSW vector search.

Two-stage RAG flow:
    1. HNSW retrieves a broad candidate pool (top_k × multiplier)
    2. Cross-encoder scores each (query, chunk_text) pair with full
       cross-attention, then returns the top_k highest-scoring chunks

Design notes:
    - Follows the same lazy-loaded singleton + asyncio.to_thread pattern
      as embedder.py to avoid blocking the async event loop.
    - Gracefully degrades: if the model fails to load or predict, it
      falls back to returning the original HNSW-ranked chunks (no crash).
    - Controlled by settings.reranker_enabled — can be toggled off via
      env var RERANKER_ENABLED=false without code changes.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from sentence_transformers import CrossEncoder

from app.core.config import settings

logger = logging.getLogger(__name__)


# ── Lazy-loaded singleton model ───────────────────────────────────────────
_reranker_model: CrossEncoder | None = None
_reranker_load_failed: bool = False


def _get_reranker() -> CrossEncoder | None:
    """Load the cross-encoder model once and reuse it.

    Returns None if model loading fails, allowing graceful fallback.
    """
    global _reranker_model, _reranker_load_failed

    # Don't retry loading if it already failed once
    if _reranker_load_failed:
        return None

    if _reranker_model is None:
        try:
            logger.info("Loading cross-encoder reranker: %s", settings.reranker_model)
            _reranker_model = CrossEncoder(settings.reranker_model)
            logger.info("Cross-encoder reranker loaded successfully")
        except Exception as e:
            _reranker_load_failed = True
            logger.error("Failed to load cross-encoder model %s: %s", settings.reranker_model, e)
            return None

    return _reranker_model


class Reranker:
    """Cross-encoder reranker for two-stage RAG retrieval.

    Usage:
        reranker = Reranker()
        reranked = await reranker.rerank(query, chunks, top_k=5)

    If reranking is disabled or the model fails, chunks are returned
    in their original HNSW order (safe fallback).
    """

    def __init__(self) -> None:
        self.enabled = settings.reranker_enabled
        self._model: CrossEncoder | None = None

        if self.enabled:
            self._model = _get_reranker()
            if self._model is None:
                logger.warning(
                    "Reranker enabled but model failed to load — "
                    "falling back to HNSW ordering"
                )
                self.enabled = False

    def _predict_sync(self, pairs: list[tuple[str, str]]) -> list[float]:
        """Synchronous predict — called via asyncio.to_thread."""
        scores = self._model.predict(pairs)  # type: ignore[union-attr]
        return scores.tolist()

    async def rerank(
        self,
        query: str,
        chunks: list[dict[str, Any]],
        top_k: int = 5,
    ) -> list[dict[str, Any]]:
        """Rerank chunks using cross-encoder scores.

        Args:
            query: The user's search query.
            chunks: List of chunk dicts (must contain "chunk_text" key).
            top_k: Number of chunks to return after reranking.

        Returns:
            Top-k chunks sorted by cross-encoder relevance score (descending).
            Each chunk gets an additional "rerank_score" field.
            Falls back to original order on any error.
        """
        if not self.enabled or not chunks:
            return chunks[:top_k]

        # If we have fewer chunks than top_k, reranking is still useful
        # for reordering but we return all of them
        try:
            # Build (query, chunk_text) pairs for cross-attention scoring
            pairs: list[tuple[str, str]] = [
                (query, chunk["chunk_text"]) for chunk in chunks
            ]

            # Run inference in a thread pool (CPU-bound model)
            scores = await asyncio.to_thread(self._predict_sync, pairs)

            # Attach rerank score to each chunk for observability
            for chunk, score in zip(chunks, scores):
                chunk["rerank_score"] = float(score)

            # Sort by cross-encoder score (highest relevance first)
            reranked = sorted(chunks, key=lambda c: c["rerank_score"], reverse=True)

            logger.info(
                "Reranked %d candidates → top %d (best=%.4f, worst=%.4f)",
                len(chunks),
                min(top_k, len(reranked)),
                reranked[0]["rerank_score"] if reranked else 0.0,
                reranked[min(top_k, len(reranked)) - 1]["rerank_score"] if reranked else 0.0,
            )

            return reranked[:top_k]

        except Exception as e:
            # Graceful degradation: log the error and return original order
            logger.error("Reranking failed, falling back to HNSW order: %s", e)
            return chunks[:top_k]
