"""
Reddit Pipeline Test — Full end-to-end pipeline test.

Stages:
    1. FETCH    — Fetch Reddit posts + comments via JSON API (no auth needed)
    2. STORE    — Insert source_documents into test DB
    3. CHUNK    — Split + embed + store document_chunks
    4. RETRIEVE — RAG cosine similarity search
    5. EXTRACT  — LLM claim extraction from chunks
    6. SUMMARIZE — LLM structured report generation

Usage:
    python run_pipeline.py "best noise cancelling headphones 2024"
    python run_pipeline.py "best laptop for programming"
"""

from __future__ import annotations

import asyncio
import json
import sys
import os
import uuid
import time

# Add parent directory to path so we can import test_config
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests

from test_config import (
    async_session,
    embed_texts,
    chunk_text,
    llm_chat,
    pre_filter,
    call_llm_for_claims,
    build_claim_user_message,
    parse_claims_response,
    build_summary_user_prompt,
    parse_summary_response,
    SUMMARIZE_SYSTEM_PROMPT,
    CLAIM_BATCH_SIZE,
    print_stage_header,
    print_result,
    print_error,
    print_info,
    print_warning,
    print_divider,
    wait_for_next_stage,
    print_pipeline_header,
    print_pipeline_footer,
    Colors,
)
from sqlalchemy import text as sql_text


# ── Reddit JSON API helpers ───────────────────────────────────────────────
# Reddit serves JSON at any URL by appending .json — no API key or OAuth needed.
# We use this to avoid the Playwright dependency in tests.

REDDIT_HEADERS = {
    "User-Agent": "ReSearchPlatformTest/0.1 (research-project; testing)",
}
MAX_POSTS = 5
MAX_COMMENTS_PER_POST = 7


def search_reddit_posts(query: str) -> list[dict]:
    """Search Reddit for posts matching the query via JSON API."""
    url = "https://www.reddit.com/search.json"
    params = {
        "q": query,
        "sort": "relevance",
        "limit": MAX_POSTS,
        "type": "link",
    }
    try:
        resp = requests.get(url, params=params, headers=REDDIT_HEADERS, timeout=15)
        resp.raise_for_status()
        data = resp.json()

        posts = []
        children = data.get("data", {}).get("children", [])
        for child in children:
            post_data = child.get("data", {})
            posts.append({
                "post_id": post_data.get("id", ""),
                "title": post_data.get("title", ""),
                "subreddit": post_data.get("subreddit_name_prefixed", ""),
                "url": f"https://www.reddit.com{post_data.get('permalink', '')}",
                "upvotes": post_data.get("ups", 0),
                "body": post_data.get("selftext", ""),
                "num_comments": post_data.get("num_comments", 0),
                "author": post_data.get("author", ""),
                "created_utc": post_data.get("created_utc", 0),
            })
        return posts

    except Exception as exc:
        print_error(f"Reddit search failed: {exc}")
        return []


def fetch_reddit_comments(permalink: str) -> list[dict]:
    """Fetch top-level comments for a Reddit post via JSON API."""
    url = f"https://www.reddit.com{permalink}.json"
    params = {"sort": "top", "limit": MAX_COMMENTS_PER_POST}
    try:
        # Reddit rate limits — be polite
        time.sleep(1)
        resp = requests.get(url, params=params, headers=REDDIT_HEADERS, timeout=15)
        resp.raise_for_status()
        data = resp.json()

        comments = []
        if len(data) > 1:
            comment_listing = data[1].get("data", {}).get("children", [])
            for child in comment_listing:
                if child.get("kind") != "t1":
                    continue
                comment_data = child.get("data", {})
                body = (comment_data.get("body") or "").strip()
                if not body or body == "[deleted]" or body == "[removed]":
                    continue
                comments.append({
                    "id": comment_data.get("id", ""),
                    "author": comment_data.get("author", ""),
                    "text": body,
                    "upvotes": comment_data.get("ups", 0),
                    "created_utc": comment_data.get("created_utc", 0),
                })
        return comments[:MAX_COMMENTS_PER_POST]

    except Exception as exc:
        print_warning(f"Failed to fetch comments: {exc}")
        return []


# ══════════════════════════════════════════════════════════════════════════
# STAGE 1: DATA FETCHING
# ══════════════════════════════════════════════════════════════════════════

