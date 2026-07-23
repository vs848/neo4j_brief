"""Small text/url helpers shared across modules."""
from __future__ import annotations

import hashlib
import re
import unicodedata
from urllib.parse import urlparse

import tldextract

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(text: str) -> str:
    """Normalise a string into a URL-safe slug."""
    normalised = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    lowered = normalised.lower().strip()
    slug = _SLUG_RE.sub("-", lowered).strip("-")
    return slug or "unnamed"


def registered_domain(url: str) -> str:
    """Return the eTLD+1 for a URL, e.g. ``https://blog.nike.com/x`` -> ``nike.com``."""
    ext = tldextract.extract(url)
    if not ext.domain:
        return urlparse(url).netloc.lower()
    if ext.suffix:
        return f"{ext.domain}.{ext.suffix}".lower()
    return ext.domain.lower()


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


# Domains that are almost never a competitor (news, social, aggregators, marketplaces,
# competitive-intel listicle sites, market-research vendors).
BLOCKED_DOMAINS: frozenset[str] = frozenset(
    {
        # search / social / video
        "wikipedia.org",
        "youtube.com",
        "linkedin.com",
        "facebook.com",
        "instagram.com",
        "twitter.com",
        "x.com",
        "reddit.com",
        "tiktok.com",
        "pinterest.com",
        "quora.com",
        "medium.com",
        "substack.com",
        "yahoo.com",
        "google.com",
        "bing.com",
        "duckduckgo.com",
        # marketplaces / retailers
        "amazon.com",
        "ebay.com",
        "walmart.com",
        "target.com",
        "shopify.com",
        "etsy.com",
        # news / business media
        "forbes.com",
        "bloomberg.com",
        "nytimes.com",
        "cnbc.com",
        "businessinsider.com",
        "techcrunch.com",
        "wsj.com",
        "reuters.com",
        "ft.com",
        "economist.com",
        # HR / reviews
        "glassdoor.com",
        "indeed.com",
        "trustpilot.com",
        # competitive-intel / company-database aggregators (the big offenders)
        "cbinsights.com",
        "tracxn.com",
        "owler.com",
        "comparably.com",
        "craft.co",
        "crunchbase.com",
        "pitchbook.com",
        "similarweb.com",
        "growjo.com",
        "getlatka.com",
        "explodingtopics.com",
        "zoominfo.com",
        "rocketreach.co",
        "apollo.io",
        "6sense.com",
        "clearbit.com",
        "apistemic.com",
        "foresightiq.co",
        "thebrandhopper.com",
        "brandvm.com",
        "brandcredential.com",
        "feedough.com",
        "marketing91.com",
        "businessmodelanalyst.com",
        "hivelr.com",
        "latterly.org",
        "dcfmodeling.com",
        "businessdit.com",
        "mbaknol.com",
        "strategyzer.com",
        "startuptalky.com",
        "canvasbusinessmodel.com",
        # market-research vendors
        "statista.com",
        "ibisworld.com",
        "marketresearch.com",
        "mordorintelligence.com",
        "researchandmarkets.com",
        "expertmarketresearch.com",
        "grandviewresearch.com",
        "fortunebusinessinsights.com",
        "marketsandmarkets.com",
        "alliedmarketresearch.com",
        "polarismarketresearch.com",
        "openpr.com",
        "prnewswire.com",
        "businesswire.com",
        # SaaS review / comparison
        "g2.com",
        "capterra.com",
        "gartner.com",
        "trustradius.com",
        "softwareadvice.com",
        # marketing / martech / CMS tooling that shows up on business-analysis blogs
        "hubspot.com",
        "salesforce.com",
        "mailchimp.com",
        "wordpress.com",
        "wordpress.org",
        "wpconsent.com",
        "wpbeginner.com",
        "wpengine.com",
        "wix.com",
        "squarespace.com",
        "cloudflare.com",
        "gravatar.com",
        "w3.org",
        "schema.org",
        "creativecommons.org",
        "archive.org",
        "wayback.archive.org",
    }
)


def is_blocked_domain(domain: str) -> bool:
    d = domain.lower()
    return any(d == b or d.endswith("." + b) for b in BLOCKED_DOMAINS)
