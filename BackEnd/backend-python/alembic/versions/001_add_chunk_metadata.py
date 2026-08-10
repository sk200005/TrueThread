"""Add chunk_index and metadata to document_chunks

Revision ID: 001_add_chunk_metadata
Revises: 
Create Date: 2026-07-23 18:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '001_add_chunk_metadata'
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add chunk_index and metadata to document_chunks
    op.add_column('document_chunks', sa.Column('chunk_index', sa.Integer(), nullable=True))
    op.add_column('document_chunks', sa.Column('metadata', postgresql.JSONB(astext_type=sa.Text()), nullable=True))

    # Ensure embedding column is vector(384) to match all-MiniLM-L6-v2.
    op.execute("ALTER TABLE document_chunks ALTER COLUMN embedding TYPE vector(384)")


def downgrade() -> None:
    op.drop_column('document_chunks', 'metadata')
    op.drop_column('document_chunks', 'chunk_index')
