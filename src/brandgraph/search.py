"""Competitor discovery via DuckDuckGo + listicle mining (free, no API key)."""
from __future__ import annotations

import logging
import re
from collections import Counter, OrderedDict, defaultdict
from typing import Iterable
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup
from ddgs import DDGS

from .config import settings
from .models import Competitor, SearchHit
from .utils import is_blocked_domain, registered_domain, slugify

log = logging.getLogger(__name__)

# How many source pages to fetch and mine per discovery run. Each costs one
# HTTP request plus HTML parsing, so keep this modest.
_MAX_SOURCES_TO_MINE = 12

# A candidate domain is only accepted if it's linked from at least this many
# *distinct source pages*. This is what filters out garbage: a real competitor
# gets mentioned by multiple listicles; a random footer link on one page does not.
_MIN_SOURCES_FOR_ACCEPTANCE = 2

# If DDG returned very few usable sources we relax the cross-source requirement
# so the pipeline still produces something.
_RELAX_IF_FEWER_SOURCES = 3

# URL path patterns that scream "meta-analysis about brands, not a brand's own site".
# Sources on these paths are still mined for outbound links but never accepted
# as competitors themselves.
_META_PATH_RE = re.compile(
    r"/("
    r"competitors?|alternatives?|vs|versus|comparison|analysis|report|reports|"
    r"reviews?|wiki|news|article|articles|blog|posts|top-\d+|list|listicles?"
    r")/",
    re.IGNORECASE,
)


def _ddg_text(query: str, max_results: int) -> list[SearchHit]:
    """Run a DuckDuckGo text search and return normalised hits."""
    hits: list[SearchHit] = []
    with DDGS() as ddgs:
        for raw in ddgs.text(query, max_results=max_results):
            url = raw.get("href") or raw.get("url")
            title = raw.get("title") or ""
            snippet = raw.get("body") or ""
            if not url:
                continue
            try:
                hits.append(SearchHit(title=title, url=url, snippet=snippet))
            except Exception:  # pragma: no cover - pydantic url validation
                log.debug("skipping non-http hit: %s", url)
    return hits


def _extract_outbound_domains(html: str, source_domain: str) -> Counter[str]:
    """Return a Counter of external eTLD+1 domains linked from an HTML page."""
    soup = BeautifulSoup(html, "html.parser")
    counts: Counter[str] = Counter()
    for anchor in soup.find_all("a", href=True):
        href = anchor["href"].strip()
        parsed = urlparse(href)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            continue
        dom = registered_domain(href)
        if not dom or dom == source_domain:
            continue
        counts[dom] += 1
    return counts


def _fetch_html(url: str) -> str | None:
    try:
        resp = httpx.get(
            url,
            timeout=settings.http_timeout,
            follow_redirects=True,
            headers={"User-Agent": settings.http_user_agent},
        )
        resp.raise_for_status()
        return resp.text
    except httpx.HTTPError as e:
        log.debug("fetch failed for %s: %s", url, e)
        return None


def _mine_source(
    url: str,
    brand_slug: str,
    seed_domain: str | None,
) -> Counter[str]:
    """Fetch a page and return the outbound-link domain counter after filtering."""
    html = _fetch_html(url)
    if not html:
        return Counter()
    source_domain = registered_domain(url)
    raw = _extract_outbound_domains(html, source_domain)

    filtered: Counter[str] = Counter()
    for dom, count in raw.items():
        if is_blocked_domain(dom):
            continue
        if seed_domain and dom == seed_domain.lower():
            continue
        if brand_slug and brand_slug in dom.replace(".", "-"):
            continue
        filtered[dom] = count
    return filtered


def _is_meta_page(url: str) -> bool:
    return bool(_META_PATH_RE.search(urlparse(url).path))


