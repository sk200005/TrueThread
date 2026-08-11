"""
test_config.py — Shared configuration for all test pipelines.

Provides:
    - Database connection (async SQLAlchemy) pointing to research_test
    - LLM client (Groq via OpenAI SDK)
    - Embedder (all-MiniLM-L6-v2, 384 dims)
    - Text chunker (LangChain RecursiveCharacterTextSplitter)
    - Styled logging helpers for stage-by-stage output
    - Pre-filter for claim extraction
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, List, Optional

from dotenv import load_dotenv
from openai import AsyncOpenAI
from pydantic import BaseModel, Field, field_validator
from sentence_transformers import SentenceTransformer
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)
from langchain_text_splitters import RecursiveCharacterTextSplitter

# ── Load .env from BackEnd ────────────────────────────────────────────────
_env_path = os.path.join(os.path.dirname(__file__), "..", "BackEnd", "backend-python", ".env")
if os.path.exists(_env_path):
    load_dotenv(_env_path)
else:
    # Fallback to BackEnd/.env
    _env_path2 = os.path.join(os.path.dirname(__file__), "..", "BackEnd", ".env")
    if os.path.exists(_env_path2):
        load_dotenv(_env_path2)


# ══════════════════════════════════════════════════════════════════════════
# Database Configuration
# ══════════════════════════════════════════════════════════════════════════

TEST_DATABASE_URL = "postgresql+asyncpg://swayam:PGSQLpw%231@localhost:5433/research_test"

engine = create_async_engine(TEST_DATABASE_URL, echo=False, pool_size=5, max_overflow=10)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


# ══════════════════════════════════════════════════════════════════════════
# LLM Client (Groq via OpenAI-compatible API)
# ══════════════════════════════════════════════════════════════════════════

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_MODEL = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")
GROQ_BASE_URL = "https://api.groq.com/openai/v1"

_llm_client: AsyncOpenAI | None = None


def get_llm_client() -> AsyncOpenAI:
    """Return a shared AsyncOpenAI client pointed at Groq."""
    global _llm_client
    if _llm_client is None:
        if not GROQ_API_KEY:
            print("  ⚠  WARNING: GROQ_API_KEY not set. LLM calls will fail.")
        _llm_client = AsyncOpenAI(api_key=GROQ_API_KEY, base_url=GROQ_BASE_URL)
    return _llm_client


async def llm_chat(
    system_prompt: str,
    user_prompt: str,
    temperature: float = 0.1,
    max_tokens: int = 1024,
) -> str:
    """Send a chat completion to Groq and return the text response."""
    client = get_llm_client()
    completion = await client.chat.completions.create(
        model=GROQ_MODEL,
        temperature=temperature,
        max_tokens=max_tokens,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    )
    return (completion.choices[0].message.content or "").strip()


# ══════════════════════════════════════════════════════════════════════════
# Embedder (all-MiniLM-L6-v2, 384 dims)
# ══════════════════════════════════════════════════════════════════════════

EMBEDDING_MODEL_NAME = "all-MiniLM-L6-v2"
EMBEDDING_DIMENSION = 384

_embedding_model: SentenceTransformer | None = None


def get_embedding_model() -> SentenceTransformer:
    """Lazy-load the embedding model."""
    global _embedding_model
    if _embedding_model is None:
        print(f"  → Loading embedding model: {EMBEDDING_MODEL_NAME}...")
        _embedding_model = SentenceTransformer(EMBEDDING_MODEL_NAME)
        print(f"  ✅ Model loaded ({EMBEDDING_DIMENSION} dimensions)")
    return _embedding_model


async def embed_texts(texts: List[str]) -> List[List[float]]:
    """Generate embeddings for a batch of texts. Runs in a thread to avoid blocking."""
    if not texts:
        return []
    model = get_embedding_model()
    embeddings = await asyncio.to_thread(
        model.encode, texts, show_progress_bar=False
    )
    return [emb.tolist() for emb in embeddings]


# ══════════════════════════════════════════════════════════════════════════
# Text Chunker
# ══════════════════════════════════════════════════════════════════════════

CHUNK_SIZE_CHARS = 800
CHUNK_OVERLAP_CHARS = 150

_splitter = RecursiveCharacterTextSplitter(
    chunk_size=CHUNK_SIZE_CHARS,
    chunk_overlap=CHUNK_OVERLAP_CHARS,
    length_function=len,
    separators=["\n\n", "\n", ". ", " ", ""],
)


@dataclass
class ChunkResult:
    chunk_text: str
    chunk_index: int
    token_count: int  # Approximate (chars // 4)


def chunk_text(text_content: str) -> List[ChunkResult]:
    """Split text into chunks for embedding."""
    if not text_content or not text_content.strip():
        return []

    raw_chunks = _splitter.split_text(text_content)
    results = []
    for i, chunk in enumerate(raw_chunks):
        if not chunk.strip():
            continue
        results.append(ChunkResult(
            chunk_text=chunk,
            chunk_index=i,
            token_count=len(chunk) // 4,
        ))
    return results


# ══════════════════════════════════════════════════════════════════════════
# Claim Extraction — Pre-filter + Pydantic model + LLM call
# ══════════════════════════════════════════════════════════════════════════

MIN_WORD_COUNT = 5
CLAIM_BATCH_SIZE = 5

# Pre-filter (ported from extract_claims.py)
def pre_filter(text_content: str) -> tuple[bool, str]:
    """Cost gate — skip text unlikely to contain claims."""
    text_content = (text_content or "").strip()
    if not text_content:
        return False, "empty text"

    words = [w for w in text_content.split() if w]
    if len(words) < MIN_WORD_COUNT:
        return False, f"too short ({len(words)} words)"

    sentences = [s.strip() for s in re.split(r'[.!?\n]+', text_content) if s.strip()]
    question_words_pattern = re.compile(
        r'^(what|who|where|when|why|how|is|are|was|were|do|does|did|can|could|should|would|will)\b',
        re.IGNORECASE,
    )
    if sentences:
        is_pure_question = all(
            s.endswith('?') or question_words_pattern.match(s)
            for s in sentences
        )
        if is_pure_question:
            return False, "pure question"

    return True, "ok"


class ExtractedClaim(BaseModel):
    """Validates a single claim from the LLM."""
    claim_text: str
    entities: list[str] = Field(default_factory=list)
    claim_type: str = "opinion"
    confidence: str = "medium"
    source_comment_id: str

    @field_validator("claim_type")
    @classmethod
    def validate_claim_type(cls, v: str) -> str:
        allowed = {"comparison", "effectiveness", "warning", "opinion", "factual"}
        return v if v in allowed else "opinion"

    @field_validator("confidence")
    @classmethod
    def validate_confidence(cls, v: str) -> str:
        allowed = {"high", "medium", "low"}
        return v if v in allowed else "medium"


EXTRACT_SYSTEM_PROMPT = (
    'Extract claims from the text chunks. Return a JSON array only — no prose, no fences.\n'
    'A claim = declarative assertion about an entity (effectiveness, warning, opinion, factual, comparison).\n'
    'Skip: questions, greetings, filler, meta-commentary.\n'
    'Schema per claim: {"claim_text":string,"entities":string[],'
    '"claim_type":"comparison"|"effectiveness"|"warning"|"opinion"|"factual",'
    '"confidence":"high"|"medium"|"low","source_comment_id":string}\n'
    'confidence: high=no hedge, medium="I think"/"seems", low="might"/"could".\n'
    'If no claims, return [].'
)


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=2, min=2, max=30),
    retry=retry_if_exception_type(Exception),
    reraise=True,
)
async def call_llm_for_claims(user_message: str) -> str:
    """Call the LLM for claim extraction with retry."""
    return await llm_chat(
        system_prompt=EXTRACT_SYSTEM_PROMPT,
        user_prompt=user_message,
        temperature=0.1,
        max_tokens=1024,
    )


def build_claim_user_message(batch: list[dict[str, str]]) -> str:
    """Build the multi-chunk user message for claim extraction."""
    entries = []
    for item in batch:
        entry = f"[ID:{item['id']}]\n{item['text']}"
        entries.append(entry)
    return "\n---\n".join(entries)


def parse_claims_response(raw_response: str) -> tuple[list[ExtractedClaim], str | None]:
    """Parse LLM claim extraction response."""
    if not raw_response or not raw_response.strip():
        return [], "empty response"

    cleaned = raw_response.strip()
    cleaned = re.sub(r'^```(?:json)?\s*', '', cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r'\s*```$', '', cleaned, flags=re.IGNORECASE)
    cleaned = cleaned.strip()

    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError as e:
        return [], str(e)

    if not isinstance(parsed, list):
        return [], f"expected array, got {type(parsed).__name__}"

    claims: list[ExtractedClaim] = []
    for i, raw_claim in enumerate(parsed):
        try:
            claim = ExtractedClaim.model_validate(raw_claim)
            claims.append(claim)
        except Exception:
            continue

    return claims, None


# ══════════════════════════════════════════════════════════════════════════
# Summarization — System prompt + LLM call
# ══════════════════════════════════════════════════════════════════════════

SUMMARIZE_SYSTEM_PROMPT = """You are a research analyst. Analyze the following text chunks retrieved from various sources and produce a structured JSON report.

