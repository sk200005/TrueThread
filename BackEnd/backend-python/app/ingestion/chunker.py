"""
app/ingestion/chunker.py — Token-aware text chunking.

Splits text into chunks using tiktoken and LangChain's RecursiveCharacterTextSplitter.
Does NOT make any API calls or DB connections.
"""

from dataclasses import dataclass     # cInstead of writing the constructor yourself, Dataclass in python generates it automatically.
from typing import List               #Type Hinting. Helps in understanding the code.
import tiktoken                       #It converts text into tokens. "I love AI" -> ["I", " love", " AI"] not count the characters 
from langchain_text_splitters import RecursiveCharacterTextSplitter

# Target model tokenization. text-embedding-3-small uses cl100k_base.
ENCODING_NAME = "cl100k_base"                #tokenizer to use.
 
# Settings for chunking
CHUNK_SIZE_TOKENS = 600
CHUNK_OVERLAP_TOKENS = 80


@dataclass
class ChunkResult:
    chunk_text: str          #Text: "Python is..."
    chunk_index: int         #Index: 1, 2, 3...
    token_count: int         #Tokens: 587, 598, 567... (Always less than 600) and more than 80


def _get_encoder() -> tiktoken.Encoding:                                #This function returns an object of type tiktoken.Encoding 
    """Returns the tiktoken encoder for the target embedding model."""
    return tiktoken.get_encoding(ENCODING_NAME)


def _get_splitter() -> RecursiveCharacterTextSplitter:
    """Returns a pre-configured LangChain text splitter using tiktoken."""
    return RecursiveCharacterTextSplitter.from_tiktoken_encoder(
        encoding_name=ENCODING_NAME,
        chunk_size=CHUNK_SIZE_TOKENS,
        chunk_overlap=CHUNK_OVERLAP_TOKENS,
        separators=["\n\n", "\n", ". ", " ", ""],
    )


def chunk_document(text: str) -> List[ChunkResult]:
    """
    Splits a document text into smaller chunks for embedding.
    
    Args:
        text: The raw text of the document.
        
    Returns:
        List of ChunkResult objects containing the chunk text, index, and token count.
    """
    if not text or not text.strip():
        return []

    splitter = _get_splitter()
    encoder = _get_encoder()
    
    # Langchain splitter automatically handles splitting based on the separators
    raw_chunks = splitter.split_text(text)
    
    results = []
    for i, chunk in enumerate(raw_chunks):
        # Ignore empty chunks that might result from weird formatting
        if not chunk.strip():
            continue
            
        token_count = len(encoder.encode(chunk, disallowed_special=()))
        results.append(ChunkResult(
            chunk_text=chunk,
            chunk_index=i,
            token_count=token_count
        ))
        
    return results