async def stage_fetch(query: str) -> list[dict]:
    """Fetch Reddit posts + comments."""
    print_stage_header(1, "DATA FETCHING (Reddit)")

    print_info(f"Searching Reddit for: \"{query}\"")
    print_info("NOTICE: Reddit currently blocks unauthenticated automated requests (403).")
    print_info("Using realistic mock data for testing the downstream AI pipeline...")
    print()

    # Provide realistic mock data since real fetching is blocked
    posts = [
        {
            "post_id": "mock123",
            "title": "Best noise cancelling headphones in 2024?",
            "subreddit": "r/headphones",
            "url": "https://www.reddit.com/r/headphones/comments/mock123/best_noise_cancelling_headphones_in_2024/",
            "upvotes": 452,
            "body": "I'm looking for a pair of over-ear headphones with the absolute best ANC for flights and office work. I've tried the Sony WH-1000XM4s in the past and they were great, but wondering if the XM5s or Bose QuietComfort Ultra are worth the upgrade.",
            "num_comments": 15,
            "author": "audiophile_guy",
            "created_utc": 1704067200,
        }
    ]
    
    mock_comments = [
        {
            "id": "c1",
            "author": "bose_fan",
            "text": "The Bose QuietComfort Ultras have hands down the best ANC I've ever experienced. It completely blocks out airplane engine noise.",
            "upvotes": 120,
            "created_utc": 1704070000,
        },
        {
            "id": "c2",
            "author": "sony_user",
            "text": "Honestly, the Sony WH-1000XM5 are slightly better for music quality, but the headband can get uncomfortable after a few hours compared to the XM4s.",
            "upvotes": 85,
            "created_utc": 1704071000,
        },
        {
            "id": "c3",
            "author": "apple_lover",
            "text": "Don't sleep on the AirPods Max if you're in the Apple ecosystem. Transparency mode is unmatched, even if they are a bit heavy.",
            "upvotes": 45,
            "created_utc": 1704072000,
        }
    ]

    print_result("Posts found", len(posts))
    print()

    # Fetch comments for each post
    documents = []
    for i, post in enumerate(posts):
        print_info(f"Post {i+1}/{len(posts)}: \"{post['title'][:60]}...\"")
        print(f"     {Colors.DIM}{post['subreddit']} | ↑{post['upvotes']} | {post['num_comments']} comments{Colors.RESET}")

        comments = mock_comments

        print_result(f"  Comments fetched", len(comments))

        # Each comment becomes a source document (matching reddit-collector/db-helper.js)
        for comment in comments:
            documents.append({
                "source": "reddit",
                "author": comment["author"],
                "text": comment["text"],
                "url": post["url"],
                "published_at": None,
                "engagement_metrics": json.dumps({
                    "upvotes": comment["upvotes"],
                    "post_id": post["post_id"],
                    "post_title": post["title"],
                    "author": comment["author"],
                    "subreddit": post["subreddit"],
                }),
            })

        # Also include post body if it has content
        if post["body"] and len(post["body"].strip()) > 50:
            documents.append({
                "source": "reddit",
                "author": post["author"],
                "text": post["body"],
                "url": post["url"],
                "published_at": None,
                "engagement_metrics": json.dumps({
                    "upvotes": post["upvotes"],
                    "post_id": post["post_id"],
                    "post_title": post["title"],
                    "subreddit": post["subreddit"],
                    "type": "post_body",
                }),
            })
        print()

    print_divider()
    print_result("Total source documents", len(documents))
    if documents:
        total_chars = sum(len(d["text"]) for d in documents)
        print_result("Total text size", f"{total_chars:,} characters")
        print_result("Avg document size", f"{total_chars // len(documents):,} chars")

    return documents


# ══════════════════════════════════════════════════════════════════════════
# STAGE 2: DATABASE STORAGE
# ══════════════════════════════════════════════════════════════════════════

