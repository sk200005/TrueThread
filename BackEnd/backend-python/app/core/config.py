"""
core/config.py — Application settings loaded from environment variables.

Uses pydantic-settings to validate and type-check all required config
at startup, so missing env vars fail fast rather than at first use.
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All environment variables consumed by backend-python."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",  # ignore env vars not listed here
    )

    # ── Database ──────────────────────────────────────────────────────────
    database_url: str = "postgresql+asyncpg://swayam:PGSQLpw#1@localhost:5432/postgres"

    # ── YouTube API ───────────────────────────────────────────────────────
    youtube_api_key: str = ""

    # ── Groq LLM (OpenAI-compatible) ──────────────────────────────────────
    groq_api_key: str = ""

    # ── Server ────────────────────────────────────────────────────────────
    python_service_port: int = 8000

    # ── Embedding (Local) ─────────────────────────────────────────────────
    # sentence-transformers model used for all embedding (384 dims).
    embedding_model: str = "all-MiniLM-L6-v2"
    embedding_dimension: int = 384

    # ── Groq LLM Model ───────────────────────────────────────────────────
    # Model used for summarization and claim extraction (via OpenAI-compatible API).
    groq_model: str = "llama-3.3-70b-versatile"

    # ── Redis (BullMQ Job Queue) ─────────────────────────────────────────
    redis_host: str = "localhost"
    redis_port: int = 6379

    # ── NewsAPI (Phase E — cross-verification) ────────────────────────────
    # Free tier: 100 requests/day, ~1 month lookback.
    # Get a key at https://newsapi.org/register
    news_api_key: str = ""


settings = Settings()
