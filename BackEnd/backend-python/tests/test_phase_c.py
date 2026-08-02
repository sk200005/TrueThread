"""
Test script for Phase C — RAG Retrieval, Claim Extraction, Summarization.

This script:
  1. Seeds test data: Creates test documents, chunks them, embeds with the
     local sentence-transformers model (same as store_node.py), and stores
     in document_chunks.
  2. Tests rag_retrieve with 2-3 sample queries.
  3. Tests extract_claims on the retrieved chunks.
  4. Tests summarize on the retrieved chunks + claims.

Run from backend-python/:
    source venv/bin/activate && python -m tests.test_phase_c
"""

import asyncio
import json
import logging
import sys
import uuid

from sentence_transformers import SentenceTransformer
from sqlalchemy import text

from app.core.config import settings
from app.core.database import async_session
from app.ingestion.chunker import chunk_document

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("test_phase_c")

# ── Test data: synthetic smartphone articles ─────────────────────────────

TEST_DOCUMENTS = [
    {
        "source": "wikipedia",
        "author": None,
        "text": (
            "The iPhone 15 Pro features a titanium design, replacing the stainless steel "
            "used in previous models. Apple introduced the A17 Pro chip, which offers "
            "improved GPU performance and supports hardware-accelerated ray tracing. "
            "The camera system includes a 48MP main sensor with a new tetraprism "
            "telephoto lens offering 5x optical zoom. Battery life has been improved "
            "compared to the iPhone 14 Pro, with Apple claiming up to 23 hours of "
            "video playback. The device also introduces USB-C connectivity, replacing "
            "the Lightning port used since 2012. The Action Button replaces the "
            "traditional mute switch, offering customizable functionality."
        ),
        "url": "https://en.wikipedia.org/wiki/iPhone_15_Pro",
    },
    {
        "source": "wikipedia",
        "author": None,
        "text": (
            "Samsung Galaxy S24 Ultra features a flat titanium frame design with a "
            "6.8-inch Dynamic AMOLED 2X display. It is powered by the Snapdragon 8 "
            "Gen 3 processor and includes Galaxy AI features like Circle to Search, "
            "Live Translate, and Chat Assist. The camera system has a 200MP main "
            "sensor, 50MP 5x telephoto, 10MP 3x telephoto, and 12MP ultrawide. "
            "Samsung claims up to 30 hours of video playback. The phone supports "
            "an integrated S Pen stylus. It runs One UI 6.1 based on Android 14. "
            "Samsung promises 7 years of OS and security updates."
        ),
        "url": "https://en.wikipedia.org/wiki/Samsung_Galaxy_S24_Ultra",
    },
    {
        "source": "wikipedia",
        "author": None,
        "text": (
            "Google Pixel 8 Pro is powered by the Tensor G3 chip, designed by Google "
            "specifically for AI and machine learning tasks. It features a 6.7-inch "
            "Super Actua display with 120Hz refresh rate. The camera system includes "
            "a 50MP main sensor, 48MP ultrawide with Macro Focus, and a 48MP telephoto "
            "with 5x optical zoom. Key software features include Best Take, Magic "
            "Eraser, Photo Unblur, and the new AI-powered Magic Editor. The Pixel 8 "
            "Pro introduces a thermometer sensor and improved Face Unlock. Google "
            "promises 7 years of OS, security, and Feature Drop updates. The device "
            "runs stock Android 14 with exclusive Pixel features."
        ),
        "url": "https://en.wikipedia.org/wiki/Pixel_8_Pro",
    },
]

TEST_QUERIES = [
    "Which phone has the best camera system?",
    "Compare battery life of flagship phones",
    "What AI features do modern smartphones have?",
]


async def seed_test_data():
    """Seed document_chunks with embedded test documents using local model."""
    logger.info("=" * 60)
    logger.info("STEP 1: Seeding test data into document_chunks")
    logger.info("=" * 60)

    # Use the same local model as store_node.py
    logger.info("Loading embedding model: %s", settings.embedding_model)
    model = SentenceTransformer(settings.embedding_model)

    query_id = str(uuid.uuid4())

    # Create a query row first (FK requirement)
    async with async_session() as session:
        await session.execute(
            text("""
                INSERT INTO queries (id, query_text, status, sources_requested)
                VALUES (:id, :query_text, 'done', :sources)
            """),
            {
                "id": query_id,
                "query_text": "smartphone comparison test",
                "sources": ["wikipedia"],
            },
        )
        await session.commit()
    logger.info("Created test query: %s", query_id)

    total_chunks = 0

    async with async_session() as session:
        for doc in TEST_DOCUMENTS:
            # Insert source_document
            doc_id = str(uuid.uuid4())
            await session.execute(
                text("""
                    INSERT INTO source_documents (id, query_id, source, author, text, url)
                    VALUES (:id, :query_id, :source, :author, :text, :url)
                """),
                {
                    "id": doc_id,
                    "query_id": query_id,
                    "source": doc["source"],
                    "author": doc["author"],
                    "text": doc["text"],
                    "url": doc["url"],
                },
            )

            # Chunk the text
            chunks = chunk_document(doc["text"])
            if not chunks:
                logger.warning("No chunks for doc %s", doc_id)
                continue

            chunk_texts = [c.chunk_text for c in chunks]

            # Embed using local sentence-transformers (same as store_node.py)
            logger.info("Embedding %d chunks for %s...", len(chunk_texts), doc["url"])
            embeddings = model.encode(chunk_texts, show_progress_bar=False)

            # Insert chunks with embeddings
            for chunk, embedding in zip(chunks, embeddings):
                chunk_id = str(uuid.uuid4())
                embedding_list = embedding.tolist()
                await session.execute(
                    text("""
                        INSERT INTO document_chunks (id, source_document_id, query_id, chunk_text, embedding)
                        VALUES (:id, :source_document_id, :query_id, :chunk_text, :embedding)
                    """),
                    {
                        "id": chunk_id,
                        "source_document_id": doc_id,
                        "query_id": query_id,
                        "chunk_text": chunk.chunk_text,
                        "embedding": str(embedding_list),
                    },
                )
                total_chunks += 1

        await session.commit()

    logger.info("Seeded %d chunks from %d documents.", total_chunks, len(TEST_DOCUMENTS))
    return query_id