Return STRICT JSON only — no markdown fences, no prose before or after the JSON.

Required JSON schema:
{
  "overall_sentiment": "positive" | "negative" | "mixed" | "neutral",
  "themes": [
    {
      "theme": "string — a short theme label",
      "supporting_chunk_ids": ["chunk_id_1", "chunk_id_2"]
    }
  ],
  "summary": "string — 2-3 sentence overview of findings"
}

Rules:
- overall_sentiment must be exactly one of: positive, negative, mixed, neutral
- Each theme should be a distinct topic or pattern found across the chunks
- supporting_chunk_ids must reference actual chunk IDs from the input
- summary should be concise and factual, synthesizing the key findings
- If claims are provided, incorporate them into the themes and summary
- If the text is insufficient for analysis, return neutral sentiment with a single theme"""


def build_summary_user_prompt(
    query: str,
    chunks: list[dict[str, Any]],
    claims: list[dict[str, Any]] | None = None,
) -> str:
    """Build user prompt for summarization."""
    parts = [f'User\'s research query: "{query}"\n']

    parts.append("=== RETRIEVED TEXT CHUNKS ===\n")
    for i, chunk in enumerate(chunks):
        chunk_id = chunk.get("chunk_id", f"chunk_{i}")
        similarity = chunk.get("similarity", 0.0)
        text_content = chunk.get("chunk_text", "")
        parts.append(f"[Chunk ID: {chunk_id}] (relevance: {similarity:.3f})")
        parts.append(text_content)
        parts.append("")

    if claims:
        parts.append("\n=== EXTRACTED CLAIMS ===\n")
        for claim in claims:
            parts.append(
                f"- [{claim.get('claim_type', 'unknown')}] "
                f"{claim.get('claim_text', '')} "
                f"(confidence: {claim.get('confidence', 'unknown')}, "
                f"entities: {claim.get('entities', [])})"
            )

    return "\n".join(parts)


def parse_summary_response(raw_response: str) -> dict[str, Any] | None:
    """Parse the LLM summarization response."""
    if not raw_response or not raw_response.strip():
        return None

    cleaned = raw_response.strip()
    cleaned = re.sub(r'^```(?:json)?\s*', '', cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r'\s*```$', '', cleaned, flags=re.IGNORECASE)

    try:
        parsed = json.loads(cleaned.strip())
    except json.JSONDecodeError:
        return None

    return parsed if isinstance(parsed, dict) else None


# ══════════════════════════════════════════════════════════════════════════
# Styled Output Helpers
# ══════════════════════════════════════════════════════════════════════════

class Colors:
    HEADER = '\033[95m'
    BLUE = '\033[94m'
    CYAN = '\033[96m'
    GREEN = '\033[92m'
    YELLOW = '\033[93m'
    RED = '\033[91m'
    BOLD = '\033[1m'
    DIM = '\033[2m'
    RESET = '\033[0m'


def print_stage_header(stage_num: int, title: str):
    """Print a styled stage header."""
    print()
    print(f"{Colors.BOLD}{Colors.CYAN}╔══════════════════════════════════════════════════╗{Colors.RESET}")
    print(f"{Colors.BOLD}{Colors.CYAN}║  STAGE {stage_num}: {title:<41}║{Colors.RESET}")
    print(f"{Colors.BOLD}{Colors.CYAN}╚══════════════════════════════════════════════════╝{Colors.RESET}")
    print()


def print_result(label: str, value: Any):
    """Print a labeled result."""
    print(f"  {Colors.GREEN}✓{Colors.RESET} {Colors.BOLD}{label}:{Colors.RESET} {value}")


def print_error(message: str):
    """Print an error message."""
    print(f"  {Colors.RED}✗ ERROR:{Colors.RESET} {message}")


def print_info(message: str):
    """Print an info message."""
    print(f"  {Colors.BLUE}ℹ{Colors.RESET} {message}")


def print_warning(message: str):
    """Print a warning message."""
    print(f"  {Colors.YELLOW}⚠{Colors.RESET} {message}")


def print_divider():
    """Print a thin divider."""
    print(f"  {Colors.DIM}{'─' * 48}{Colors.RESET}")


def wait_for_next_stage():
    """Pause execution and wait for user to press Enter."""
    print()
    input(f"  {Colors.DIM}Press Enter to continue to next stage...{Colors.RESET}")


def print_pipeline_header(platform: str, query: str):
    """Print the pipeline header."""
    print()
    print(f"{Colors.BOLD}{Colors.HEADER}╔══════════════════════════════════════════════════╗{Colors.RESET}")
    print(f"{Colors.BOLD}{Colors.HEADER}║  {platform.upper()} PIPELINE TEST{' ' * (35 - len(platform))}║{Colors.RESET}")
    print(f"{Colors.BOLD}{Colors.HEADER}╚══════════════════════════════════════════════════╝{Colors.RESET}")
    print(f"  Query: \"{query}\"")
    print(f"  Database: research_test @ localhost:5433")
    print(f"  Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print()


def print_pipeline_footer(platform: str, stats: dict[str, Any]):
    """Print the pipeline completion footer."""
    print()
    print(f"{Colors.BOLD}{Colors.GREEN}╔══════════════════════════════════════════════════╗{Colors.RESET}")
    print(f"{Colors.BOLD}{Colors.GREEN}║  {platform.upper()} PIPELINE COMPLETE{' ' * (31 - len(platform))}║{Colors.RESET}")
    print(f"{Colors.BOLD}{Colors.GREEN}╚══════════════════════════════════════════════════╝{Colors.RESET}")
    for key, value in stats.items():
        print(f"  {Colors.GREEN}✓{Colors.RESET} {key}: {value}")
    print()
