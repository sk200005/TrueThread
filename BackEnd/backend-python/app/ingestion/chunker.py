"""
app/ingestion/chunker.py — Text chunking for the ingestion pipeline.

Splits text into chunks using LangChain's RecursiveCharacterTextSplitter.
Chunk sizes are calibrated for all-MiniLM-L6-v2 which has a 256-token
context window — we aim for ~200 word-pieces per chunk with some overlap.

Does NOT make any API calls or DB connections.
"""

from dataclasses import dataclass
from typing import List

from langchain_text_splitters import RecursiveCharacterTextSplitter

# ── Settings for chunking ────────────────────────────────────────────────
# all-MiniLM-L6-v2 has a 256 word-piece token limit.
# ~4 chars ≈ 1 token on average, so 800 chars ≈ 200 tokens.
CHUNK_SIZE_CHARS = 800
CHUNK_OVERLAP_CHARS = 150


@dataclass
class ChunkResult:
    chunk_text: str          # Text: "Python is..."
    chunk_index: int         # Index: 0, 1, 2...
    token_count: int         # Approximate token count (chars // 4)


def _get_splitter() -> RecursiveCharacterTextSplitter:
    """Returns a pre-configured LangChain text splitter."""
    return RecursiveCharacterTextSplitter(
        chunk_size=CHUNK_SIZE_CHARS,
        chunk_overlap=CHUNK_OVERLAP_CHARS,
        length_function=len,
        separators=["\n\n", "\n", ". ", " ", ""],
    )


def chunk_document(text: str) -> List[ChunkResult]:
    """
    Splits a document text into smaller chunks for embedding.

    Args:
        text: The raw text of the document.

    Returns:
        List of ChunkResult objects containing the chunk text, index,
        and approximate token count.
    """
    if not text or not text.strip():
        return []

    splitter = _get_splitter()
    raw_chunks = splitter.split_text(text)

    results = []
    for i, chunk in enumerate(raw_chunks):
        # Ignore empty chunks that might result from weird formatting
        if not chunk.strip():
            continue

        # Approximate token count (~4 chars per token)
        approx_tokens = len(chunk) // 4
        results.append(ChunkResult(
            chunk_text=chunk,
            chunk_index=i,
            token_count=approx_tokens,
        ))

    return results
