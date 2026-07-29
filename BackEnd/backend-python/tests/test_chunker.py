"""Tests for the token-aware chunker."""

import pytest
from app.ingestion.chunker import chunk_document, CHUNK_SIZE_TOKENS, CHUNK_OVERLAP_TOKENS

def test_empty_text():
    assert chunk_document("") == []
    assert chunk_document("   \n  ") == []

def test_short_text():
    # Text shorter than one chunk
    text = "This is a very short text that easily fits in one chunk."
    chunks = chunk_document(text)
    
    assert len(chunks) == 1
    assert chunks[0].chunk_text == text
    assert chunks[0].chunk_index == 0
    assert chunks[0].token_count < 20

def test_long_text_overlap():
    # Create a long string with multiple sentences to force splitting
    # We repeat a sentence enough times to exceed 600 tokens.
    # "This is a sentence that will be repeated many times to test chunking and overlapping logic. "
    # has ~17 tokens. 17 * 50 = 850 tokens.
    sentence = "This is a sentence that will be repeated many times to test chunking and overlapping logic. "
    text = sentence * 50
    
    chunks = chunk_document(text)
    
    assert len(chunks) >= 2
    
    # First chunk should be around the target chunk size
    assert chunks[0].token_count <= CHUNK_SIZE_TOKENS
    
    # Ensure sequential index
    assert chunks[0].chunk_index == 0
    assert chunks[1].chunk_index == 1
    
    # Check for overlap: the end of chunk 0 should share some sentences with the beginning of chunk 1
    chunk_0_sentences = chunks[0].chunk_text.strip().split(". ")
    chunk_1_sentences = chunks[1].chunk_text.strip().split(". ")
    
    # Due to chunk_overlap=80 tokens, there should be several overlapping sentences
    last_sentences = chunk_0_sentences[-3:]
    first_sentences = chunk_1_sentences[:3]
    
    # At least some of the trailing sentences of chunk 0 should be in the leading sentences of chunk 1
    overlap_found = any(s in first_sentences for s in last_sentences)
    assert overlap_found, "Expected overlapping text between adjacent chunks"

def test_no_natural_split_points():
    # A massive block of text with no spaces or punctuation
    # A token is usually ~4 chars, so 4000 chars is ~1000 tokens.
    giant_word = "a" * 4000
    
    chunks = chunk_document(giant_word)
    
    assert len(chunks) >= 2
    for chunk in chunks:
        assert chunk.token_count <= CHUNK_SIZE_TOKENS
        assert set(chunk.chunk_text) == {"a"}  # Only contains 'a's
