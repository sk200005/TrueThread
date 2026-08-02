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

    # ── Groq LLM (OpenAI-compatible) ──────────────────────────────────────
    groq_api_key: str = ""

    # ── Server ────────────────────────────────────────────────────────────
    python_service_port: int = 8000

    # ── Embedding (Legacy Local) ──────────────────────────────────────────
    # Model used by sentence-transformers (384 dims).
    embedding_model: str = "all-MiniLM-L6-v2"
    embedding_dimension: int = 384

    # ── Embedding (OpenAI) ────────────────────────────────────────────────
    # Model used by the new RAG ingestion pipeline (1536 dims).
    openai_api_key: str = ""
    openai_embedding_model: str = "text-embedding-3-small"

    # ── Groq LLM Model ───────────────────────────────────────────────────
    # Model used for summarization and claim extraction (via OpenAI-compatible API).
    groq_model: str = "llama-3.3-70b-versatile"


settings = Settings()