def discover_competitors(
    brand: str,
    seed_domain: str | None = None,
    max_competitors: int | None = None,
) -> list[Competitor]:
    """
    Discover competitor domains for ``brand``.

    Strategy (blocklist is a safety net, cross-source support is the real filter):

        1. Run several DuckDuckGo queries and collect all hits.
        2. Fetch up to ``_MAX_SOURCES_TO_MINE`` pages and extract every
           external ``<a href>`` domain from each — every page is treated as
           a potential listicle. No "aggregator vs. direct hit" distinction.
        3. A candidate domain is accepted only if it's linked from
           ``_MIN_SOURCES_FOR_ACCEPTANCE`` (default 2) *distinct source pages*.
           This is what rejects one-off footer / boilerplate links from news
           articles, wikis, activist sites, etc.
        4. If fewer than 3 sources were fetched (DDG very sparse), the
           cross-source threshold is relaxed to 1 so we still produce output.
        5. Return top-N candidates by total link count.
    """
    limit = max_competitors or settings.max_competitors
    brand_slug = slugify(brand)
    queries = [
        f"{brand} competitors",
        f"{brand} main competitors",
        f"{brand} top competitors",
        f"{brand} alternatives",
        f"brands like {brand}",
    ]

    all_hits: list[SearchHit] = []
    seen_urls: set[str] = set()
    for q in queries:
        for hit in _ddg_text(q, max_results=15):
            url = str(hit.url)
            if url in seen_urls:
                continue
            seen_urls.add(url)
            all_hits.append(hit)

    # {candidate_domain: total link count across all sources}
    scores: Counter[str] = Counter()
    # {candidate_domain: set of source URLs that linked to it}. Keyed by URL,
    # not domain, so two different articles on the same aggregator count as
    # independent evidence.
    sources_for: dict[str, set[str]] = defaultdict(set)
    hit_by_domain: dict[str, SearchHit] = {}
    sources_fetched = 0

    for hit in all_hits:
        if sources_fetched >= _MAX_SOURCES_TO_MINE:
            break
        url = str(hit.url)
        source_dom = registered_domain(url)
        if not source_dom:
            continue
        if seed_domain and source_dom == seed_domain.lower():
            continue
        if brand_slug in source_dom.replace(".", "-"):
            continue

        mined = _mine_source(url, brand_slug=brand_slug, seed_domain=seed_domain)
        sources_fetched += 1
        if mined:
            log.info(
                "mined %d candidates from %s (source: %s)",
                len(mined),
                url,
                source_dom,
            )
        for cand_dom, count in mined.items():
            scores[cand_dom] += count
            sources_for[cand_dom].add(url)
            hit_by_domain.setdefault(
                cand_dom,
                SearchHit(
                    title=cand_dom,
                    url=f"https://{cand_dom}",
                    snippet=f"Linked from {source_dom}",
                ),
            )

    if not scores:
        log.warning("no competitor domains discovered for '%s'", brand)
        return []

    min_sources = (
        _MIN_SOURCES_FOR_ACCEPTANCE
        if sources_fetched >= _RELAX_IF_FEWER_SOURCES
        else 1
    )
    accepted = [
        (dom, scores[dom])
        for dom in scores
        if len(sources_for[dom]) >= min_sources
    ]
    # Fallback: if the strict threshold produced nothing, take everything.
    if not accepted:
        log.warning(
            "no candidates passed the >=%d source filter for '%s'; falling back to all mined domains",
            min_sources,
            brand,
        )
        accepted = list(scores.items())

    # Sort by (# distinct sources DESC, total link count DESC) — cross-source
    # support is the primary quality signal.
    accepted.sort(key=lambda p: (len(sources_for[p[0]]), p[1]), reverse=True)
    top_domains = [dom for dom, _score in accepted[:limit]]

    ordered: OrderedDict[str, SearchHit] = OrderedDict()
    for dom in top_domains:
        ordered[dom] = hit_by_domain[dom]

    competitors: list[Competitor] = []
    for domain, hit in ordered.items():
        display = domain.split(".")[0].replace("-", " ").title()
        competitors.append(
            Competitor(
                name=display,
                slug=slugify(display),
                domain=domain,
                homepage=f"https://{domain}",
                description=hit.snippet,
            )
        )
    return competitors


