"""Application configuration from environment variables."""

import os
from datetime import date, datetime
from functools import lru_cache
from pathlib import Path
from typing import Optional

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── Data Paths ──────────────────────────────────────
    corpus_dir: str = "./data/corpus"
    manifest_path: str = "./data/manifest.json"
    db_path: str = "./data/veltrix.db"

    # ── Application ─────────────────────────────────────
    app_today: Optional[str] = None  # YYYY-MM-DD, defaults to system date
    default_year: int = 2026  # "A date without a year means 2026" (Section 8.4)
    vector_store_path: Optional[str] = None  # defaults to <DB_PATH dir>/vector_store

    # ── LLM Provider ────────────────────────────────────
    llm_provider: str = "openai"  # openai | anthropic | google
    openai_api_key: Optional[str] = None
    anthropic_api_key: Optional[str] = None
    google_api_key: Optional[str] = None
    # OpenAI-compatible endpoint for the "openai" provider, e.g. local Ollama
    # (http://localhost:11434/v1), Groq or OpenRouter. Empty = api.openai.com.
    openai_base_url: Optional[str] = None
    # Optional JSON object merged into OpenAI-compatible requests, e.g. for
    # OpenRouter reasoning models: {"reasoning": {"enabled": false}}
    openai_extra_body: Optional[str] = None

    # ── Model Configuration ─────────────────────────────
    llm_model: str = "gpt-4o-mini"
    llm_temperature: float = 0.0
    llm_timeout_seconds: float = 10.0  # per LLM call; each request must finish < 30s
    llm_max_retries: int = 1
    # Hard wall-clock limit for one LLM call including retries. The HTTP
    # timeout above is per read, so a provider that streams keep-alive bytes
    # could otherwise hold a call past the request deadline.
    llm_deadline_seconds: float = 18.0
    llm_seed: int = 7  # used where the provider supports it (OpenAI)

    # ── Embedding ───────────────────────────────────────
    embedding_model: str = "all-MiniLM-L6-v2"

    # ── Retrieval ───────────────────────────────────────
    max_corpus_tokens: int = 3000
    retrieval_top_k: int = 20
    retrieval_final_k: int = 10
    retrieval_min_score: float = 0.25  # cosine similarity below this = not relevant
    extractive_min_score: float = 0.43  # stricter bar for answers composed without an LLM
    max_chunks_per_doc: int = 3
    upcoming_token_reserve: int = 600  # share of the 3,000 budget kept for upcoming changes

    # ── API ─────────────────────────────────────────────
    request_timeout_seconds: float = 27.0  # hard deadline per /chat request (< 30s)
    max_message_chars: int = 4000
    max_session_id_chars: int = 200
    cors_origins: str = ""  # comma-separated allow-list; empty = CORS disabled

    # ── Logging ─────────────────────────────────────────
    log_level: str = "INFO"

    @field_validator("app_today")
    @classmethod
    def _validate_app_today(cls, value: Optional[str]) -> Optional[str]:
        if value is None or not value.strip():
            return None
        date.fromisoformat(value.strip())  # raises ValueError on bad input
        return value.strip()

    @field_validator("llm_provider")
    @classmethod
    def _normalise_provider(cls, value: str) -> str:
        value = (value or "openai").strip().lower()
        aliases = {"gemini": "google", "claude": "anthropic"}
        return aliases.get(value, value)

    @property
    def llm_configured(self) -> bool:
        """True when an API key exists for the selected provider."""
        key_map = {
            "openai": self.openai_api_key,
            "anthropic": self.anthropic_api_key,
            "google": self.google_api_key,
        }
        key = key_map.get(self.llm_provider)
        # Placeholders such as "your-key" or "sk-..." are treated as not configured.
        return bool(key and key.strip() and not key.startswith("your-") and "..." not in key)

    @property
    def today(self) -> date:
        """Get today's date from APP_TODAY or system date."""
        if self.app_today:
            return date.fromisoformat(self.app_today)
        return date.today()

    @property
    def corpus_path(self) -> Path:
        return Path(self.corpus_dir)

    @property
    def manifest_file(self) -> Path:
        return Path(self.manifest_path)

    @property
    def database_path(self) -> Path:
        return Path(self.db_path)

    @property
    def vector_store_dir(self) -> Path:
        """Directory to store FAISS index files."""
        if self.vector_store_path:
            return Path(self.vector_store_path)
        return Path(self.db_path).parent / "vector_store"

    def get_api_key(self) -> str:
        """Get the API key for the configured LLM provider."""
        key_map = {
            "openai": self.openai_api_key,
            "anthropic": self.anthropic_api_key,
            "google": self.google_api_key,
        }
        key = key_map.get(self.llm_provider)
        if not key:
            raise ValueError(
                f"API key not set for provider '{self.llm_provider}'. "
                f"Set the corresponding environment variable."
            )
        return key

    def get_llm_model(self) -> str:
        """Get the model name for the configured provider."""
        # Default models per provider if the default is still set
        if self.llm_model == "gpt-4o-mini" and self.llm_provider != "openai":
            defaults = {
                "anthropic": "claude-3-5-haiku-latest",
                "google": "gemini-2.0-flash",
            }
            return defaults.get(self.llm_provider, self.llm_model)
        return self.llm_model


@lru_cache()
def get_settings() -> Settings:
    """Get cached settings instance."""
    return Settings()