async def test_rag_retrieve(query_id: str):
    """Test the RAG retrieval node with sample queries."""
    logger.info("\n" + "=" * 60)
    logger.info("STEP 2: Testing RAG Retrieval")
    logger.info("=" * 60)

    from app.graphs.nodes.retrieve import rag_retrieve

    for query_text in TEST_QUERIES:
        logger.info("\n--- Query: %r ---", query_text)

        state = {
            "query": query_text,
            "top_k": 3,
        }

        result = await rag_retrieve(state)
        chunks = result.get("retrieved_chunks", [])

        logger.info("Retrieved %d chunks:", len(chunks))
        for i, chunk in enumerate(chunks):
            preview = chunk["chunk_text"][:120].replace("\n", " ")
            logger.info(
                "  [%d] similarity=%.4f | %s...",
                i + 1, chunk["similarity"], preview,
            )

        print()


async def test_extract_claims(query_id: str):
    """Test claim extraction on sample chunks."""
    logger.info("\n" + "=" * 60)
    logger.info("STEP 3: Testing Claim Extraction")
    logger.info("=" * 60)

    from app.graphs.nodes.retrieve import rag_retrieve
    from app.graphs.nodes.extract_claims import extract_claims

    state = {
        "query": "Compare smartphone cameras and battery life",
        "top_k": 5,
    }
    retrieve_result = await rag_retrieve(state)
    state.update(retrieve_result)

    logger.info("Running claim extraction on %d retrieved chunks...", len(state["retrieved_chunks"]))

    claims_result = await extract_claims(state)
    claims = claims_result.get("extracted_claims", [])

    logger.info("\nExtracted %d claims:", len(claims))
    for i, claim in enumerate(claims):
        logger.info("  [%d] type=%s confidence=%s", i + 1, claim["claim_type"], claim["confidence"])
        logger.info("      text: %s", claim["claim_text"])
        logger.info("      entities: %s", claim["entities"])

    return state, claims_result


async def test_summarize(state: dict, claims_result: dict):
    """Test summarization with sample data."""
    logger.info("\n" + "=" * 60)
    logger.info("STEP 4: Testing Summarization")
    logger.info("=" * 60)

    from app.graphs.nodes.summarize import summarize

    state.update(claims_result)
    # Don't set query_id to avoid writing to DB during test
    state.pop("query_id", None)

    logger.info("Running summarization on %d chunks and %d claims...",
                len(state.get("retrieved_chunks", [])),
                len(state.get("extracted_claims", [])))

    result = await summarize(state)
    report = result.get("final_report", {})

    logger.info("\n" + "-" * 40)
    logger.info("FINAL REPORT:")
    logger.info("-" * 40)
    logger.info(json.dumps(report, indent=2))

    return report


async def main():
    """Run all tests in sequence."""
    logger.info("Phase C Test Suite Starting...\n")

    # Check if we need to seed data
    async with async_session() as session:
        result = await session.execute(text("SELECT COUNT(*) FROM document_chunks"))
        count = result.scalar()

    if count == 0:
        logger.info("No chunks in DB — seeding test data first...")
        query_id = await seed_test_data()
    else:
        logger.info("Found %d existing chunks — skipping seed.", count)
        async with async_session() as session:
            result = await session.execute(
                text("SELECT DISTINCT query_id FROM document_chunks LIMIT 1")
            )
            query_id = str(result.scalar())

    # Test 1: RAG Retrieval
    await test_rag_retrieve(query_id)

    # Test 2: Claim Extraction
    state, claims_result = await test_extract_claims(query_id)

    # Test 3: Summarization
    await test_summarize(state, claims_result)

    logger.info("\n" + "=" * 60)
    logger.info("ALL TESTS COMPLETE")
    logger.info("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
