"""
graphs/nodes/store_node.py — Chunk, embed, and persist documents to Postgres.

This node takes the raw_documents accumulated by fetch nodes, and:
  1. Writes each document as a `source_documents` row
  2. Splits each document's text into chunks via LangChain's RecursiveCharacterTextSplitter
  3. Generates embeddings for each chunk using sentence-transformers (all-MiniLM-L6-v2)
  4. Writes each chunk as a `document_chunks` row with its embedding vector

Uses async SQLAlchemy for non-blocking DB writes.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from langchain_text_splitters import RecursiveCharacterTextSplitter
from sentence_transformers import SentenceTransformer
from sqlalchemy import text

from app.core.config import settings
from app.core.database import async_session
from app.graphs.state import ResearchState, SourceDoc

logger = logging.getLogger(__name__)

# ── Text splitter ─────────────────────────────────────────────────────────
_splitter = RecursiveCharacterTextSplitter(
    chunk_size=1000,
    chunk_overlap=200,
    length_function=len,
    separators=["\n\n", "\n", ". ", " ", ""],
)

# ── Embedding model (loaded lazily on first call) ─────────────────────────
_embedding_model: SentenceTransformer | None = None


def _get_embedding_model() -> SentenceTransformer:
    """Lazy-load the embedding model to avoid slow import at boot time."""
    global _embedding_model
    if _embedding_model is None:
        logger.info("Loading embedding model: %s", settings.embedding_model)
        _embedding_model = SentenceTransformer(settings.embedding_model)
    return _embedding_model


async def store_documents(state: ResearchState) -> dict[str, Any]:
    """
    LangGraph node: chunk, embed, and write all raw_documents to Postgres.

    Returns updated state with results counts.
    """
    sources = state.get("sources", {})
    job_id = state.get("job_id")
    query = state.get("query", "")

    # Fan-in: merge documents from all successful sources
    merged_documents: list[SourceDoc] = []
    successful_sources = []
    failed_sources = []

    for source_name, source_result in sources.items():
        if source_result.get("status") == "done":
            docs = source_result.get("documents", [])
            merged_documents.extend(docs)
            successful_sources.append(source_name)
        elif source_result.get("status") == "failed":
            failed_sources.append(source_name)

    logger.info(
        "Fan-in complete for job %s. Success: %s, Failed: %s",
        job_id, successful_sources, failed_sources
    )

    if not merged_documents:
        logger.info("No documents to store for job %s", job_id)
        return {"status": "storing", "results": {"docsInserted": 0, "chunksInserted": 0}, "merged_documents": []}

    logger.info("Storing %d merged documents for job %s", len(merged_documents), job_id)

    model = _get_embedding_model()
    docs_inserted = 0
    chunks_inserted = 0

    async with async_session() as session:
        for doc in merged_documents:
            try:
                # 1. Insert source_document row
                doc_id = uuid.uuid4()
                await session.execute(
                    text("""
                        INSERT INTO source_documents (id, query_id, source, author, text, url, published_at, created_at)
                        VALUES (:id, :query_id, :source, :author, :text, :url, :published_at, now())
                    """),
                    {
                        "id": str(doc_id),
                        "query_id": job_id,
                        "source": doc["source"],
                        "author": doc.get("author"),
                        "text": doc["text"],
                        "url": doc.get("url"),
                        "published_at": doc.get("published_at"),
                    },
                )
                docs_inserted += 1

                # 2. Chunk the text
                chunks = _splitter.split_text(doc["text"])
                if not chunks:
                    continue

                # 3. Generate embeddings for all chunks in one batch call in a background thread
                import asyncio
                embeddings = await asyncio.to_thread(model.encode, chunks, show_progress_bar=False)

                # 4. Insert chunk rows with embeddings
                for chunk_text, embedding in zip(chunks, embeddings):
                    chunk_id = uuid.uuid4()
                    embedding_list = embedding.tolist()
                    await session.execute(
                        text("""
                            INSERT INTO document_chunks
                                (id, source_document_id, query_id, chunk_text, embedding, created_at)
                            VALUES (:id, :source_document_id, :query_id, :chunk_text, :embedding, now())
                        """),
                        {
                            "id": str(chunk_id),
                            "source_document_id": str(doc_id),
                            "query_id": job_id,
                            "chunk_text": chunk_text,
                            "embedding": str(embedding_list),
                        },
                    )
                    chunks_inserted += 1

                logger.info(
                    "Stored doc %s: %d chunks (source=%s, url=%s)",
                    doc_id, len(chunks), doc["source"], doc.get("url", "N/A"),
                )

            except Exception as exc:
                logger.error("Failed to store document: %s", exc)
                continue

        await session.commit()

    logger.info(
        "Store complete for job %s: %d docs, %d chunks",
        job_id, docs_inserted, chunks_inserted,
    )

    return {
        "status": "storing",
        "merged_documents": merged_documents,
        "results": {
            "docsInserted": docs_inserted,
            "chunksInserted": chunks_inserted,
        },
    }
