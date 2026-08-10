"""
app/ingestion/chunk_and_embed.py — Main CLI for the RAG ingestion pipeline.

Reads unprocessed documents, chunks them, embeds the chunks, and saves to DB.
Designed to be repeatable, resumable, and idempotent.

Usage:
    python -m app.ingestion.chunk_and_embed --source-type all [--batch-size 10] [--limit 100] [--dry-run]
"""

import argparse
import asyncio
import logging
import sys
import uuid
from typing import Optional, List

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import async_session, engine
from app.models.source_document import SourceDocument, DocumentChunk
from app.ingestion.chunker import chunk_document, ChunkResult
from app.ingestion.embedder import Embedder, EmbeddingError

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)


async def get_unprocessed_documents(
    session: AsyncSession,
    source_type: str,
    limit: Optional[int] = None
) -> List[SourceDocument]:
    """
    Finds documents that have not been chunked yet.
    Determined by checking if a SourceDocument has zero corresponding DocumentChunk rows.
    """
    query = (
        select(SourceDocument)
        .outerjoin(DocumentChunk, SourceDocument.id == DocumentChunk.source_document_id)
        .where(DocumentChunk.id.is_(None))
        .order_by(SourceDocument.created_at.desc())
    )

    if source_type != "all":
        query = query.where(SourceDocument.source == source_type)

    if limit:
        query = query.limit(limit)

    result = await session.execute(query)
    return list(result.scalars().all())


async def check_already_processed(session: AsyncSession, doc_id: uuid.UUID) -> bool:
    """Double checks if chunks exist right before inserting, ensuring idempotency."""
    result = await session.execute(
        select(DocumentChunk.id).where(DocumentChunk.source_document_id == doc_id).limit(1)
    )
    return result.scalar_one_or_none() is not None


async def process_document(
    session: AsyncSession,
    doc: SourceDocument,
    embedder: Embedder,
    dry_run: bool
) -> tuple[int, int]:
    """
    Processes a single document: chunks -> embeds -> inserts.
    Returns (num_chunks, sum_tokens).
    """
    logger.info(f"Processing doc {doc.id} (source: {doc.source}, chars: {len(doc.text)})")

    # 1. Chunking
    chunks = chunk_document(doc.text)
    if not chunks:
        logger.warning(f"Doc {doc.id} produced 0 chunks. Skipping.")
        return 0, 0

    chunk_texts = [c.chunk_text for c in chunks]
    total_tokens = sum(c.token_count for c in chunks)

    if dry_run:
        logger.info(f"  [Dry Run] Would generate {len(chunks)} chunks ({total_tokens} tokens)")
        return len(chunks), total_tokens

    # 2. Embedding
    try:
        embeddings = await embedder.embed_batch(chunk_texts)
    except EmbeddingError as e:
        logger.error(f"Failed to embed doc {doc.id}: {e}")
        return 0, 0

    # 3. Insertion (Idempotency check + Transaction)
    # Using a sub-transaction (nested session block) for this document
    async with session.begin_nested():
        # Double check no chunks exist (in case another process got to it)
        if await check_already_processed(session, doc.id):
            logger.info(f"  Doc {doc.id} already processed. Skipping.")
            return 0, 0

        # Prepare metadata
        metadata = {
            "source_type": doc.source,
            "author": doc.author,
            "url": doc.url,
            # Pass along any upstream engagement metrics if available
            **(doc.engagement_metrics or {})
        }

        # Insert chunks
        for chunk, embedding in zip(chunks, embeddings):
            db_chunk = DocumentChunk(
                source_document_id=doc.id,
                query_id=doc.query_id,
                chunk_text=chunk.chunk_text,
                chunk_index=chunk.chunk_index,
                metadata_=metadata,
                embedding=embedding
            )
            session.add(db_chunk)

    logger.info(f"  Successfully inserted {len(chunks)} chunks for doc {doc.id}")
    return len(chunks), total_tokens


async def run_pipeline(source_type: str, limit: Optional[int], dry_run: bool):
    """Main pipeline execution loop."""
    logger.info("=" * 60)
    logger.info(f"Starting Ingestion Pipeline")
    logger.info(f"Source Type : {source_type}")
    logger.info(f"Limit       : {limit or 'No limit'}")
    logger.info(f"Dry Run     : {dry_run}")
    logger.info("=" * 60)

    embedder = None
    if not dry_run:
        embedder = Embedder()

    docs_processed = 0
    total_chunks = 0
    total_tokens = 0

    async with async_session() as session:
        # 1. Fetch unprocessed documents
        docs = await get_unprocessed_documents(session, source_type, limit)
        logger.info(f"Found {len(docs)} unprocessed documents.")

        if not docs:
            logger.info("Nothing to do.")
            return

        # 2. Process each document sequentially
        # Sequential processing is safer for idempotency and DB load,
        # but the embedding API calls inside process_document are batched over chunks.
        for doc in docs:
            try:
                chunks_created, tokens_created = await process_document(session, doc, embedder, dry_run)
                if chunks_created > 0:
                    docs_processed += 1
                    total_chunks += chunks_created
                    total_tokens += tokens_created
            except Exception as e:
                logger.error(f"Unexpected error processing doc {doc.id}: {e}", exc_info=True)
                # Rollback just this document's transaction (handled by session.begin_nested() raising)
                continue

        # 3. Commit all successful documents
        if not dry_run and docs_processed > 0:
            logger.info("Committing to database...")
            await session.commit()
            
            # 4. Update index statistics for IVFFlat
            # ANALYZE must be run outside a transaction block
            logger.info("Running ANALYZE on document_chunks table...")
            async with engine.connect() as conn:
                # Need to use isolation_level="AUTOCOMMIT" to run ANALYZE
                await conn.execution_options(isolation_level="AUTOCOMMIT").execute(
                    text("ANALYZE document_chunks")
                )

    # Summary
    logger.info("=" * 60)
    logger.info("Pipeline Complete")
    logger.info(f"Docs Processed : {docs_processed}/{len(docs)}")
    logger.info(f"Chunks Created : {total_chunks}")
    if total_chunks > 0:
        logger.info(f"Avg Chunk Size : {total_tokens // total_chunks} tokens")
    logger.info("=" * 60)


def main():
    parser = argparse.ArgumentParser(description="Run the chunking and embedding pipeline.")
    parser.add_argument("--source-type", type=str, required=True, help="all | reddit | youtube | wikipedia")
    parser.add_argument("--limit", type=int, default=None, help="Max number of documents to process.")
    parser.add_argument("--dry-run", action="store_true", help="Chunk only, do not embed or write to DB.")
    args = parser.parse_args()

    asyncio.run(run_pipeline(
        source_type=args.source_type,
        limit=args.limit,
        dry_run=args.dry_run
    ))


if __name__ == "__main__":
    main()
