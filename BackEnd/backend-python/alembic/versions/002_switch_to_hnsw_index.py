"""Switch document_chunks vector index from IVFFlat to HNSW

Revision ID: 002_switch_to_hnsw_index
Revises: 001_add_chunk_metadata
Create Date: 2026-10-25 12:25:00.000000

"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '002_switch_to_hnsw_index'
down_revision: Union[str, None] = '001_add_chunk_metadata'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Safely drop legacy ivfflat index if it exists
    op.execute("DROP INDEX IF EXISTS idx_document_chunks_embedding")
    # Create HNSW index with cosine distance
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_document_chunks_embedding_hnsw
        ON document_chunks
        USING hnsw (embedding vector_cosine_ops)
        WITH (m = 16, ef_construction = 64)
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_document_chunks_embedding_hnsw")
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_document_chunks_embedding
        ON document_chunks
        USING ivfflat (embedding vector_cosine_ops)
        WITH (lists = 100)
    """)
