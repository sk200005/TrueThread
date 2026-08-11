"""
YouTube Pipeline Test — Full end-to-end pipeline test.

Stages:
    1. FETCH    — Search YouTube, get metadata, transcripts, comments
    2. STORE    — Insert source_documents into test DB
    3. CHUNK    — Split + embed + store document_chunks
    4. RETRIEVE — RAG cosine similarity search
    5. EXTRACT  — LLM claim extraction from chunks
    6. SUMMARIZE — LLM structured report generation

Usage:
    python run_pipeline.py "iPhone 16 Pro review"
    python run_pipeline.py "best laptop for programming 2024"

Requirements:
    pip install youtube-transcript-api google-api-python-client
"""

from __future__ import annotations

import asyncio
import json
import sys
import os
import uuid

# Add parent directory to path so we can import test_config
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

# Load env for YouTube API key
_env_path = os.path.join(os.path.dirname(__file__), "..", "..", "BackEnd", ".env")
if os.path.exists(_env_path):
    load_dotenv(_env_path)

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

# ── YouTube API helpers ───────────────────────────────────────────────────

YOUTUBE_API_KEY = os.environ.get("YOUTUBE_API_KEY", "")
TARGET_VIDEOS = 5


def search_youtube_videos(query: str, api_key: str) -> list[str]:
    """Search YouTube for video IDs matching the query."""
    import requests

    url = "https://www.googleapis.com/youtube/v3/search"
    params = {
        "part": "snippet",
        "q": query,
        "type": "video",
        "maxResults": 10,
        "order": "relevance",
        "key": api_key,
    }
    try:
        resp = requests.get(url, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        items = data.get("items", [])
        return [item["id"]["videoId"] for item in items if "videoId" in item.get("id", {})]
    except Exception as exc:
        print_error(f"YouTube search failed: {exc}")
        return []


def get_video_metadata(video_ids: list[str], api_key: str) -> list[dict]:
    """Fetch metadata for a batch of video IDs."""
    import requests

    if not video_ids:
        return []

    url = "https://www.googleapis.com/youtube/v3/videos"
    params = {
        "part": "snippet,statistics,contentDetails",
        "id": ",".join(video_ids),
        "key": api_key,
    }
    try:
        resp = requests.get(url, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()

        videos = []
        for item in data.get("items", []):
            snippet = item.get("snippet", {})
            stats = item.get("statistics", {})
            videos.append({
                "videoId": item["id"],
                "title": snippet.get("title", ""),
                "channel": snippet.get("channelTitle", ""),
                "publishedAt": snippet.get("publishedAt", ""),
                "description": snippet.get("description", ""),
                "views": int(stats.get("viewCount", 0)),
                "likes": int(stats.get("likeCount", 0)),
                "duration": item.get("contentDetails", {}).get("duration", ""),
                "url": f"https://www.youtube.com/watch?v={item['id']}",
            })

        # Rank by views (simple relevance proxy)
        videos.sort(key=lambda v: v["views"], reverse=True)
        return videos

    except Exception as exc:
        print_error(f"YouTube metadata fetch failed: {exc}")
        return []


def get_video_transcript(video_id: str) -> str | None:
    """Fetch transcript for a YouTube video using youtube-transcript-api."""
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
        
        ytt_api = YouTubeTranscriptApi()
        transcript_list = ytt_api.list(video_id)
        
        transcript = None
        try:
            # Try English first
            transcript = transcript_list.find_transcript(['en', 'en-US', 'en-GB'])
        except Exception:
            # Fallback to the first available transcript
            for t in transcript_list:
                transcript = t
                break
                
        if not transcript:
            raise Exception("No transcripts found")
            
        # Translate to English if it's in another language
        if transcript.language_code not in ['en', 'en-US', 'en-GB']:
            try:
                transcript = transcript.translate('en')
            except Exception:
                pass # Just use raw if translation fails
                
        transcript_data = transcript.fetch()
        
        # Handle both dicts and FetchedTranscriptSnippet objects
        texts = []
        for entry in transcript_data:
            if isinstance(entry, dict) and "text" in entry:
                texts.append(entry["text"])
            elif hasattr(entry, "text"):
                texts.append(entry.text)
        
        full_text = " ".join(texts)
        return full_text
    except Exception as exc:
        print_warning(f"Transcript unavailable for {video_id}: {exc}")
        return None


def get_video_comments(video_id: str, api_key: str, max_comments: int = 5) -> list[dict]:
    """Fetch top comments for a video."""
    import requests

    url = "https://www.googleapis.com/youtube/v3/commentThreads"
    params = {
        "part": "snippet",
        "videoId": video_id,
        "maxResults": max_comments,
        "order": "relevance",
        "textFormat": "plainText",
        "key": api_key,
    }
    try:
        resp = requests.get(url, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()

        comments = []
        for item in data.get("items", []):
            snippet = item.get("snippet", {}).get("topLevelComment", {}).get("snippet", {})
            text = (snippet.get("textDisplay") or "").strip()
            if text:
                comments.append({
                    "author": snippet.get("authorDisplayName", ""),
                    "text": text,
                    "likes": snippet.get("likeCount", 0),
                    "publishedAt": snippet.get("publishedAt", ""),
                })
        return comments

    except Exception as exc:
        print_warning(f"Comments unavailable for {video_id}: {exc}")
        return []


# ══════════════════════════════════════════════════════════════════════════
# STAGE 1: DATA FETCHING
# ══════════════════════════════════════════════════════════════════════════

async def stage_fetch(query: str) -> list[dict]:
    """Fetch YouTube videos with transcripts and comments."""
    print_stage_header(1, "DATA FETCHING (YouTube)")

    if not YOUTUBE_API_KEY:
        print_error("YOUTUBE_API_KEY not set in environment!")
        print_info("Set it in BackEnd/.env or export YOUTUBE_API_KEY=...")
        return []

    print_info(f"Searching YouTube for: \"{query}\"")

    video_ids = await asyncio.to_thread(search_youtube_videos, query, YOUTUBE_API_KEY)
    if not video_ids:
        print_warning("No YouTube videos found.")
        return []

    print_result("Video IDs found", len(video_ids))

    # Fetch metadata
    print_info("Fetching video metadata...")
    videos = await asyncio.to_thread(get_video_metadata, video_ids, YOUTUBE_API_KEY)
    print_result("Videos with metadata", len(videos))
    print()

    # Process each video: get transcript + comments
    documents = []
    videos_collected = 0

    for video in videos:
        if videos_collected >= TARGET_VIDEOS:
            break

        print_info(f"Video: \"{video['title'][:60]}\"")
        print(f"     {Colors.DIM}{video['channel']} | {video['views']:,} views | {video['likes']:,} likes{Colors.RESET}")

        # Get transcript (mandatory for this pipeline)
        transcript = await asyncio.to_thread(get_video_transcript, video["videoId"])
        if not transcript:
            print_warning("  Skipped — no transcript available")
            print()
            continue

        print_result("  Transcript", f"{len(transcript):,} chars")

        # Store transcript as a source document
        documents.append({
            "source": "youtube",
            "author": video["channel"],
            "text": transcript,
            "url": video["url"],
            "published_at": video["publishedAt"],
            "engagement_metrics": json.dumps({
                "video_id": video["videoId"],
                "video_title": video["title"],
                "channel": video["channel"],
                "views": video["views"],
                "likes": video["likes"],
                "type": "transcript",
            }),
        })

        # Get comments
        comments = await asyncio.to_thread(get_video_comments, video["videoId"], YOUTUBE_API_KEY, 5)
        print_result("  Comments", len(comments))

        for comment in comments:
            if comment["text"].strip():
                documents.append({
                    "source": "youtube",
                    "author": comment["author"],
                    "text": comment["text"],
                    "url": video["url"],
                    "published_at": comment.get("publishedAt"),
                    "engagement_metrics": json.dumps({
                        "video_id": video["videoId"],
                        "video_title": video["title"],
                        "channel": video["channel"],
                        "type": "comment",
                        "likes": comment["likes"],
                    }),
                })

        videos_collected += 1
        print()

    print_divider()
    print_result("Videos processed", videos_collected)
    print_result("Total source documents", len(documents))
    if documents:
        total_chars = sum(len(d["text"]) for d in documents)
        print_result("Total text size", f"{total_chars:,} characters")

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
                    "published_at": __import__("datetime").datetime.fromisoformat(doc.get("published_at").replace("Z", "+00:00")) if doc.get("published_at") else None,
                    "engagement_metrics": doc.get("engagement_metrics"),
                },
            )
            doc_ids.append(doc_id)

        await session.commit()

    # Count transcripts vs comments
    transcript_count = sum(
        1 for d in documents
        if "transcript" in (d.get("engagement_metrics") or "")
    )
    comment_count = len(documents) - transcript_count

    print_result("Documents inserted", len(doc_ids))
    print_result("Transcripts", transcript_count)
    print_result("Comments", comment_count)
    print_result("Query ID", query_id)

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
                        "source_platform": "youtube",
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
    """Run the full YouTube pipeline."""
    query_id = str(uuid.uuid4())

    print_pipeline_header("YouTube", query)

    # Create queries row for FK constraints
    async with async_session() as session:
        await session.execute(
            sql_text("""
                INSERT INTO queries (id, query_text, status, sources_requested, created_at)
                VALUES (:id, :query_text, 'running', :sources, now())
            """),
            {"id": query_id, "query_text": query, "sources": ["youtube"]},
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
    print_pipeline_footer("YouTube", stats)


def main():
    if len(sys.argv) < 2:
        print("Usage: python run_pipeline.py \"<search query>\"")
        print("Example: python run_pipeline.py \"iPhone 16 Pro review\"")
        sys.exit(1)

    query = sys.argv[1]
    asyncio.run(run_pipeline(query))


if __name__ == "__main__":
    main()
