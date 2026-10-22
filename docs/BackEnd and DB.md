# Backend Architecture & Database Schema
## Re-Search Application — Implementation Reference

This document is meant to be fed directly to an AI coding assistant (Claude Code, Cursor, etc.) as grounding context when scaffolding the backend. It defines the service boundaries, API contracts, database schema, and LangGraph pipeline state so generated code stays consistent across sessions.

---

## 1. Service Boundaries

Two backend services, one database.

```
/backend-node        → Express.js — Gateway, Auth, Queue, Postgres writes for user-facing data
/backend-python       → FastAPI — LangGraph pipeline, all AI/data-fetching logic
```

**Rule of thumb for the AI assistant:** Node never calls LLMs or external data-source APIs directly. Python never handles user auth or session state. Node calls Python over HTTP for one thing only: "run research pipeline for this query" and "get status of this job."

---

## 2. Node.js / Express Service (`/backend-node`)

### 2.1 Folder Structure
```
backend-node/
├── src/
│   ├── routes/
│   │   ├── auth.routes.js
│   │   ├── query.routes.js
│   │   └── report.routes.js
│   ├── controllers/
│   │   ├── auth.controller.js
│   │   ├── query.controller.js
│   │   └── report.controller.js
│   ├── middleware/
│   │   ├── authenticate.js
│   │   └── errorHandler.js
│   ├── queues/
│   │   └── researchQueue.js        # BullMQ producer
│   ├── db/
│   │   ├── prisma/schema.prisma    # or knex/sequelize, see §4
│   │   └── client.js
│   ├── services/
│   │   └── pythonServiceClient.js  # axios wrapper calling FastAPI
│   └── app.js
├── package.json
└── .env
```

### 2.2 API Endpoints (Node — user-facing)

| Method | Route | Purpose |
|---|---|---|
| POST | `/api/auth/signup` | Create user |
| POST | `/api/auth/login` | Return JWT |
| POST | `/api/queries` | Submit a new research query → creates job in queue → returns `job_id` |
| GET | `/api/queries/:jobId/status` | Poll job status (or use SSE, see below) |
| GET | `/api/queries/:jobId/stream` | SSE endpoint — streams pipeline progress events |
| GET | `/api/reports` | List logged-in user's past reports |
| GET | `/api/reports/:reportId` | Get single full report |
| DELETE | `/api/reports/:reportId` | Delete a report |

### 2.3 Job Queue Flow (BullMQ + Redis)
1. `POST /api/queries` → validates input → pushes job `{ userId, queryText, jobId }` to `researchQueue` → responds immediately with `jobId`.
2. A worker process (can live in Node, just triggers Python) picks up the job → calls `POST http://python-service/pipeline/run` on FastAPI with the query.
3. FastAPI runs the LangGraph pipeline **async** and emits progress via a webhook back to Node (`POST /api/internal/progress`), or Node polls a `GET /pipeline/status/:jobId` endpoint on FastAPI.
4. Progress events get pushed to the frontend over SSE using the `jobId` as the channel key.
5. On completion, FastAPI returns the final structured report → Node writes it to Postgres → marks job complete.

---

## 3. FastAPI / LangGraph Service (`/backend-python`)

### 3.1 Folder Structure
```
backend-python/
├── app/
│   ├── main.py
│   ├── api/
│   │   └── pipeline_routes.py       # /pipeline/run, /pipeline/status/{job_id}
│   ├── graph/
│   │   ├── state.py                 # LangGraph state schema (Pydantic)
│   │   ├── nodes/
│   │   │   ├── query_planner.py
│   │   │   ├── fetch_reddit.py
│   │   │   ├── fetch_youtube.py
│   │   │   ├── fetch_wikipedia.py
│   │   │   ├── fetch_news.py
│   │   │   ├── fetch_ecommerce.py   # optional/pluggable, P1
│   │   │   ├── normalize.py
│   │   │   ├── embed_and_store.py
│   │   │   ├── summarize.py
│   │   │   └── verify_claims.py
│   │   └── graph_builder.py         # wires nodes + edges together
│   ├── services/
│   │   ├── embeddings.py
│   │   ├── vector_store.py          # pgvector queries
│   │   └── llm_client.py
│   ├── db/
│   │   └── models.py                # SQLAlchemy models, mirrors Node's schema
│   └── core/
│       └── config.py
├── requirements.txt
└── .env
```

### 3.2 LangGraph State Schema (`state.py`)

This is the single object passed between every node — the AI assistant should treat this as the contract for the whole pipeline.

```python
from typing import TypedDict, Literal, Optional
from pydantic import BaseModel

class SourceDocument(BaseModel):
    source: Literal["reddit", "youtube", "wikipedia", "news", "amazon", "flipkart"]
    author: Optional[str]
    text: str
    url: str
    timestamp: Optional[str]
    engagement_metrics: Optional[dict] = None

class ThemeSummary(BaseModel):
    theme: str
    sentiment: Literal["positive", "negative", "neutral", "mixed"]
    representative_quotes: list[dict]  # {text, source, url}

class ClaimVerification(BaseModel):
    claim: str
    status: Literal["verified", "disputed", "unverified", "inconclusive"]
    verification_source: Optional[str]
    explanation: str

class ResearchState(TypedDict):
    job_id: str
    query: str
    sources_to_fetch: list[str]          # decided by query_planner node
    sub_queries: dict[str, str]          # per-platform optimized queries
    raw_documents: list[SourceDocument]  # accumulated across fetch nodes
    failed_sources: list[str]
    sentiment_summary: dict              # {"positive": 62, "negative": 28, "neutral": 10}
    themes: list[ThemeSummary]
    verified_claims: list[ClaimVerification]
    status: Literal["planning", "fetching", "summarizing", "verifying", "done", "error"]
```

