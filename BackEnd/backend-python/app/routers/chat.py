from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text
from typing import Any

from app.core.config import settings
from app.core.database import get_db
from app.schemas.chat import ChatRequest, ChatResponse
from app.ingestion.embedder import Embedder, EmbeddingError
from app.core.gemini_client import GeminiClient

import logging
logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/chat", tags=["chat"])

# Number of final chunks to pass to the LLM
CHAT_TOP_K = 5

@router.post("/", response_model=ChatResponse)
async def chat_with_query(
    request: ChatRequest,
    db: AsyncSession = Depends(get_db)
):
    try:
        # 1. Embed the user's message
        embedder = Embedder()
        embeddings = await embedder.embed_batch([request.message])
        query_embedding = embeddings[0]
        
        # 2. Retrieve candidate chunks for this query_id
        # When reranking is enabled, over-fetch so the cross-encoder has a
        # broader candidate pool to reorder for precision.
        candidate_limit = CHAT_TOP_K
        if settings.reranker_enabled:
            candidate_limit = CHAT_TOP_K * settings.rerank_candidate_multiplier

        sql = text("""
            SELECT chunk_text, (1 - (embedding <=> :query_embedding)) AS similarity
            FROM document_chunks
            WHERE query_id = :query_id
            ORDER BY embedding <=> :query_embedding
            LIMIT :limit
        """)
        
        await db.execute(text(f"SET hnsw.ef_search = {settings.hnsw_ef_search}"))
        result = await db.execute(sql, {
            "query_embedding": str(query_embedding),
            "query_id": str(request.query_id),
            "limit": candidate_limit,
        })
        
        rows = result.fetchall()

        if not rows:
            return ChatResponse(response="I couldn't find any data related to this query to answer your question.")

        # 3. Apply cross-encoder reranking if enabled
        if settings.reranker_enabled and len(rows) > CHAT_TOP_K:
            from app.ingestion.reranker import Reranker
            reranker = Reranker()

            # Build chunk dicts for the reranker (it expects "chunk_text" key)
            candidate_chunks = [
                {"chunk_text": row[0], "similarity": float(row[1])}
                for row in rows
            ]
            reranked = await reranker.rerank(
                query=request.message,
                chunks=candidate_chunks,
                top_k=CHAT_TOP_K,
            )
            chunks = [c["chunk_text"] for c in reranked]
        else:
            chunks = [row[0] for row in rows[:CHAT_TOP_K]]
        
        if not chunks:
            return ChatResponse(response="I couldn't find any data related to this query to answer your question.")
            
        # 4. Construct the prompt
        context_str = "\n\n".join(f"- {chunk}" for chunk in chunks)
        
        system_prompt = (
            "You are a helpful research assistant. "
            "You must answer the user's question using strictly the context provided below. "
            "If the answer is not contained in the context, say 'I cannot answer this based on the retrieved research context.'\n\n"
            f"Context:\n{context_str}"
        )
        
        # 5. Call Gemini LLM (heavy reasoning — user-facing quality)
        llm = GeminiClient()
        response_text = await llm.chat(
            system_prompt=system_prompt,
            user_prompt=request.message
        )
        
        return ChatResponse(response=response_text, contexts=chunks)
        
    except EmbeddingError as e:
        logger.error(f"Embedding error: {e}")
        raise HTTPException(status_code=500, detail="Failed to process query embedding")
    except Exception as e:
        logger.error(f"Chat error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

