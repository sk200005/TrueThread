"""
core/database.py — Async SQLAlchemy engine and session factory.

Provides:
  - `engine`       : AsyncEngine bound to DATABASE_URL
  - `async_session` : sessionmaker that yields AsyncSession instances
  - `get_db()`     : FastAPI dependency that yields a session per request
  - `Base`         : declarative base for all ORM models
"""

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.core.config import settings

engine = create_async_engine(
    settings.database_url,
    echo=False,
    pool_size=5,
    max_overflow=10,
)

async_session = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)



class Base(DeclarativeBase):
    """Declarative base for all SQLAlchemy models."""
    pass


async def get_db():
    """FastAPI dependency — yields an async session, auto-closes on exit."""
    async with async_session() as session:
        yield session
