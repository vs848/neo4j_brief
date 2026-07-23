"""Dataclass-like Pydantic models used to move data between pipeline stages."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from pydantic import BaseModel, Field, HttpUrl


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Brand(BaseModel):
    name: str
    slug: str
    seed_domain: Optional[str] = None
    created_at: datetime = Field(default_factory=_utcnow)


class SearchHit(BaseModel):
    title: str
    url: HttpUrl
    snippet: str = ""


class Competitor(BaseModel):
    name: str
    slug: str
    domain: str
    homepage: Optional[HttpUrl] = None
    description: str = ""
    discovered_at: datetime = Field(default_factory=_utcnow)


class Document(BaseModel):
    url: HttpUrl
    title: str = ""
    text: str
    content_hash: str
    fetched_at: datetime = Field(default_factory=_utcnow)


class Chunk(BaseModel):
    id: str  # document_hash + position, stable across runs
    document_url: HttpUrl
    position: int
    text: str


class KeywordScore(BaseModel):
    term: str
    score: float
