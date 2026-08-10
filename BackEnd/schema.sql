-- ==========================================
-- Re-Search Application — Database Schema
-- PostgreSQL + pgvector
-- ==========================================

-- Enable required extensions
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- ==========================================
-- Users
-- ==========================================
CREATE TABLE IF NOT EXISTS users (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    email VARCHAR(255) UNIQUE NOT NULL,
    password_hash VARCHAR(255),          -- null if OAuth-only
    name VARCHAR(255),
    created_at TIMESTAMPTZ DEFAULT now()
);

-- ==========================================
-- Queries
-- One row per research request submitted by a user
-- ==========================================
CREATE TABLE IF NOT EXISTS queries (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id UUID REFERENCES users(id) ON DELETE CASCADE,
    query_text TEXT NOT NULL,
    status VARCHAR(20) DEFAULT 'pending', -- pending | running | done | error
    sources_requested TEXT[],             -- e.g. {reddit,youtube,wikipedia,news}
    sources_failed TEXT[],
    created_at TIMESTAMPTZ DEFAULT now(),
    completed_at TIMESTAMPTZ
);

-- ==========================================
-- Source Documents
-- Raw fetched documents, before/after chunking — kept for traceability.
-- Every claim/quote in a report must be traceable back to a row here.
-- ==========================================
CREATE TABLE IF NOT EXISTS source_documents (
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

-- ==========================================
-- Document Chunks
-- Chunked + embedded text for RAG retrieval.
-- query_id is intentionally denormalized from source_documents
-- so retrieval queries don't need a join.
-- ==========================================
CREATE TABLE IF NOT EXISTS document_chunks (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    source_document_id UUID REFERENCES source_documents(id) ON DELETE CASCADE,
    query_id UUID REFERENCES queries(id) ON DELETE CASCADE,  -- denormalized for faster scoped search
    chunk_text TEXT NOT NULL,
    embedding VECTOR(384),                -- dimension matches embedding model (all-MiniLM-L6-v2)
    created_at TIMESTAMPTZ DEFAULT now()
);

-- ==========================================
-- Reports
-- Final generated report per query (1:1 with queries once complete).
-- themes and verified_claims stored as JSONB — read as whole objects,
-- not filtered at DB level in v1.
-- ==========================================
CREATE TABLE IF NOT EXISTS reports (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    query_id UUID REFERENCES queries(id) ON DELETE CASCADE UNIQUE,
    sentiment_summary JSONB,              -- {"positive": 62, "negative": 28, "neutral": 10}
    themes JSONB,                         -- array of ThemeSummary objects
    verified_claims JSONB,                -- array of ClaimVerification objects
    created_at TIMESTAMPTZ DEFAULT now()
);

-- ==========================================
-- Indexes
-- ==========================================
CREATE INDEX IF NOT EXISTS idx_queries_user_id ON queries(user_id);
CREATE INDEX IF NOT EXISTS idx_source_documents_query_id ON source_documents(query_id);
CREATE INDEX IF NOT EXISTS idx_document_chunks_query_id ON document_chunks(query_id);

-- Vector similarity index (IVFFlat — good enough at small/medium scale)
-- NOTE: This index requires at least 100 rows in the table before it is
-- useful. For small datasets, a sequential scan may be faster. The lists=100
-- value should be set to roughly sqrt(row_count) in production.
CREATE INDEX IF NOT EXISTS idx_document_chunks_embedding ON document_chunks
    USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);

-- ==========================================
-- Job Checkpoints
-- Persists the last successfully processed item ID per (job, source) pair.
-- Enables a retried or resumed scrape job to continue from where it left off
-- instead of restarting from scratch.
--
-- last_id stores the platform-native ID of the last successfully saved item:
--   reddit  → post_id of the last post fully saved
--   youtube → videoId of the last video fully saved
-- ==========================================
CREATE TABLE IF NOT EXISTS job_checkpoints (
    job_id     UUID NOT NULL,
    source     VARCHAR(50) NOT NULL,
    last_id    TEXT,
    updated_at TIMESTAMPTZ DEFAULT now(),
    PRIMARY KEY (job_id, source)
);

-- ==========================================
-- Extracted Claims
-- LLM-extracted claims from source comments/chunks.
-- Written at ingestion time by claim extraction pipeline.
-- source_comment_id references the original comment ID from
-- the scraping platform (e.g. Reddit's "t1_ipxtyld").
-- ==========================================
CREATE TABLE IF NOT EXISTS extracted_claims (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    claim_text TEXT NOT NULL,
    entities JSONB,                        -- array of entity strings, e.g. ["iPhone", "Apple"]
    claim_type VARCHAR(50),               -- comparison | effectiveness | warning | opinion | factual
    direction VARCHAR(20),                -- positive | negative | neutral (nullable)
    confidence VARCHAR(10),               -- high | medium | low
    is_sincere BOOLEAN DEFAULT true,
    source_comment_id TEXT,               -- platform-native ID (e.g. "t1_ipxtyld") or chunk UUID
    source_platform VARCHAR(50),          -- reddit | youtube | wikipedia | ...
    raw_llm_response JSONB,              -- full LLM response for debugging
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_extracted_claims_source_comment_id
    ON extracted_claims(source_comment_id);

-- ==========================================
-- Example RAG Retrieval Query (reference)
-- ==========================================
-- SELECT chunk_text, source_document_id
-- FROM document_chunks
-- WHERE query_id = $1
-- ORDER BY embedding <=> $2   -- $2 = query embedding vector
-- LIMIT 8;
