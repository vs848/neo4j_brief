"""Centralised runtime settings loaded from environment / .env file."""
from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Neo4j
    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = "12345678"
    neo4j_database: str = "neo4j"

    # HTTP
    http_timeout: float = 20.0
    http_user_agent: str = "brandgraph/0.1 (+https://example.local)"
    request_delay_seconds: float = 1.0

    # Discovery
    max_competitors: int = Field(default=8, ge=1, le=50)
    max_pages_per_competitor: int = Field(default=5, ge=1, le=25)

    # Text processing
    chunk_size: int = Field(default=800, ge=200, le=4000)
    chunk_overlap: int = Field(default=120, ge=0, le=1000)
    keywords_per_competitor: int = Field(default=25, ge=5, le=200)


settings = Settings()
