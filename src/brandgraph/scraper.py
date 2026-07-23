"""Fetch pages politely and extract clean main-body text."""
from __future__ import annotations

import logging
import time
from typing import Optional

import httpx
import trafilatura
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from .config import settings
from .models import Document
from .utils import content_hash

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


class Scraper:
    """Thin wrapper around ``httpx.Client`` that returns cleaned ``Document`` objects."""

    def __init__(self) -> None:
        self._client = httpx.Client(
            timeout=settings.http_timeout,
            headers={"User-Agent": settings.http_user_agent},
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "Scraper":
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    def fetch(self, url: str) -> Optional[Document]:
        try:
            resp = _get(self._client, url)
        except httpx.HTTPError as e:
            log.warning("fetch failed for %s: %s", url, e)
            return None

        html = resp.text
        extracted = trafilatura.extract(
            html,
            url=url,
            include_comments=False,
            include_tables=False,
            favor_precision=True,
        )
        if not extracted or len(extracted.strip()) < 200:
            log.debug("no substantive content for %s", url)
            return None

        title = ""
        metadata = trafilatura.extract_metadata(html)
        if metadata is not None:
            title = (metadata.title or "").strip()

        # be polite between requests
        time.sleep(settings.request_delay_seconds)

        text = extracted.strip()
        return Document(
            url=str(resp.url),
            title=title,
            text=text,
            content_hash=content_hash(text),
        )
