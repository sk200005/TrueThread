from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text
from typing import Any

from app.core.database import get_db
from app.schemas.chat import ChatRequest, ChatResponse
from app.ingestion.embedder import Embedder, EmbeddingError
from app.core.llm_client import LLMClient

import logging
logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/chat", tags=["chat"])

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
        
        # 2. Retrieve top chunks for this query_id
        sql = text("""
            SELECT chunk_text, (1 - (embedding <=> :query_embedding)) AS similarity
            FROM document_chunks
            WHERE query_id = :query_id
            ORDER BY embedding <=> :query_embedding
            LIMIT 5
        """)
        
        result = await db.execute(sql, {
            "query_embedding": str(query_embedding),
            "query_id": str(request.query_id)
        })
        
        chunks = [row[0] for row in result.fetchall()]
        
        if not chunks:
            return ChatResponse(response="I couldn't find any data related to this query to answer your question.")
            
        # 3. Construct the prompt
        context_str = "\\n\\n".join(f"- {chunk}" for chunk in chunks)
        
        system_prompt = (
            "You are a helpful research assistant. "
            "You must answer the user's question using strictly the context provided below. "
            "If the answer is not contained in the context, say 'I cannot answer this based on the retrieved research context.'\\n\\n"
            f"Context:\\n{context_str}"
        )
        
        # 4. Call Groq LLM
        llm = LLMClient()
        response_text = await llm.chat(
            system_prompt=system_prompt,
            user_prompt=request.message
        )
        
        return ChatResponse(response=response_text)
        
    except EmbeddingError as e:
        logger.error(f"Embedding error: {e}")
        raise HTTPException(status_code=500, detail="Failed to process query embedding")
    except Exception as e:
        logger.error(f"Chat error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