async def stage_store(documents: list[dict], query_id: str) -> list[str]:
    """Store source documents in the test database."""
    print_stage_header(2, "DATABASE STORAGE")

    if not documents:
        print_warning("No documents to store.")
        return []

    doc_ids = []
    async with async_session() as session:
        for doc in documents:
            doc_id = str(uuid.uuid4())
            await session.execute(
                sql_text("""
                    INSERT INTO source_documents (id, query_id, source, author, text, url, published_at, engagement_metrics, created_at)
                    VALUES (:id, :query_id, :source, :author, :text, :url, :published_at, :engagement_metrics, now())
                """),
                {
                    "id": doc_id,
                    "query_id": query_id,
                    "source": doc["source"],
                    "author": doc.get("author"),
                    "text": doc["text"],
                    "url": doc.get("url"),
                    "published_at": doc.get("published_at"),
                    "engagement_metrics": doc.get("engagement_metrics"),
                },
            )
            doc_ids.append(doc_id)

        await session.commit()

    print_result("Documents inserted", len(doc_ids))
    print_result("Source type", "reddit")
    print_result("Query ID", query_id)

    # Show a sample
    if doc_ids:
        print()
        print_info("Sample document IDs:")
        for did in doc_ids[:3]:
            print(f"     {did}")
        if len(doc_ids) > 3:
            print(f"     ... and {len(doc_ids) - 3} more")

    return doc_ids


# ══════════════════════════════════════════════════════════════════════════
# STAGE 3: CHUNKING & EMBEDDING
# ══════════════════════════════════════════════════════════════════════════

async def stage_chunk_and_embed(documents: list[dict], doc_ids: list[str], query_id: str) -> int:
    """Chunk documents, generate embeddings, store in document_chunks."""
    print_stage_header(3, "CHUNKING & EMBEDDING")

    if not documents:
        print_warning("No documents to chunk.")
        return 0

    total_chunks = 0
    docs_processed = 0

    async with async_session() as session:
        for doc, doc_id in zip(documents, doc_ids):
            chunks = chunk_text(doc["text"])
            if not chunks:
                continue

            chunk_texts = [c.chunk_text for c in chunks]
            embeddings = await embed_texts(chunk_texts)

            for chunk, embedding in zip(chunks, embeddings):
                chunk_id = str(uuid.uuid4())
                await session.execute(
                    sql_text("""
                        INSERT INTO document_chunks
                            (id, source_document_id, query_id, chunk_text, embedding, created_at)
                        VALUES (:id, :source_document_id, :query_id, :chunk_text, :embedding, now())
                    """),
                    {
                        "id": chunk_id,
                        "source_document_id": doc_id,
                        "query_id": query_id,
                        "chunk_text": chunk.chunk_text,
                        "embedding": str(embedding),
                    },
                )
                total_chunks += 1

            docs_processed += 1

        await session.commit()

    print_result("Documents processed", f"{docs_processed}/{len(documents)}")
    print_result("Total chunks created", total_chunks)
    print_result("Embedding dimensions", 384)
    if docs_processed > 0:
        print_result("Avg chunks per doc", f"{total_chunks / docs_processed:.1f}")

    return total_chunks


# ══════════════════════════════════════════════════════════════════════════
# STAGE 4: RAG RETRIEVAL
# ══════════════════════════════════════════════════════════════════════════

async def stage_retrieve(query: str, query_id: str, top_k: int = 8) -> list[dict]:
    """Run RAG retrieval — embed query, cosine similarity search."""
    print_stage_header(4, "RAG RETRIEVAL")

    print_info(f"Embedding query: \"{query}\"")
    query_embeddings = await embed_texts([query])
    query_embedding = query_embeddings[0]
    print_result("Query embedding", f"{len(query_embedding)} dimensions")

    print_info(f"Running cosine similarity search (top-{top_k})...")

    retrieved_chunks = []
    async with async_session() as session:
        result = await session.execute(
            sql_text("""
                SELECT
                    id,
                    chunk_text,
                    source_document_id,
                    (1 - (embedding <=> :query_embedding)) AS similarity
                FROM document_chunks
                WHERE query_id = :query_id
                ORDER BY embedding <=> :query_embedding
                LIMIT :top_k
            """),
            {
                "query_embedding": str(query_embedding),
                "query_id": query_id,
                "top_k": top_k,
            },
        )
        rows = result.fetchall()

        for row in rows:
            chunk = {
                "chunk_id": str(row.id),
                "chunk_text": row.chunk_text,
                "source_document_id": str(row.source_document_id),
                "similarity": float(row.similarity),
            }
            retrieved_chunks.append(chunk)

    print_divider()
    print_result("Chunks retrieved", len(retrieved_chunks))

    if retrieved_chunks:
        print_result("Best similarity", f"{retrieved_chunks[0]['similarity']:.4f}")
        print_result("Worst similarity", f"{retrieved_chunks[-1]['similarity']:.4f}")
        print()
        print_info("Top retrieved chunks:")
        for i, chunk in enumerate(retrieved_chunks[:5]):
            preview = chunk["chunk_text"][:120].replace("\n", " ")
            print(f"     {Colors.DIM}[{i+1}] sim={chunk['similarity']:.4f}{Colors.RESET} {preview}...")

    return retrieved_chunks