### 3.3 Graph Structure (`graph_builder.py` — conceptual)

```
START
  → query_planner
  → [fetch_reddit, fetch_youtube, fetch_wikipedia, fetch_news]  (parallel branch)
  → normalize
  → embed_and_store
  → summarize
  → verify_claims
  → END
```
- Each `fetch_*` node wraps its call in try/except; on failure, appends to `failed_sources` instead of raising — the graph must never hard-fail because one source is down.
- `fetch_ecommerce` (Amazon/Flipkart) is NOT in the default graph — add it as a conditional edge only if `query_planner` classifies the query as "product" type.

### 3.4 API Endpoints (FastAPI — internal, called only by Node)

| Method | Route | Purpose |
|---|---|---|
| POST | `/pipeline/run` | Kick off LangGraph run for a query, returns immediately with `job_id` |
| GET | `/pipeline/status/{job_id}` | Current state/progress of a running pipeline |
| GET | `/pipeline/result/{job_id}` | Final structured report once done |

---

## 4. Database Schema (PostgreSQL + pgvector)

### 4.1 Enable extension
```sql
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
```

### 4.2 Tables

```sql
-- Users
CREATE TABLE users (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    email VARCHAR(255) UNIQUE NOT NULL,
    password_hash VARCHAR(255),          -- null if OAuth-only
    name VARCHAR(255),
    created_at TIMESTAMPTZ DEFAULT now()
);

-- One row per research request
CREATE TABLE queries (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id UUID REFERENCES users(id) ON DELETE CASCADE,
    query_text TEXT NOT NULL,
    status VARCHAR(20) DEFAULT 'pending', -- pending | running | done | error
    sources_requested TEXT[],             -- e.g. {reddit,youtube,wikipedia,news}
    sources_failed TEXT[],
    created_at TIMESTAMPTZ DEFAULT now(),
    completed_at TIMESTAMPTZ
);

-- Raw fetched documents, before/after chunking — kept for traceability
CREATE TABLE source_documents (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    query_id UUID REFERENCES queries(id) ON DELETE CASCADE,
    source VARCHAR(50) NOT NULL,          -- reddit | youtube | wikipedia | news | amazon | flipkart
    author VARCHAR(255),
    text TEXT NOT NULL,
    url TEXT,
    published_at TIMESTAMPTZ,
    engagement_metrics JSONB,             -- upvotes, likes, view count, etc.
    created_at TIMESTAMPTZ DEFAULT now()
);

-- Chunked + embedded text for RAG retrieval
CREATE TABLE document_chunks (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    source_document_id UUID REFERENCES source_documents(id) ON DELETE CASCADE,
    query_id UUID REFERENCES queries(id) ON DELETE CASCADE,  -- denormalized for faster scoped search
    chunk_text TEXT NOT NULL,
    embedding VECTOR(1536),               -- dimension depends on embedding model used
    created_at TIMESTAMPTZ DEFAULT now()
);

-- Final generated report per query (1:1 with queries once complete)
CREATE TABLE reports (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    query_id UUID REFERENCES queries(id) ON DELETE CASCADE UNIQUE,
    sentiment_summary JSONB,              -- {"positive": 62, "negative": 28, "neutral": 10}
    themes JSONB,                         -- array of ThemeSummary objects
    verified_claims JSONB,                -- array of ClaimVerification objects
    created_at TIMESTAMPTZ DEFAULT now()
);

-- Indexes
CREATE INDEX idx_queries_user_id ON queries(user_id);
CREATE INDEX idx_source_documents_query_id ON source_documents(query_id);
CREATE INDEX idx_document_chunks_query_id ON document_chunks(query_id);

-- Vector similarity index (IVFFlat — good enough at small/medium scale)
CREATE INDEX idx_document_chunks_embedding ON document_chunks
    USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);
ALTER DATABASE postgres SET ivfflat.probes = 3;
```

### 4.3 Example Retrieval Query (RAG step)

```sql
SELECT chunk_text, source_document_id
FROM document_chunks
WHERE query_id = $1
ORDER BY embedding <=> $2   -- $2 = query embedding vector
LIMIT 8;
```

### 4.4 Notes on Schema Design
- `source_documents` and `document_chunks` are kept separate so every generated claim/quote can be traced back to (a) the exact chunk used for retrieval and (b) the original raw document/URL — this is what prevents hallucinated citations.
- `reports.themes` and `verified_claims` are stored as JSONB rather than fully normalized tables — fine for v1 since they're read as a whole object by the frontend and not individually queried/filtered at the DB level. Normalize later only if you need to query "all reports where theme=X" across users.
- `document_chunks.query_id` is intentionally denormalized (duplicated from `source_documents`) purely so retrieval queries don't need a join — worth a one-line comment in code since an AI assistant reading this cold might try to "clean it up" into a join.

---

## 5. Environment Variables (both services)

```
# Shared
DATABASE_URL=postgresql://user:pass@localhost:5432/research_db
REDIS_URL=redis://localhost:6379

# Node
JWT_SECRET=
PYTHON_SERVICE_URL=http://localhost:8000

# Python
OPENAI_API_KEY=
REDDIT_CLIENT_ID=
REDDIT_CLIENT_SECRET=
YOUTUBE_API_KEY=
NEWS_API_KEY=
```

---

