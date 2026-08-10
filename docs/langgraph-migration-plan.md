# LangGraph Migration Plan

## Overview

This document describes the plan to migrate research pipeline orchestration from the Node.js in-memory worker (`researchWorker.js`) to a Python FastAPI service powered by LangGraph. This is the design for **Milestone 2** (Wikipedia source) with the architecture for future milestones (Reddit, YouTube, News).

---

## 1. Current State (Node.js)

### How it works today

The Node gateway (`backend-node`) handles the full lifecycle:

1. **Job submission** — `query.controller.js` receives a POST, writes a `queries` row to Postgres, and pushes a job object into an in-memory FIFO queue in `researchWorker.js`.

2. **Worker loop** — `researchWorker.js` processes one job at a time. For each job, it runs scraper functions sequentially based on `sources[]`:
   - **Reddit** — `reddit-collector/scraper.js` uses Playwright to search Reddit, extract posts + comments, write to Postgres via `db-helper.js`
   - **YouTube** — `youtube-collector/scraper.js` uses the YouTube Data API for search/metadata, `youtube-transcript` for transcripts, writes to Postgres

3. **Checkpointing** — Each scraper calls `checkpoint.js` (`saveCheckpoint` / `loadCheckpoint`) to persist the last successfully processed item ID per `(jobId, source)`. On retry, scrapers skip already-processed items.

4. **SSE streaming** — The worker emits events via a Node `EventEmitter`. `query.controller.js` forwards these to connected SSE clients. Event shape: `{ type, jobId, source, status, counts, error, timestamp }`.

5. **Error handling** — Individual source failures are non-fatal (recorded in `sources_failed[]`). The job still completes with partial results. Only unexpected top-level errors mark the job as `error`.

### What each scraper does

| Source | Module | Data fetched | Items per query |
|--------|--------|--------------|-----------------|
| Reddit | `reddit-collector/scraper.js` | Post title/body, top-level + nested comments, upvotes | ~7 posts, ~7 comments each |
| YouTube | `youtube-collector/scraper.js` | Video metadata, cleaned transcripts, top comments | ~5 videos |

---

## 2. Target State (Python + LangGraph)

### Architecture

```
Node Gateway (auth, routing, SSE relay)
    ↓ HTTP (contract: /api/v1/jobs)
Python FastAPI + LangGraph
    ↓
StateGraph: query → [fetch nodes] → store → (future: summarize → verify) → END
    ↓
PostgreSQL + pgvector
```

### LangGraph State Schema

```python
class ResearchState(TypedDict, total=False):
    job_id: str
    query: str
    sources_to_fetch: list[str]       # ["wikipedia", "reddit", "youtube"]
    raw_documents: list[SourceDoc]    # accumulated across all fetch nodes
    failed_sources: list[str]         # sources that errored (non-fatal)
    status: str                       # pending | fetching | storing | done | error
    results: dict                     # final output per source
```

### Node Boundaries

| Node | Responsibility | Input from state | Output to state |
|------|---------------|------------------|-----------------|
| `wikipedia_fetch` | Fetch Wikipedia articles for query | `query` | `raw_documents` (appended) |
| `reddit_fetch` (future) | Fetch Reddit posts + comments | `query` | `raw_documents` (appended) |
| `youtube_fetch` (future) | Fetch YouTube videos + transcripts | `query` | `raw_documents` (appended) |
| `store_documents` | Chunk text, generate embeddings, write to Postgres | `raw_documents` | `results` (counts) |
| `summarize` (future) | RAG retrieval + LLM summarization | `results` | `themes`, `sentiment` |
| `verify_claims` (future) | Cross-verify extracted claims | `themes` | `verified_claims` |

**Design decision:** One node per source (not shared fetch + per-source parse), because each source has fundamentally different fetch mechanisms (API vs scraping vs SDK) and error handling needs. Shared logic (chunking, embedding, storage) lives in `store_documents`.

### Checkpointing Strategy

**LangGraph's built-in checkpointing replaces `checkpoint.js`:**

- LangGraph persists state after each node execution automatically (using `MemorySaver` in development, `PostgresSaver` in production).
- If a graph run fails mid-pipeline, it can be resumed from the last successful node — no need for manual item-level checkpoint tracking.
- The existing `checkpoint.js` (item-level resume within a single source) will remain available in Node for the transition period but becomes unnecessary once each fetch node handles its own internal retry logic.

**Key difference:** Node's `checkpoint.js` tracks *which item within a scrape* was last processed (e.g., "post 5 of 7"). LangGraph's checkpointing tracks *which node in the graph* last completed. For Wikipedia (which fetches a small number of articles per query), node-level checkpointing is sufficient. For Reddit/YouTube (large item counts), the fetch nodes themselves should implement internal batching with try/except per item, matching the existing scraper pattern.

---

## 3. Node's Role After Migration

> **Node shrinks to auth/routing/SSE relay ONLY. No scraping or AI logic should remain in Node after full migration.**

Specifically:
- `query.controller.js` calls `pythonServiceClient.submitJob()` instead of `addJob()` to the in-memory queue
- `streamJobProgress()` forwards SSE events from Python's SSE endpoint to the frontend, instead of listening to `researchWorker.js`'s EventEmitter
- `researchWorker.js` is deprecated (kept for rollback) but no longer invoked
- `checkpoint.js` remains in the codebase (used by existing Reddit/YouTube scrapers during the transition) but is not used for new sources
- All LLM calls, embedding, and data fetching logic lives exclusively in Python

---

## 4. Migration Order

### Phase 1 (This milestone — M2)
- **Wikipedia source only**
- `wikipedia_fetch` node → `store_documents` node
- End-to-end: Node → Python → Wikipedia API → Postgres → SSE back to Node
- No LLM calls yet — just fetch + chunk + embed + store

### Phase 2 (Future — M3)
- Add `reddit_fetch` node — port logic from `reddit-collector/scraper.js`
- Add `youtube_fetch` node — port logic from `youtube-collector/scraper.js`
- Run fetch nodes in parallel via LangGraph's fan-out/fan-in pattern
- Add `summarize` node with RAG retrieval + Groq LLM

### Phase 3 (Future — M4+)
- Add `verify_claims` node — cross-verify against news/fact-check sources
- Add `news_fetch` node
- Full pipeline: plan → fetch → embed → summarize → verify → report

### What is explicitly NOT in this phase
- Reddit scraper migration
- YouTube scraper migration
- LLM summarization / claim extraction
- News/fact-check verification
- `query_planner` node (source routing)

---

## 5. Risk & Mitigations

| Risk | Mitigation |
|------|-----------|
| Wikipedia API rate limits | `wikipedia-api` package handles retries; we fetch ≤5 articles per query |
| Embedding model mismatch with existing DB schema | RESOLVED — unified on `all-MiniLM-L6-v2` (384 dims); schema uses `VECTOR(384)` |
| SSE format mismatch between Python and Node | Python emits `data: {JSON}\n\n` — tested against Node's `pythonServiceClient.js` parser byte-for-byte |
| Dual-write period (both Node and Python can write to same tables) | Wikipedia writes to `source_documents` with `source='wikipedia'` — no overlap with existing Reddit/YouTube data |
