"""Dictionary-based tag extraction over already-scraped competitor text.

The engine assigns typed tags (Theme, Occasion, Sponsorship, ...) to a
competitor by regex-matching a curated taxonomy against their text. Each tag
carries a ``mentions`` count so callers can rank confidence.

Taxonomies are pluggable per vertical. ``GENERIC`` always applies; a vertical
overlay (e.g. ``BEER``) is merged on top so vertical-specific tag values win
over generic ones with the same name.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Mapping

# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

# Tag type (e.g. "Theme") -> tag value (e.g. "sustainability") -> list of
# case-insensitive regex patterns that indicate the tag is present.
Taxonomy = dict[str, dict[str, list[str]]]

# Tag type -> (node label, relationship type) used when persisting to Neo4j.
TAG_SPECS: dict[str, tuple[str, str]] = {
    "Category": ("Category", "IN_CATEGORY"),
    "PriceTier": ("PriceTier", "AT_PRICE_TIER"),
    "ParentCompany": ("ParentCompany", "OWNED_BY"),
    "Audience": ("Audience", "TARGETS"),
    "Occasion": ("Occasion", "FOR_OCCASION"),
    "Theme": ("Theme", "USES_THEME"),
    "Sponsorship": ("Sponsorship", "SPONSORS"),
    "Claim": ("Claim", "MAKES_CLAIM"),
    "Channel": ("Channel", "PRESENT_ON"),
    "Market": ("Market", "PRESENT_IN"),
}


@dataclass
class TagHits:
    """Per-competitor extraction result: {tag_type: {tag_value: mentions}}."""

    counts: dict[str, dict[str, int]] = field(default_factory=dict)

    def bump(self, tag_type: str, value: str, n: int = 1) -> None:
        self.counts.setdefault(tag_type, {})[value] = (
            self.counts.setdefault(tag_type, {}).get(value, 0) + n
        )

    def as_rows(self) -> Iterable[tuple[str, str, int]]:
        for tag_type, values in self.counts.items():
            for value, n in values.items():
                yield tag_type, value, n


# ---------------------------------------------------------------------------
# Taxonomies
# ---------------------------------------------------------------------------

GENERIC: Taxonomy = {
    "Theme": {
        "sustainability": [
            r"\bsustainab\w*", r"\brecycl\w*", r"\bcarbon[- ]neutral\b",
            r"\bnet[- ]zero\b", r"\bcircular economy\b", r"\bregenerative\b",
        ],
        "innovation": [r"\binnovat\w*", r"\bcutting[- ]edge\b", r"\bnext[- ]gen\w*"],
        "heritage": [r"\bheritage\b", r"\btradition\w*", r"\bsince \d{4}\b", r"\bfounded in \d{4}\b"],
        "craft": [r"\bcraft\b", r"\bhandcraft\w*", r"\bartisan\w*"],
        "wellness": [r"\bwellness\b", r"\bwell[- ]being\b", r"\bmindful\w*"],
        "inclusivity": [r"\binclusiv\w*", r"\bdiversity\b", r"\bequity\b"],
        "adventure": [r"\badventure\b", r"\bexplor\w*", r"\bwild\b"],
        "community": [r"\bcommunity\b", r"\btogetherness\b", r"\bshared moments\b"],
        "quality": [r"\bpremium quality\b", r"\bhighest quality\b", r"\buncompromising\b"],
        "authenticity": [r"\bauthentic\w*", r"\breal\b", r"\bgenuine\b"],
    },
    "Audience": {
        "gen z": [r"\bgen[- ]?z\b", r"\bgeneration z\b"],
        "millennials": [r"\bmillennial\w*"],
        "gen x": [r"\bgen[- ]?x\b", r"\bgeneration x\b"],
        "boomers": [r"\bbaby boomer\w*", r"\bboomers\b"],
        "families": [r"\bfamil(y|ies)\b", r"\bparents\b"],
        "professionals": [r"\bprofessional\w*", r"\bcorporate\b"],
        "students": [r"\bstudent\w*", r"\buniversity\b", r"\bcollege\b"],
        "urban": [r"\burban\b", r"\bcity[- ]dweller\w*"],
    },
    "Occasion": {
        "at-home": [r"\bat[- ]home\b", r"\bhome consumption\b"],
        "dining": [r"\brestaurant\w*", r"\bdining\b", r"\bfine dining\b"],
        "nightlife": [r"\bnightlife\b", r"\bnightclub\w*", r"\bbar[s]?\b"],
        "sports viewing": [r"\bmatch[- ]?day\b", r"\bgame[- ]?day\b", r"\bwatch(ing)? (the )?(match|game)\b"],
        "festival": [r"\bfestival\w*"],
        "celebration": [r"\bcelebrat\w*", r"\btoast\b"],
        "travel": [r"\btravel\w*", r"\bvacation\b", r"\bholiday\b"],
    },
    "Channel": {
        "instagram": [r"\binstagram\b"],
        "tiktok": [r"\btiktok\b"],
        "youtube": [r"\byoutube\b"],
        "facebook": [r"\bfacebook\b"],
        "linkedin": [r"\blinkedin\b"],
        "twitter/x": [r"\btwitter\b", r"\bx\.com\b"],
        "podcast": [r"\bpodcast\w*"],
        "tv": [r"\btv commercial\b", r"\bbroadcast\b", r"\btelevision\b"],
        "out-of-home": [r"\bbillboard\w*", r"\bout[- ]of[- ]home\b", r"\bOOH\b"],
        "retail media": [r"\bretail media\b"],
    },
    "Claim": {
        "premium": [r"\bpremium\b"],
        "sustainable packaging": [r"\brecyclable\b", r"\bcompostable\b", r"\bplastic[- ]free\b"],
        "award-winning": [r"\baward[- ]winning\b", r"\bwinner\b"],
        "natural ingredients": [r"\bnatural ingredients\b", r"\ball[- ]natural\b", r"\bno additives\b"],
        "responsible": [r"\bresponsibl(e|y)\b"],
    },
    "Market": {
        "united states": [r"\bunited states\b", r"\bU\.?S\.?A\.?\b"],
        "united kingdom": [r"\bunited kingdom\b", r"\bU\.?K\.?\b", r"\bbritain\b"],
        "europe": [r"\beurope\b", r"\bEU\b"],
        "asia": [r"\basia\b", r"\basia[- ]pacific\b", r"\bAPAC\b"],
        "latin america": [r"\blatin america\b", r"\bLATAM\b"],
        "china": [r"\bchina\b"],
        "india": [r"\bindia\b"],
    },
}


BEER: Taxonomy = {
    "Category": {
        "lager": [r"\blager\b", r"\bpilsner\b", r"\bpils\b"],
        "stout": [r"\bstout\b", r"\bporter\b"],
        "ale": [r"\bale\b", r"\bIPA\b", r"\bpale ale\b"],
        "wheat beer": [r"\bwheat beer\b", r"\bwitbier\b", r"\bhefeweizen\b"],
        "non-alcoholic": [r"\b0\.0\b", r"\bnon[- ]?alcoholic\b", r"\balcohol[- ]?free\b"],
        "hard seltzer": [r"\bhard seltzer\b", r"\bseltzer\b"],
        "cider": [r"\bcider\b"],
    },
    "PriceTier": {
        "mainstream": [r"\bmainstream\b", r"\beveryday\b", r"\bvalue segment\b"],
        "premium": [r"\bpremium\b"],
        "super-premium": [r"\bsuper[- ]?premium\b", r"\bluxury\b", r"\bprestige\b"],
        "craft": [r"\bcraft brew\w*", r"\bmicrobrew\w*", r"\bindependent brewer\w*"],
    },
    "ParentCompany": {
        "Heineken NV": [r"\bheineken (n\.?v\.?|holding|group|international)\b"],
        "AB InBev": [r"\bAB[- ]?InBev\b", r"\banheuser[- ]?busch\b"],
        "Carlsberg Group": [r"\bcarlsberg (group|breweries)\b"],
        "Asahi Group": [r"\basahi (group|holdings|breweries)\b"],
        "Molson Coors": [r"\bmolson coors\b"],
        "Diageo": [r"\bdiageo\b"],
        "Constellation Brands": [r"\bconstellation brands\b"],
        "Kirin": [r"\bkirin (holdings|brewery)\b"],
        "Suntory": [r"\bsuntory\b"],
    },
    "Sponsorship": {
        "UEFA Champions League": [r"\bchampions league\b", r"\bUCL\b"],
        "UEFA Euro": [r"\bUEFA Euro\b", r"\bEuro 20\d{2}\b"],
        "Formula 1": [r"\bformula 1\b", r"\bformula one\b", r"\bF1\b"],
        "Rugby World Cup": [r"\brugby world cup\b", r"\bRWC\b"],
        "NFL / Super Bowl": [r"\bNFL\b", r"\bsuper bowl\b"],
        "NBA": [r"\bNBA\b"],
        "Premier League": [r"\bpremier league\b", r"\bEPL\b"],
        "Olympics": [r"\bolympic\w*\b"],
        "Coachella": [r"\bcoachella\b"],
        "Tomorrowland": [r"\btomorrowland\b"],
    },
    "Occasion": {
        "matchday": [r"\bmatch[- ]?day\b", r"\bgame[- ]?day\b"],
        "nightlife": [r"\bnightlife\b", r"\bnightclub\w*"],
        "at-home relaxation": [r"\bafter work\b", r"\bunwind\b", r"\bat home\b"],
        "fine dining": [r"\bfine dining\b", r"\brestaurant pairing\b"],
        "festival": [r"\bfestival\w*"],
        "sober-curious": [r"\bsober[- ]?curious\b", r"\bmindful drinking\b"],
    },
    "Theme": {
        "responsible drinking": [r"\bdrink responsibly\b", r"\bresponsible drinking\b", r"\bmoderation\b"],
        "brewing craftsmanship": [r"\bmaster brewer\b", r"\bbrewing tradition\b", r"\bbrewed since\b"],
        "friendship": [r"\bfriendship\b", r"\bcheers\b", r"\btogether\b"],
        "refreshment": [r"\brefreshing\b", r"\bcrisp\b", r"\bthirst quench\w*"],
    },
    "Claim": {
        "0.0 / non-alcoholic": [r"\b0\.0\b", r"\bnon[- ]?alcoholic\b", r"\balcohol[- ]?free\b"],
        "low calorie": [r"\blow[- ]calorie\b", r"\blight beer\b"],
        "low carb": [r"\blow[- ]carb\b"],
        "gluten free": [r"\bgluten[- ]free\b"],
        "imported": [r"\bimported\b"],
        "brewed with pure water": [r"\bpure water\b", r"\bmountain water\b", r"\bspring water\b"],
    },
    "Market": {
        "netherlands": [r"\bnetherlands\b", r"\bdutch\b"],
        "mexico": [r"\bmexico\b", r"\bmexican\b"],
        "belgium": [r"\bbelgium\b", r"\bbelgian\b"],
        "ireland": [r"\bireland\b", r"\birish\b"],
        "japan": [r"\bjapan\b", r"\bjapanese\b"],
        "germany": [r"\bgermany\b", r"\bgerman\b"],
        "italy": [r"\bitaly\b", r"\bitalian\b"],
        "denmark": [r"\bdenmark\b", r"\bdanish\b"],
    },
}


VERTICALS: Mapping[str, Taxonomy] = {
    "generic": {},
    "beer": BEER,
}


def build_taxonomy(vertical: str = "generic") -> Taxonomy:
    """Return GENERIC merged with the requested vertical overlay."""
    overlay = VERTICALS.get(vertical.lower(), {})
    merged: Taxonomy = {}
    for tag_type in set(GENERIC) | set(overlay):
        merged[tag_type] = {**GENERIC.get(tag_type, {}), **overlay.get(tag_type, {})}
    return merged


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


class TaggingEngine:
    """Pre-compile taxonomy patterns and count matches over arbitrary text."""

    def __init__(self, taxonomy: Taxonomy | None = None, vertical: str = "generic") -> None:
        self.taxonomy = taxonomy if taxonomy is not None else build_taxonomy(vertical)
        self._compiled: dict[str, dict[str, list[re.Pattern[str]]]] = {
            tag_type: {
                value: [re.compile(p, re.IGNORECASE) for p in patterns]
                for value, patterns in values.items()
            }
            for tag_type, values in self.taxonomy.items()
        }

    def extract(self, text: str) -> TagHits:
        hits = TagHits()
        if not text:
            return hits
        for tag_type, values in self._compiled.items():
            for value, patterns in values.items():
                total = 0
                for pat in patterns:
                    total += len(pat.findall(text))
                if total:
                    hits.bump(tag_type, value, total)
        return hits

    def extract_many(self, texts: Iterable[str]) -> TagHits:
        merged = TagHits()
        for text in texts:
            partial = self.extract(text)
            for tag_type, value, n in partial.as_rows():
                merged.bump(tag_type, value, n)
        return merged
