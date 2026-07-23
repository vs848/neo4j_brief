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


class Product(BaseModel):
    """A sellable SKU published by a competitor (schema.org Product).

    ``id`` is a stable composite of ``competitor_domain`` + ``sku`` so two
    competitors never collide even if they share a SKU string.
    """

    id: str
    competitor_domain: str
    sku: str
    name: str
    size: Optional[str] = None
    variant: Optional[str] = None
    abv: Optional[float] = None
    url: Optional[HttpUrl] = None


class PricePoint(BaseModel):
    """A price observation for a ``Product`` at a specific retailer / moment.

    Append-only: each new observation gets a new ``id`` so history is preserved.
    ``amount`` is stored as ``float`` because Neo4j has no native decimal type;
    keep the ``currency`` explicit for cross-currency queries.
    """

    id: str
    product_id: str
    retailer_domain: str
    currency: str
    amount: float
    market: Optional[str] = None
    seen_at: datetime = Field(default_factory=_utcnow)


class Retailer(BaseModel):
    domain: str
    name: str
