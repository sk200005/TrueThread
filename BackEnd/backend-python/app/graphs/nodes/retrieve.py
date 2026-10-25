"""
graphs/nodes/retrieve.py — RAG retrieval node for the query-time graph.

Takes the user's query from graph state, embeds it using the SAME
sentence-transformers model used during ingestion (all-MiniLM-L6-v2,
384 dims), and runs a cosine-similarity search against the document_chunks
table using pgvector's <=> operator.

Returns the top-k most similar chunks with their text, IDs, and similarity
scores, stored in state as `retrieved_chunks`.

How cosine distance works in pgvector:
    - The <=> operator returns COSINE DISTANCE (0 = identical, 2 = opposite)
    - We convert to SIMILARITY by doing: similarity = 1 - distance
    - Results are ordered by distance ASC (most similar first)
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import text

from app.core.config import settings
from app.core.database import async_session
from app.graphs.state import QueryState, RetrievedChunk

logger = logging.getLogger(__name__)

# Default number of chunks to retrieve if not specified in state
DEFAULT_TOP_K = 8


async def rag_retrieve(state: QueryState) -> dict[str, Any]:
    """
    LangGraph node: embed the user's query and find the most similar chunks.

    When cross-encoder reranking is enabled (settings.reranker_enabled),
    this node implements two-stage retrieval:
        Stage 1: HNSW fetches top_k × multiplier candidate chunks (fast)
        Stage 2: Cross-encoder reranks candidates → returns top_k (precise)

    When reranking is disabled, behaviour is identical to the original
    single-stage HNSW retrieval.

    Reads from state:
        - query (str): The user's natural-language question
        - query_id (str, optional): If provided, scopes search to chunks from this query only
        - top_k (int, optional): Number of chunks to return (default: 8)

    Returns:
        - retrieved_chunks: List of RetrievedChunk dicts with chunk_id, chunk_text,
          source_document_id, and similarity score
        - status: "retrieving"
    """
    query_text = state["query"]
    query_id = state.get("query_id")           # Optional — scope to a specific ingestion run
    top_k = state.get("top_k", DEFAULT_TOP_K)

    # ── Determine fetch count ────────────────────────────────────────────
    # When reranking is enabled, over-fetch candidates so the cross-encoder
    # has a broader pool to reorder. Otherwise, fetch exactly top_k.
    candidate_k = top_k
    if settings.reranker_enabled:
        candidate_k = top_k * settings.rerank_candidate_multiplier

    logger.info(
        "RAG retrieve starting for query: %r (top_k=%d, candidate_k=%d, reranker=%s)",
        query_text, top_k, candidate_k, settings.reranker_enabled,
    )

    # ── Step 1: Embed the query ──────────────────────────────────────────
    # We use the SAME local embedding model (all-MiniLM-L6-v2, 384 dims)
    # that store_node.py uses for ingestion. This ensures query embeddings
    # match the stored chunk embeddings for accurate cosine similarity.
    from app.ingestion.embedder import Embedder, EmbeddingError
    embedder = Embedder()
    try:
        embeddings = await embedder.embed_batch([query_text])
        query_embedding = embeddings[0]
    except EmbeddingError as exc:
        logger.error("Failed to embed query: %s", exc)
        return {"retrieved_chunks": [], "status": "error"}

    logger.info("Query embedded successfully (%d dimensions)", len(query_embedding))

    # ── Step 2: Cosine-similarity search ─────────────────────────────────
    # Build the SQL query. If query_id is provided, we only search chunks
    # from that specific ingestion run. Otherwise we search all chunks.
    #
    # The <=> operator is pgvector's cosine distance:
    #   distance = 0 means identical vectors
    #   distance = 2 means opposite vectors
    # We compute similarity = 1 - distance for a more intuitive score.

    if query_id:
        sql = text("""
            SELECT
                id,
                chunk_text,
                source_document_id,
                (1 - (embedding <=> :query_embedding)) AS similarity
            FROM document_chunks
            WHERE query_id = :query_id
            ORDER BY embedding <=> :query_embedding
            LIMIT :top_k
        """)
        params = {
            "query_embedding": str(query_embedding),
            "query_id": query_id,
            "top_k": candidate_k,
        }
    else:
        # Search across ALL chunks (useful for cross-query retrieval)
        sql = text("""
            SELECT
                id,
                chunk_text,
                source_document_id,
                (1 - (embedding <=> :query_embedding)) AS similarity
            FROM document_chunks
            ORDER BY embedding <=> :query_embedding
            LIMIT :top_k
        """)
        params = {
            "query_embedding": str(query_embedding),
            "top_k": candidate_k,
        }

    retrieved_chunks: list[RetrievedChunk] = []

    async with async_session() as session:
        await session.execute(text(f"SET hnsw.ef_search = {settings.hnsw_ef_search}"))
        result = await session.execute(sql, params)
        rows = result.fetchall()

        for row in rows:
            chunk: RetrievedChunk = {
                "chunk_id": str(row.id),
                "chunk_text": row.chunk_text,
                "source_document_id": str(row.source_document_id),
                "similarity": float(row.similarity),
            }
            retrieved_chunks.append(chunk)

    logger.info(
        "HNSW retrieval complete: %d candidates returned (best similarity: %.4f)",
        len(retrieved_chunks),
        retrieved_chunks[0]["similarity"] if retrieved_chunks else 0.0,
    )

    # ── Step 3: Cross-encoder reranking (optional) ───────────────────────
    # If reranking is enabled, pass all candidates through the cross-encoder
    # to reorder by precise relevance, then trim to top_k.
    # If disabled or if reranking fails, the original HNSW order is preserved.
    if settings.reranker_enabled and len(retrieved_chunks) > 0:
        from app.ingestion.reranker import Reranker
        reranker = Reranker()
        retrieved_chunks = await reranker.rerank(
            query=query_text,
            chunks=retrieved_chunks,
            top_k=top_k,
        )

    # Log a preview of what was retrieved (helpful for debugging)
    for i, chunk in enumerate(retrieved_chunks[:3]):
        preview = chunk["chunk_text"][:100].replace("\n", " ")
        rerank_info = f", rerank={chunk.get('rerank_score', 'n/a')}" if "rerank_score" in chunk else ""
        logger.info(
            "  Chunk %d (sim=%.4f%s): %s...",
            i + 1, chunk["similarity"], rerank_info, preview,
        )

    return {
        "retrieved_chunks": retrieved_chunks,
        "status": "retrieving",
    }

