"""
models/query.py — SQLAlchemy ORM model for queries.
Minimal stub to satisfy Foreign Keys from source_documents and document_chunks.
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, String, Text
from sqlalchemy.dialects.postgresql import ARRAY, UUID

from app.core.database import Base


class Query(Base):
    __tablename__ = "queries"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # user_id = Column(UUID(as_uuid=True), ForeignKey("users.id")) # We don't strictly need users right now
    query_text = Column(Text, nullable=False)
    status = Column(String(20), default="pending")
    sources_requested = Column(ARRAY(Text))
    sources_failed = Column(ARRAY(Text))
    error_message = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    completed_at = Column(DateTime(timezone=True), nullable=True)
