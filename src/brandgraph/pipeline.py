"""End-to-end ingestion: discover competitors, scrape their pages, write to Neo4j."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .chunker import chunk_document
from .config import settings
from .graph import GraphStore
from .keywords import top_keywords
from .models import Brand, Competitor, Document
from .scraper import Scraper
from .search import competitors_from_domains, discover_competitors, find_competitor_pages
from .utils import registered_domain, slugify

log = logging.getLogger(__name__)


@dataclass
class IngestStats:
    brand: str
    competitors: int = 0
    documents: int = 0
    chunks: int = 0
    keywords: int = 0
    failed: list[str] = field(default_factory=list)


def _ingest_competitor(
    scraper: Scraper,
    store: GraphStore,
    brand_slug: str,
    competitor: Competitor,
    stats: IngestStats,
) -> None:
    store.upsert_competitor(brand_slug, competitor)

    pages = find_competitor_pages(competitor)
    if not pages:
        log.info("no pages found on-domain for %s", competitor.domain)
        return

    docs: list[Document] = []
    for hit in pages:
        doc = scraper.fetch(str(hit.url))
        if doc is None:
            stats.failed.append(str(hit.url))
            continue
        # Enforce that we only keep pages on the competitor's own domain.
        if registered_domain(str(doc.url)) != competitor.domain:
            continue
        store.upsert_document(competitor.domain, doc)
        chunks = list(chunk_document(doc))
        store.upsert_chunks(str(doc.url), chunks)
        stats.documents += 1
        stats.chunks += len(chunks)
        docs.append(doc)

    if docs:
        keywords = top_keywords([d.text for d in docs])
        store.upsert_keywords(competitor.domain, keywords)
        stats.keywords += len(keywords)


def run_ingest(
    brand_name: str,
    seed_domain: str | None = None,
    reset: bool = False,
    competitor_domains: list[str] | None = None,
) -> IngestStats:
    """
    Run the full pipeline for ``brand_name``.

    Steps:
        1. Ensure Neo4j schema exists.
        2. (Optional) Wipe any previous graph for this brand.
        3. Upsert the brand.
        4. Either use ``competitor_domains`` if provided, or discover
           competitors via DuckDuckGo + listicle mining.
        5. For each competitor: find on-domain pages, scrape, chunk, keyword, and upsert.
    """
    brand = Brand(name=brand_name, slug=slugify(brand_name), seed_domain=seed_domain)
    stats = IngestStats(brand=brand.slug)

    with GraphStore() as store:
        store.ensure_schema()
        if reset:
            store.wipe_brand(brand.slug)
        store.upsert_brand(brand)

        if competitor_domains:
            competitors = competitors_from_domains(competitor_domains)
            log.info("using %d manually provided competitors for %s", len(competitors), brand_name)
        else:
            competitors = discover_competitors(brand_name, seed_domain=seed_domain)
            log.info("discovered %d competitors for %s", len(competitors), brand_name)
        stats.competitors = len(competitors)

        with Scraper() as scraper:
            for competitor in competitors:
                try:
                    _ingest_competitor(scraper, store, brand.slug, competitor, stats)
                except Exception as e:  # keep going on per-competitor failures
                    log.exception("failed to ingest %s: %s", competitor.domain, e)
                    stats.failed.append(competitor.domain)

    return stats