def competitors_from_domains(domains: Iterable[str]) -> list[Competitor]:
    """Build ``Competitor`` objects from an explicit list of domains (manual override)."""
    out: list[Competitor] = []
    for raw in domains:
        raw = raw.strip().lower()
        if not raw:
            continue
        # Accept either a bare domain or a full URL.
        parsed = urlparse(raw if "://" in raw else f"https://{raw}")
        dom = registered_domain(parsed.geturl()) or parsed.netloc
        if not dom or is_blocked_domain(dom):
            log.warning("skipping domain '%s' (blocked or invalid)", raw)
            continue
        display = dom.split(".")[0].replace("-", " ").title()
        out.append(
            Competitor(
                name=display,
                slug=slugify(display),
                domain=dom,
                homepage=f"https://{dom}",
                description="Manually provided",
            )
        )
    return out


def find_competitor_pages(competitor: Competitor, max_pages: int | None = None) -> list[SearchHit]:
    """Find a handful of high-signal pages on the competitor's own domain."""
    limit = max_pages or settings.max_pages_per_competitor
    queries: Iterable[str] = (
        f"site:{competitor.domain} about",
        f"site:{competitor.domain} products",
        f"site:{competitor.domain} customers",
        f"site:{competitor.domain}",
    )
    seen: set[str] = set()
    pages: list[SearchHit] = []
    for q in queries:
        for hit in _ddg_text(q, max_results=10):
            url = str(hit.url)
            if url in seen:
                continue
            if registered_domain(url) != competitor.domain:
                continue
            seen.add(url)
            pages.append(hit)
            if len(pages) >= limit:
                return pages
    return pages


def competitors_from_domains(domains: Iterable[str]) -> list[Competitor]:
    """Build ``Competitor`` objects from an explicit list of domains (manual override)."""
    out: list[Competitor] = []
    for raw in domains:
        raw = raw.strip().lower()
        if not raw:
            continue
        # Accept either a bare domain or a full URL.
        parsed = urlparse(raw if "://" in raw else f"https://{raw}")
        dom = registered_domain(parsed.geturl()) or parsed.netloc
        if not dom or is_blocked_domain(dom):
            log.warning("skipping domain '%s' (blocked or invalid)", raw)
            continue
        display = dom.split(".")[0].replace("-", " ").title()
        out.append(
            Competitor(
                name=display,
                slug=slugify(display),
                domain=dom,
                homepage=f"https://{dom}",
                description="Manually provided",
            )
        )
    return out


def find_competitor_pages(competitor: Competitor, max_pages: int | None = None) -> list[SearchHit]:
    """Find a handful of high-signal pages on the competitor's own domain."""
    limit = max_pages or settings.max_pages_per_competitor
    queries: Iterable[str] = (
        f"site:{competitor.domain} about",
        f"site:{competitor.domain} products",
        f"site:{competitor.domain} customers",
        f"site:{competitor.domain}",
    )
    seen: set[str] = set()
    pages: list[SearchHit] = []
    for q in queries:
        for hit in _ddg_text(q, max_results=10):
            url = str(hit.url)
            if url in seen:
                continue
            if registered_domain(url) != competitor.domain:
                continue
            seen.add(url)
            pages.append(hit)
            if len(pages) >= limit:
                return pages
    return pages


def find_competitor_pages(competitor: Competitor, max_pages: int | None = None) -> list[SearchHit]:
    """Find a handful of high-signal pages on the competitor's own domain."""
    limit = max_pages or settings.max_pages_per_competitor
    queries: Iterable[str] = (
        f"site:{competitor.domain} about",
        f"site:{competitor.domain} products",
        f"site:{competitor.domain} customers",
        f"site:{competitor.domain}",
    )
    seen: set[str] = set()
    pages: list[SearchHit] = []
    for q in queries:
        for hit in _ddg_text(q, max_results=10):
            url = str(hit.url)
            if url in seen:
                continue
            if registered_domain(url) != competitor.domain:
                continue
            seen.add(url)
            pages.append(hit)
            if len(pages) >= limit:
                return pages
    return pages