# ══════════════════════════════════════════════════════════════════════════
# STAGE 5: CLAIM EXTRACTION
# ══════════════════════════════════════════════════════════════════════════

async def stage_extract_claims(
    retrieved_chunks: list[dict], query_id: str
) -> list[dict]:
    """Extract claims from retrieved chunks using LLM."""
    print_stage_header(5, "CLAIM EXTRACTION")

    if not retrieved_chunks:
        print_warning("No chunks to extract claims from.")
        return []

    # Pre-filter
    eligible = []
    filtered_count = 0
    for chunk in retrieved_chunks:
        passes, reason = pre_filter(chunk["chunk_text"])
        if not passes:
            print(f"     {Colors.DIM}SKIP [{chunk['chunk_id'][:8]}] — {reason}{Colors.RESET}")
            filtered_count += 1
        else:
            eligible.append({"id": chunk["chunk_id"], "text": chunk["chunk_text"]})

    print_result("Chunks passing pre-filter", f"{len(eligible)}/{len(retrieved_chunks)}")
    print_result("Filtered out", filtered_count)

    if not eligible:
        print_warning("All chunks filtered out.")
        return []

    # Batch LLM calls
    all_claims = []
    total_batches = (len(eligible) + CLAIM_BATCH_SIZE - 1) // CLAIM_BATCH_SIZE

    for i in range(0, len(eligible), CLAIM_BATCH_SIZE):
        batch = eligible[i : i + CLAIM_BATCH_SIZE]
        batch_num = i // CLAIM_BATCH_SIZE + 1

        print_info(f"Batch {batch_num}/{total_batches} — {len(batch)} chunks → Groq LLM...")

        user_message = build_claim_user_message(batch)

        try:
            raw_response = await call_llm_for_claims(user_message)
            claims, parse_error = parse_claims_response(raw_response)

            if parse_error:
                print_warning(f"Parse error: {parse_error}")
            elif not claims:
                print_info("No claims extracted in this batch.")
            else:
                print_result(f"  Claims in batch {batch_num}", len(claims))
                for claim in claims:
                    all_claims.append({
                        "claim_text": claim.claim_text,
                        "entities": claim.entities,
                        "claim_type": claim.claim_type,
                        "confidence": claim.confidence,
                        "source_comment_id": claim.source_comment_id,
                    })
        except Exception as exc:
            print_error(f"LLM call failed: {exc}")

    # Save to DB
    if all_claims:
        async with async_session() as session:
            for claim in all_claims:
                await session.execute(
                    sql_text("""
                        INSERT INTO extracted_claims
                            (claim_text, entities, claim_type, direction, confidence,
                             is_sincere, source_comment_id, source_platform)
                        VALUES (:claim_text, :entities, :claim_type, :direction, :confidence,
                                :is_sincere, :source_comment_id, :source_platform)
                    """),
                    {
                        "claim_text": claim["claim_text"],
                        "entities": json.dumps(claim["entities"]),
                        "claim_type": claim["claim_type"],
                        "direction": None,
                        "confidence": claim["confidence"],
                        "is_sincere": True,
                        "source_comment_id": claim["source_comment_id"],
                        "source_platform": "reddit",
                    },
                )
            await session.commit()
        print_result("Claims saved to DB", len(all_claims))

    print_divider()
    print_result("Total claims extracted", len(all_claims))

    if all_claims:
        print()
        print_info("Extracted claims:")
        for i, claim in enumerate(all_claims):
            print(f"     {Colors.BOLD}[{i+1}]{Colors.RESET} {claim['claim_text'][:100]}")
            print(f"         Type: {claim['claim_type']} | Confidence: {claim['confidence']} | Entities: {claim['entities']}")

    return all_claims


