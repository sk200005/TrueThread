"""
Wikipedia Pipeline Test — Full end-to-end pipeline test.

Stages:
    1. FETCH    — Search Wikipedia + fetch full articles
    2. STORE    — Insert source_documents into test DB
    3. CHUNK    — Split + embed + store document_chunks
    4. RETRIEVE — RAG cosine similarity search
    5. EXTRACT  — LLM claim extraction from chunks
    6. SUMMARIZE — LLM structured report generation

Usage:
    python run_pipeline.py "Python programming language"
    python run_pipeline.py "artificial intelligence"
"""

from __future__ import annotations

import asyncio
import json
import sys
import os
import uuid

# Add parent directory to path so we can import test_config
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests
import wikipediaapi

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

# ── Wikipedia API setup ───────────────────────────────────────────────────
MAX_ARTICLES = 5
_wiki = wikipediaapi.Wikipedia(
    user_agent="ReSearchPlatform/0.1 (research-project; contact@example.com)",
    language="en",
)


# ── LLM title filter ──────────────────────────────────────────────────────

_TITLE_FILTER_SYSTEM = (
    "You are a search result filter. Given a user's research query and a list "
    "of Wikipedia article titles returned by a search engine, return ONLY the "
    "titles that are genuinely about the query subject. Exclude results that "
    "share a similar name but refer to a different person, place, or concept "
    "(e.g. exclude 'Georges Sorel' when the query is 'George Soros'). "
    "Respond with ONLY a JSON array of strings — exact title strings, no commentary, "
    "no markdown fences, no explanation."
)


async def filter_relevant_titles(
    query: str,
    titles: list[str],
    top_n: int = 3,
) -> list[str]:
    """
    Use the Groq LLM to select only the most relevant Wikipedia titles for `query`.

    Args:
        query:  The original user research query.
        titles: Raw list of title strings returned by the Wikipedia search API.
        top_n:  Maximum number of titles to keep.

    Returns:
        A filtered, capped list of title strings. Falls back to ``titles[:top_n]``
        if the LLM call fails or returns an unparseable/empty response.
    """
    if not titles:
        return []

    fallback = titles[:top_n]

    user_prompt = (
        f'Research query: "{query}"\n'
        f'Wikipedia search results: {json.dumps(titles)}\n'
        f'Return a JSON array of the titles that are genuinely relevant to the query. '
        f'Limit to at most {top_n} titles. '
        f'Respond with ONLY the JSON array — no prose, no markdown.'
    )

    try:
        raw = await llm_chat(
            system_prompt=_TITLE_FILTER_SYSTEM,
            user_prompt=user_prompt,
            temperature=0.0,
            max_tokens=256,
        )
    except Exception as exc:
        print_warning(f"LLM title filter call failed ({exc}). Using top-{top_n} fallback.")
        return fallback

    # Parse defensively — strip fences if the model adds them
    cleaned = raw.strip()
    cleaned = re.sub(r'^```(?:json)?\s*', '', cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r'\s*```$', '', cleaned, flags=re.IGNORECASE)
    cleaned = cleaned.strip()

    try:
        parsed = json.loads(cleaned)
        if not isinstance(parsed, list) or len(parsed) == 0:
            raise ValueError(f"unexpected shape: {type(parsed).__name__}")
        # Keep only strings, cap at top_n
        result = [t for t in parsed if isinstance(t, str)][:top_n]
        if not result:
            raise ValueError("no string titles in parsed list")
        return result
    except Exception as exc:
        print_warning(f"LLM title filter parse failed ({exc}). Using top-{top_n} fallback.")
        return fallback


