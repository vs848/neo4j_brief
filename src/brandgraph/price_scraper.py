"""
Extract structured product & price data from HTML pages.

Two strategies, in order of preference:

1. **JSON-LD** (``<script type="application/ld+json">``) — schema.org
   ``Product`` markup with ``offers``. This is the most reliable path and
   the majority of DTC / e-commerce sites publish it.
2. **Open Graph** (``<meta property="og:price:amount" ...>``) — fallback for
   single-product landing pages that don't ship JSON-LD.

No LLMs; no headless browser. Just ``httpx`` + ``bs4``. Retailer-specific
CSS selectors are intentionally not added — extend here when a target site
proves it needs it, keep the generic path clean.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Iterable, Iterator, Optional

import httpx
from bs4 import BeautifulSoup
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from .config import settings
from .models import PricePoint, Product
from .utils import registered_domain

log = logging.getLogger(__name__)


@retry(
    reraise=True,
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=8),
    retry=retry_if_exception_type((httpx.HTTPError,)),
)
def _get(client: httpx.Client, url: str) -> httpx.Response:
    resp = client.get(url, follow_redirects=True)
    resp.raise_for_status()
    return resp


class PriceScraper:
    """HTTP client + extractor. Reuses the project's polite-fetch conventions."""

    def __init__(self) -> None:
        self._client = httpx.Client(
            timeout=settings.http_timeout,
            headers={"User-Agent": settings.http_user_agent},
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "PriceScraper":
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    # ---- public API ---------------------------------------------------------

    def extract(
        self,
        url: str,
        competitor_domain: str,
    ) -> list[tuple[Product, PricePoint]]:
        """Fetch ``url`` and return ``(Product, PricePoint)`` pairs found on it."""
        try:
            resp = _get(self._client, url)
        except httpx.HTTPError as e:
            log.warning("price fetch failed for %s: %s", url, e)
            return []

        page_url = str(resp.url)
        html = resp.text

        seen_at = datetime.now(tz=timezone.utc)
        retailer = registered_domain(page_url)

        pairs = list(
            _extract_from_html(
                html,
                page_url=page_url,
                competitor_domain=competitor_domain,
                retailer_domain=retailer,
                seen_at=seen_at,
            )
        )

        # be polite between requests
        time.sleep(settings.request_delay_seconds)
        return pairs


# ---- pure-function extractors ---------------------------------------------


def _extract_from_html(
    html: str,
    *,
    page_url: str,
    competitor_domain: str,
    retailer_domain: str,
    seen_at: datetime,
) -> Iterator[tuple[Product, PricePoint]]:
    soup = BeautifulSoup(html, "html.parser")

    # 1. JSON-LD Product blocks
    for block in soup.find_all("script", attrs={"type": "application/ld+json"}):
        payload = _safe_json(block.string or block.get_text() or "")
        if payload is None:
            continue
        for node in _iter_products(payload):
            pair = _pair_from_jsonld_product(
                node,
                page_url=page_url,
                competitor_domain=competitor_domain,
                retailer_domain=retailer_domain,
                seen_at=seen_at,
            )
            if pair is not None:
                yield pair

    # 2. Open Graph fallback (only if we didn't already yield anything)
    #    We can't tell "already yielded" from inside a generator cheaply, so
    #    we always try; callers dedupe by Product.id via MERGE anyway.
    og_pair = _pair_from_open_graph(
        soup,
        page_url=page_url,
        competitor_domain=competitor_domain,
        retailer_domain=retailer_domain,
        seen_at=seen_at,
    )
    if og_pair is not None:
        yield og_pair


def _safe_json(text: str) -> Any:
    text = text.strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Some sites wrap JSON-LD in HTML comments or emit trailing commas;
        # not worth chasing every variant here.
        return None


def _iter_products(node: Any) -> Iterator[dict[str, Any]]:
    """Yield every dict in ``node`` whose ``@type`` is (or contains) ``Product``."""
    if isinstance(node, list):
        for n in node:
            yield from _iter_products(n)
    elif isinstance(node, dict):
        node_type = node.get("@type")
        if _is_product_type(node_type):
            yield node
        # schema.org allows nesting via @graph / hasVariant / isVariantOf
        for key in ("@graph", "hasVariant", "isVariantOf", "itemListElement"):
            v = node.get(key)
            if isinstance(v, (dict, list)):
                yield from _iter_products(v)


def _is_product_type(t: Any) -> bool:
    if t is None:
        return False
    if isinstance(t, str):
        return t.lower() == "product"
    if isinstance(t, list):
        return any(isinstance(x, str) and x.lower() == "product" for x in t)
    return False


def _pair_from_jsonld_product(
    node: dict[str, Any],
    *,
    page_url: str,
    competitor_domain: str,
    retailer_domain: str,
    seen_at: datetime,
) -> Optional[tuple[Product, PricePoint]]:
    offers = _first_offer(node.get("offers"))
    if not offers:
        return None

    price = _coerce_price(offers.get("price") or offers.get("lowPrice"))
    currency = offers.get("priceCurrency")
    if price is None or not currency:
        return None

    sku = _first(node.get("sku") or node.get("mpn") or node.get("productID"))
    name = _first(node.get("name")) or ""
    if not sku:
        # Fall back to a URL-derived id so we don't lose the observation.
        sku = _slug_from_url(page_url) or name[:64]
    if not sku:
        return None

    product = Product(
        id=f"{competitor_domain}::{sku}",
        competitor_domain=competitor_domain,
        sku=str(sku),
        name=str(name)[:200] if name else str(sku),
        size=_stringify(node.get("size")),
        variant=_stringify(node.get("color") or node.get("material")),
        url=page_url,  # type: ignore[arg-type]  # pydantic will validate
    )
    pp = PricePoint(
        id=f"{product.id}::{retailer_domain}::{seen_at.isoformat()}",
        product_id=product.id,
        retailer_domain=retailer_domain,
        currency=str(currency).upper(),
        amount=float(price),
        seen_at=seen_at,
    )
    return product, pp


def _pair_from_open_graph(
    soup: BeautifulSoup,
    *,
    page_url: str,
    competitor_domain: str,
    retailer_domain: str,
    seen_at: datetime,
) -> Optional[tuple[Product, PricePoint]]:
    def meta(prop: str) -> Optional[str]:
        tag = soup.find("meta", attrs={"property": prop})
        if tag and tag.get("content"):
            return str(tag["content"]).strip()
        return None

    amount = meta("product:price:amount") or meta("og:price:amount")
    currency = meta("product:price:currency") or meta("og:price:currency")
    if not amount or not currency:
        return None
    price = _coerce_price(amount)
    if price is None:
        return None

    name = meta("og:title") or ""
    sku = _slug_from_url(page_url) or name[:64]
    if not sku:
        return None

    product = Product(
        id=f"{competitor_domain}::{sku}",
        competitor_domain=competitor_domain,
        sku=str(sku),
        name=str(name)[:200] if name else str(sku),
        url=page_url,  # type: ignore[arg-type]
    )
    pp = PricePoint(
        id=f"{product.id}::{retailer_domain}::{seen_at.isoformat()}",
        product_id=product.id,
        retailer_domain=retailer_domain,
        currency=str(currency).upper(),
        amount=float(price),
        seen_at=seen_at,
    )
    return product, pp


# ---- tiny helpers ---------------------------------------------------------


def _first_offer(offers: Any) -> dict[str, Any]:
    if isinstance(offers, dict):
        # AggregateOffer wraps individual Offer instances
        inner = offers.get("offers")
        if isinstance(inner, list) and inner:
            head = inner[0]
            if isinstance(head, dict):
                return head
        return offers
    if isinstance(offers, list) and offers:
        head = offers[0]
        if isinstance(head, dict):
            return head
    return {}


def _first(v: Any) -> Any:
    if isinstance(v, list):
        return v[0] if v else None
    return v


def _stringify(v: Any) -> Optional[str]:
    if v is None:
        return None
    if isinstance(v, (list, tuple)):
        return ", ".join(str(x) for x in v if x) or None
    return str(v)


def _coerce_price(v: Any) -> Optional[float]:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace(",", "")
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        # Strip currency symbols and try again
        cleaned = "".join(ch for ch in s if ch.isdigit() or ch == ".")
        if not cleaned:
            return None
        try:
            return float(cleaned)
        except ValueError:
            return None


def _slug_from_url(url: str) -> str:
    from urllib.parse import urlparse

    path = urlparse(url).path.rstrip("/")
    if not path:
        return ""
    return path.rsplit("/", 1)[-1][:64]


__all__ = ["PriceScraper", "_extract_from_html"]


# Silence "unused" warnings from re-exports used only for tests.
_ = Iterable