# ══════════════════════════════════════════════════════════════════════════
# STAGE 6: SUMMARIZATION
# ══════════════════════════════════════════════════════════════════════════

async def stage_summarize(
    query: str, retrieved_chunks: list[dict], claims: list[dict]
) -> dict | None:
    """Generate a structured JSON report."""
    print_stage_header(6, "SUMMARIZATION")

    if not retrieved_chunks:
        print_warning("No chunks to summarize.")
        return None

    print_info(f"Summarizing {len(retrieved_chunks)} chunks and {len(claims)} claims...")

    user_prompt = build_summary_user_prompt(query, retrieved_chunks, claims if claims else None)

    try:
        raw_response = await llm_chat(
            system_prompt=SUMMARIZE_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            temperature=0.2,
            max_tokens=2048,
        )
        report = parse_summary_response(raw_response)

        if report is None:
            print_error("Failed to parse LLM response.")
            print(f"     Raw: {raw_response[:200]}...")
            return None

        print_divider()
        print_result("Sentiment", report.get("overall_sentiment", "unknown"))
        print_result("Themes", len(report.get("themes", [])))

        for i, theme in enumerate(report.get("themes", [])):
            print(f"     {i+1}. {theme.get('theme', 'N/A')}")

        print()
        print_result("Summary", "")
        summary = report.get("summary", "")
        words = summary.split()
        line = "     "
        for word in words:
            if len(line) + len(word) > 80:
                print(line)
                line = "     "
            line += word + " "
        if line.strip():
            print(line)

        return report

    except Exception as exc:
        print_error(f"Summarization failed: {exc}")
        return None


# ══════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════

async def run_pipeline(query: str):
    """Run the full Reddit pipeline."""
    query_id = str(uuid.uuid4())

    print_pipeline_header("Reddit", query)

    # Create queries row for FK constraints
    async with async_session() as session:
        await session.execute(
            sql_text("""
                INSERT INTO queries (id, query_text, status, sources_requested, created_at)
                VALUES (:id, :query_text, 'running', :sources, now())
            """),
            {"id": query_id, "query_text": query, "sources": ["reddit"]},
        )
        await session.commit()
    print_info(f"Created query record: {query_id}")

    # ── Stage 1: Fetch ──
    documents = await stage_fetch(query)
    if not documents:
        print_error("No documents fetched. Pipeline cannot continue.")
        return
    wait_for_next_stage()

    # ── Stage 2: Store ──
    doc_ids = await stage_store(documents, query_id)
    wait_for_next_stage()

    # ── Stage 3: Chunk & Embed ──
    total_chunks = await stage_chunk_and_embed(documents, doc_ids, query_id)
    if total_chunks == 0:
        print_error("No chunks created. Pipeline cannot continue.")
        return
    wait_for_next_stage()

    # ── Stage 4: Retrieve ──
    retrieved_chunks = await stage_retrieve(query, query_id)
    if not retrieved_chunks:
        print_error("No chunks retrieved. Pipeline cannot continue.")
        return
    wait_for_next_stage()

    # ── Stage 5: Extract Claims ──
    claims = await stage_extract_claims(retrieved_chunks, query_id)
    wait_for_next_stage()

    # ── Stage 6: Summarize ──
    report = await stage_summarize(query, retrieved_chunks, claims)

    # ── Final Summary ──
    stats = {
        "Query": query,
        "Query ID": query_id,
        "Documents fetched": len(documents),
        "Documents stored": len(doc_ids),
        "Chunks created": total_chunks,
        "Chunks retrieved": len(retrieved_chunks),
        "Claims extracted": len(claims),
        "Report generated": "Yes" if report else "No",
        "Sentiment": report.get("overall_sentiment", "N/A") if report else "N/A",
    }
    print_pipeline_footer("Reddit", stats)


def main():
    if len(sys.argv) < 2:
        print("Usage: python run_pipeline.py \"<search query>\"")
        print("Example: python run_pipeline.py \"best noise cancelling headphones 2024\"")
        sys.exit(1)

    query = sys.argv[1]
    asyncio.run(run_pipeline(query))


if __name__ == "__main__":
    main()