def search_wikipedia(query: str, limit: int = MAX_ARTICLES) -> list[str]:
    """Search Wikipedia for article titles matching the query."""
    url = "https://en.wikipedia.org/w/api.php"
    params = {
        "action": "opensearch",
        "search": query,
        "limit": limit,
        "namespace": 0,
        "format": "json",
    }
    headers = {
        "User-Agent": "ReSearchPlatform/0.1 (research-project; contact@example.com)",
    }
    try:
        resp = requests.get(url, params=params, headers=headers, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        return data[1] if len(data) > 1 else []
    except Exception as exc:
        print_error(f"Wikipedia search failed: {exc}")
        return []


def fetch_wikipedia_page(title: str) -> dict | None:
    """Fetch a Wikipedia page and return a source document dict."""
    page = _wiki.page(title)
    if not page.exists():
        return None

    # Skip disambiguation pages
    if "disambiguation" in (page.summary or "").lower() and len(page.text) < 500:
        return None

    text = page.text
    if not text or len(text.strip()) < 100:
        return None

    return {
        "source": "wikipedia",
        "author": None,
        "text": text,
        "url": page.fullurl,
        "published_at": None,
        "engagement_metrics": None,
    }


# ══════════════════════════════════════════════════════════════════════════
# STAGE 1: DATA FETCHING
# ══════════════════════════════════════════════════════════════════════════

async def stage_fetch(query: str) -> list[dict]:
    """Fetch Wikipedia articles for the query."""
    print_stage_header(1, "DATA FETCHING (Wikipedia)")

    print_info(f"Searching Wikipedia for: \"{query}\"")
    titles = await asyncio.to_thread(search_wikipedia, query)

    if not titles:
        print_warning("No Wikipedia articles found.")
        return []

    print_result("Raw search results", f"{len(titles)} titles")
    for i, title in enumerate(titles):
        print(f"     {i+1}. {title}")

    # ── LLM title filter ─────────────────────────────────────────────────
    print()
    print_info(f"Running LLM title filter (keeping top 3 relevant titles)...")
    filtered_titles = await filter_relevant_titles(query, titles, top_n=3)
    print_result("Titles after LLM filter", f"{len(filtered_titles)} titles")
    for i, title in enumerate(filtered_titles):
        print(f"     {i+1}. {title}")

    print()
    print_info("Fetching full article text...")
    documents = []
    for title in filtered_titles:
        doc = await asyncio.to_thread(fetch_wikipedia_page, title)
        if doc:
            documents.append(doc)
            print_result(f"  {title}", f"{len(doc['text']):,} chars")
        else:
            print_warning(f"  Skipped: {title} (no content or disambiguation)")

    print_divider()
    print_result("Total documents fetched", len(documents))
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
                    INSERT INTO source_documents (id, query_id, source, author, text, url, published_at, created_at)
                    VALUES (:id, :query_id, :source, :author, :text, :url, :published_at, now())
                """),
                {
                    "id": doc_id,
                    "query_id": query_id,
                    "source": doc["source"],
                    "author": doc.get("author"),
                    "text": doc["text"],
                    "url": doc.get("url"),
                    "published_at": doc.get("published_at"),
                },
            )
            doc_ids.append(doc_id)
            print_result(f"  Stored", f"{doc_id[:8]}... ({doc['source']}, {len(doc['text']):,} chars)")

        await session.commit()

    print_divider()
    print_result("Documents inserted", len(doc_ids))
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

    async with async_session() as session:
        for doc, doc_id in zip(documents, doc_ids):
            # Chunk the text
            chunks = chunk_text(doc["text"])
            if not chunks:
                print_warning(f"  Doc {doc_id[:8]}: 0 chunks produced")
                continue

            chunk_texts = [c.chunk_text for c in chunks]

            # Generate embeddings
            embeddings = await embed_texts(chunk_texts)

            # Insert into DB
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

            url_short = (doc.get("url") or "N/A")[-40:]
            print_result(
                f"  Doc {doc_id[:8]}",
                f"{len(chunks)} chunks, ~{chunks[0].token_count} tokens/chunk | ...{url_short}"
            )

        await session.commit()

    print_divider()
    print_result("Total chunks created", total_chunks)
    print_result("Embedding dimensions", 384)
    print_result("Chunk size", "~800 chars with 150 overlap")

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
    retrieved_chunks: list[dict], query_id: str, source_platform: str = "wikipedia"
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
        print_warning("All chunks filtered out. No LLM calls needed.")
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
                    claim_dict = {
                        "claim_text": claim.claim_text,
                        "entities": claim.entities,
                        "claim_type": claim.claim_type,
                        "confidence": claim.confidence,
                        "source_comment_id": claim.source_comment_id,
                    }
                    all_claims.append(claim_dict)
        except Exception as exc:
            print_error(f"LLM call failed: {exc}")

    # Save claims to DB
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
                        "source_platform": source_platform,
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
    """Generate a structured JSON report from chunks and claims."""
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

        themes = report.get("themes", [])
        for i, theme in enumerate(themes):
            print(f"     {i+1}. {theme.get('theme', 'N/A')}")

        print()
        print_result("Summary", "")
        summary = report.get("summary", "")
        # Word-wrap the summary
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
    """Run the full Wikipedia pipeline."""
    query_id = str(uuid.uuid4())

    print_pipeline_header("Wikipedia", query)

    # Create a queries row so FK constraints are satisfied
    async with async_session() as session:
        await session.execute(
            sql_text("""
                INSERT INTO queries (id, query_text, status, sources_requested, created_at)
                VALUES (:id, :query_text, 'running', :sources, now())
            """),
            {"id": query_id, "query_text": query, "sources": ["wikipedia"]},
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
    claims = await stage_extract_claims(retrieved_chunks, query_id, "wikipedia")
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
    print_pipeline_footer("Wikipedia", stats)


def main():
    if len(sys.argv) < 2:
        print("Usage: python run_pipeline.py \"<search query>\"")
        print("Example: python run_pipeline.py \"Python programming language\"")
        sys.exit(1)

    query = sys.argv[1]
    asyncio.run(run_pipeline(query))


if __name__ == "__main__":
    main()
