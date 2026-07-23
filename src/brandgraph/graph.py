"""Neo4j client: schema management and idempotent upserts for the brand graph.

Graph model
-----------
Nodes
    (:Brand       {slug, name, created_at})
    (:Competitor  {slug, name, domain, homepage, description, discovered_at})
    (:Document    {url, title, content_hash, fetched_at})
    (:Chunk       {id, position, text})
    (:Keyword     {term})
    (:Product     {id, sku, name, size, variant, abv, url})
    (:PricePoint  {id, currency, amount, market, seen_at})
    (:Retailer    {domain, name})

Relationships
    (:Brand)-[:COMPETES_WITH]->(:Competitor)
    (:Competitor)-[:HAS_DOCUMENT]->(:Document)
    (:Document)-[:HAS_CHUNK]->(:Chunk)
    (:Competitor)-[:TAGGED_WITH {score}]->(:Keyword)
    (:Competitor)-[:SELLS]->(:Product)
    (:Product)-[:PRICED_AT]->(:PricePoint)
    (:PricePoint)-[:AT_RETAILER]->(:Retailer)
"""
from __future__ import annotations

import logging
from typing import Iterable

from neo4j import Driver, GraphDatabase

from .config import settings
from .models import Brand, Chunk, Competitor, Document, KeywordScore, PricePoint, Product
from .taxonomies import TAG_SPECS, TagHits

log = logging.getLogger(__name__)


def _tag_constraints() -> tuple[str, ...]:
    """Emit a UNIQUE constraint per typed tag node label."""
    return tuple(
        f"CREATE CONSTRAINT {label.lower()}_name IF NOT EXISTS "
        f"FOR (n:{label}) REQUIRE n.name IS UNIQUE"
        for label, _rel in TAG_SPECS.values()
    )


SCHEMA_STATEMENTS: tuple[str, ...] = (
    "CREATE CONSTRAINT brand_slug IF NOT EXISTS FOR (b:Brand) REQUIRE b.slug IS UNIQUE",
    "CREATE CONSTRAINT competitor_domain IF NOT EXISTS FOR (c:Competitor) REQUIRE c.domain IS UNIQUE",
    "CREATE CONSTRAINT document_url IF NOT EXISTS FOR (d:Document) REQUIRE d.url IS UNIQUE",
    "CREATE CONSTRAINT chunk_id IF NOT EXISTS FOR (ch:Chunk) REQUIRE ch.id IS UNIQUE",
    "CREATE CONSTRAINT keyword_term IF NOT EXISTS FOR (k:Keyword) REQUIRE k.term IS UNIQUE",
    "CREATE CONSTRAINT product_id IF NOT EXISTS FOR (p:Product) REQUIRE p.id IS UNIQUE",
    "CREATE CONSTRAINT retailer_domain IF NOT EXISTS FOR (r:Retailer) REQUIRE r.domain IS UNIQUE",
    "CREATE CONSTRAINT price_point_id IF NOT EXISTS FOR (pp:PricePoint) REQUIRE pp.id IS UNIQUE",
    "CREATE INDEX price_point_seen_at IF NOT EXISTS FOR (pp:PricePoint) ON (pp.seen_at)",
    "CREATE FULLTEXT INDEX chunk_text IF NOT EXISTS FOR (ch:Chunk) ON EACH [ch.text]",
) + _tag_constraints()


class GraphStore:
    """Thin wrapper around the Neo4j driver with upsert helpers."""

    def __init__(self) -> None:
        self._driver: Driver = GraphDatabase.driver(
            settings.neo4j_uri,
            auth=(settings.neo4j_user, settings.neo4j_password),
        )
        self._db = settings.neo4j_database

    def close(self) -> None:
        self._driver.close()

    def __enter__(self) -> "GraphStore":
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    # ---- schema -------------------------------------------------------------

    def ensure_schema(self) -> None:
        with self._driver.session(database=self._db) as session:
            for stmt in SCHEMA_STATEMENTS:
                session.run(stmt)

    def wipe_brand(self, brand_slug: str) -> None:
        """Remove a brand and every node reachable only through it. Destructive."""
        cypher = """
        MATCH (b:Brand {slug: $slug})
        OPTIONAL MATCH (b)-[:COMPETES_WITH]->(c:Competitor)
        OPTIONAL MATCH (c)-[:HAS_DOCUMENT]->(d:Document)
        OPTIONAL MATCH (d)-[:HAS_CHUNK]->(ch:Chunk)
        OPTIONAL MATCH (c)-[:SELLS]->(p:Product)
        OPTIONAL MATCH (p)-[:PRICED_AT]->(pp:PricePoint)
        DETACH DELETE pp, p, ch, d, c, b
        """
        with self._driver.session(database=self._db) as session:
            session.run(cypher, slug=brand_slug)
        with self._driver.session(database=self._db) as session:
            session.run(cypher, slug=brand_slug)

    # ---- writes -------------------------------------------------------------

    def upsert_brand(self, brand: Brand) -> None:
        cypher = """
        MERGE (b:Brand {slug: $slug})
        ON CREATE SET b.name = $name, b.created_at = datetime($created_at)
        SET b.name = $name
        """
        with self._driver.session(database=self._db) as session:
            session.run(
                cypher,
                slug=brand.slug,
                name=brand.name,
                created_at=brand.created_at.isoformat(),
            )

    def upsert_competitor(self, brand_slug: str, competitor: Competitor) -> None:
        cypher = """
        MERGE (c:Competitor {domain: $domain})
        ON CREATE SET c.discovered_at = datetime($discovered_at)
        SET c.name = $name,
            c.slug = $slug,
            c.homepage = $homepage,
            c.description = coalesce($description, c.description)
        WITH c
        MATCH (b:Brand {slug: $brand_slug})
        MERGE (b)-[:COMPETES_WITH]->(c)
        """
        with self._driver.session(database=self._db) as session:
            session.run(
                cypher,
                brand_slug=brand_slug,
                domain=competitor.domain,
                slug=competitor.slug,
                name=competitor.name,
                homepage=str(competitor.homepage) if competitor.homepage else None,
                description=competitor.description or None,
                discovered_at=competitor.discovered_at.isoformat(),
            )

    def upsert_document(self, competitor_domain: str, doc: Document) -> None:
        cypher = """
        MERGE (d:Document {url: $url})
        ON CREATE SET d.fetched_at = datetime($fetched_at)
        SET d.title = $title, d.content_hash = $content_hash
        WITH d
        MATCH (c:Competitor {domain: $domain})
        MERGE (c)-[:HAS_DOCUMENT]->(d)
        """
        with self._driver.session(database=self._db) as session:
            session.run(
                cypher,
                domain=competitor_domain,
                url=str(doc.url),
                title=doc.title,
                content_hash=doc.content_hash,
                fetched_at=doc.fetched_at.isoformat(),
            )

    def upsert_chunks(self, document_url: str, chunks: Iterable[Chunk]) -> int:
        rows = [
            {"id": ch.id, "position": ch.position, "text": ch.text}
            for ch in chunks
        ]
        if not rows:
            return 0
        cypher = """
        MATCH (d:Document {url: $url})
        UNWIND $rows AS row
        MERGE (ch:Chunk {id: row.id})
        SET ch.position = row.position, ch.text = row.text
        MERGE (d)-[:HAS_CHUNK]->(ch)
        """
        with self._driver.session(database=self._db) as session:
            session.run(cypher, url=document_url, rows=rows)
        return len(rows)

    def upsert_keywords(self, competitor_domain: str, keywords: Iterable[KeywordScore]) -> int:
        rows = [{"term": k.term, "score": k.score} for k in keywords]
        if not rows:
            return 0
        cypher = """
        MATCH (c:Competitor {domain: $domain})
        UNWIND $rows AS row
        MERGE (k:Keyword {term: row.term})
        MERGE (c)-[r:TAGGED_WITH]->(k)
        SET r.score = row.score
        """
        with self._driver.session(database=self._db) as session:
            session.run(cypher, domain=competitor_domain, rows=rows)
        return len(rows)

    def upsert_tags(self, competitor_domain: str, hits: TagHits) -> int:
        """
        Materialise typed tag nodes and their relationships for one competitor.

        Each ``TagHits`` entry becomes ``(:<Label> {name})`` linked to the
        competitor via a spec-driven relationship type carrying ``mentions``.
        Returns the total number of (tag_type, value) pairs written.
        """
        written = 0
        for tag_type, values in hits.counts.items():
            spec = TAG_SPECS.get(tag_type)
            if spec is None:
                log.debug("no spec for tag_type=%s; skipping", tag_type)
                continue
            label, rel = spec
            rows = [{"name": v, "mentions": n} for v, n in values.items()]
            if not rows:
                continue
            cypher = (
                "MATCH (c:Competitor {domain: $domain}) "
                "UNWIND $rows AS row "
                f"MERGE (n:{label} {{name: row.name}}) "
                f"MERGE (c)-[r:{rel}]->(n) "
                "SET r.mentions = row.mentions"
            )
            with self._driver.session(database=self._db) as session:
                session.run(cypher, domain=competitor_domain, rows=rows)
            written += len(rows)
        return written

    def upsert_product(self, product: Product) -> None:
        """Upsert a ``:Product`` and attach it to its competitor via ``:SELLS``."""
        cypher = """
        MERGE (p:Product {id: $id})
        SET p.sku = $sku,
            p.name = $name,
            p.size = $size,
            p.variant = $variant,
            p.abv = $abv,
            p.url = $url
        WITH p
        MATCH (c:Competitor {domain: $competitor_domain})
        MERGE (c)-[:SELLS]->(p)
        """
        with self._driver.session(database=self._db) as session:
            session.run(
                cypher,
                id=product.id,
                sku=product.sku,
                name=product.name,
                size=product.size,
                variant=product.variant,
                abv=product.abv,
                url=str(product.url) if product.url else None,
                competitor_domain=product.competitor_domain,
            )

    def upsert_price_point(self, pp: PricePoint) -> None:
        """
        Append a ``:PricePoint`` observation for a ``:Product`` at a retailer.

        Idempotent per ``(product, retailer, seen_at)`` — re-running the same
        scrape within the same second is a no-op; a later scrape adds a new
        point, preserving history.
        """
        cypher = """
        MERGE (r:Retailer {domain: $retailer_domain})
          ON CREATE SET r.name = $retailer_domain
        MERGE (pp:PricePoint {id: $id})
          ON CREATE SET pp.currency = $currency,
                        pp.amount   = $amount,
                        pp.market   = $market,
                        pp.seen_at  = datetime($seen_at)
        WITH pp, r
        MATCH (p:Product {id: $product_id})
        MERGE (p)-[:PRICED_AT]->(pp)
        MERGE (pp)-[:AT_RETAILER]->(r)
        """
        with self._driver.session(database=self._db) as session:
            session.run(
                cypher,
                id=pp.id,
                product_id=pp.product_id,
                retailer_domain=pp.retailer_domain,
                currency=pp.currency,
                amount=pp.amount,
                market=pp.market,
                seen_at=pp.seen_at.isoformat(),
            )

    # ---- reads --------------------------------------------------------------

    def list_competitors(self, brand_slug: str) -> list[dict[str, object]]:
        cypher = """
        MATCH (b:Brand {slug: $slug})-[:COMPETES_WITH]->(c:Competitor)
        OPTIONAL MATCH (c)-[:HAS_DOCUMENT]->(d:Document)
        RETURN c.name AS name, c.domain AS domain, c.homepage AS homepage,
               count(DISTINCT d) AS documents
        ORDER BY documents DESC, name ASC
        """
        with self._driver.session(database=self._db) as session:
            result = session.run(cypher, slug=brand_slug)
            return [dict(record) for record in result]

    def iter_competitor_texts(self, brand_slug: str) -> list[dict[str, object]]:
        """
        Return one row per competitor with all their chunk text concatenated.

        Used by the tagging engine to run regex extraction without a second
        network round-trip per chunk.
        """
        cypher = """
        MATCH (:Brand {slug: $slug})-[:COMPETES_WITH]->(c:Competitor)
        OPTIONAL MATCH (c)-[:HAS_DOCUMENT]->(:Document)-[:HAS_CHUNK]->(ch:Chunk)
        WITH c, collect(ch.text) AS chunks
        RETURN c.domain AS domain, c.name AS name,
               apoc.text.join(chunks, '\\n\\n') AS text,
               size(chunks) AS chunk_count
        """
        # Fall back to a pure-Cypher concat if APOC isn't installed.
        fallback = """
        MATCH (:Brand {slug: $slug})-[:COMPETES_WITH]->(c:Competitor)
        OPTIONAL MATCH (c)-[:HAS_DOCUMENT]->(:Document)-[:HAS_CHUNK]->(ch:Chunk)
        WITH c, collect(ch.text) AS chunks
        RETURN c.domain AS domain, c.name AS name,
               reduce(s = '', t IN chunks | s + t + '\\n\\n') AS text,
               size(chunks) AS chunk_count
        """
        with self._driver.session(database=self._db) as session:
            try:
                result = session.run(cypher, slug=brand_slug)
                return [dict(r) for r in result]
            except Exception:
                result = session.run(fallback, slug=brand_slug)
                return [dict(r) for r in result]

    def tag_counts(
        self,
        brand_slug: str,
        tag_type: str,
        limit: int = 50,
    ) -> list[dict[str, object]]:
        """
        Aggregate a tag type across the brand's competitor set.

        Rows ordered by competitor count (crowded territory at the top,
        whitespace at the bottom).
        """
        spec = TAG_SPECS.get(tag_type)
        if spec is None:
            raise ValueError(f"unknown tag_type: {tag_type}")
        label, rel = spec
        cypher = (
            "MATCH (:Brand {slug: $slug})-[:COMPETES_WITH]->(c:Competitor) "
            f"MATCH (c)-[r:{rel}]->(n:{label}) "
            "RETURN n.name AS name, "
            "count(DISTINCT c) AS competitors, "
            "sum(r.mentions) AS total_mentions, "
            "collect(DISTINCT c.name)[..5] AS example_competitors "
            "ORDER BY competitors DESC, total_mentions DESC "
            "LIMIT $limit"
        )
        with self._driver.session(database=self._db) as session:
            result = session.run(cypher, slug=brand_slug, limit=limit)
            return [dict(r) for r in result]

    def top_shared_keywords(self, brand_slug: str, limit: int = 30) -> list[dict[str, object]]:
        cypher = """
        MATCH (:Brand {slug: $slug})-[:COMPETES_WITH]->(c:Competitor)-[r:TAGGED_WITH]->(k:Keyword)
        RETURN k.term AS term,
               count(DISTINCT c) AS competitors,
               sum(r.score) AS total_score
        ORDER BY competitors DESC, total_score DESC
        LIMIT $limit
        """
        with self._driver.session(database=self._db) as session:
            result = session.run(cypher, slug=brand_slug, limit=limit)
            return [dict(record) for record in result]

    def search_chunks(self, query: str, limit: int = 10) -> list[dict[str, object]]:
        """Full-text search across all Chunk nodes."""
        cypher = """
        CALL db.index.fulltext.queryNodes('chunk_text', $q) YIELD node, score
        MATCH (c:Competitor)-[:HAS_DOCUMENT]->(d:Document)-[:HAS_CHUNK]->(node)
        RETURN c.name AS competitor, c.domain AS domain, d.url AS url,
               node.text AS text, score
        ORDER BY score DESC
        LIMIT $limit
        """
        with self._driver.session(database=self._db) as session:
            result = session.run(cypher, q=query, limit=limit)
            return [dict(record) for record in result]

    # ---- product / price reads ---------------------------------------------

    def document_urls_for_brand(self, brand_slug: str) -> list[dict[str, str]]:
        """Return ``[{competitor_domain, url}]`` for every stored document."""
        cypher = """
        MATCH (:Brand {slug: $slug})-[:COMPETES_WITH]->(c:Competitor)
              -[:HAS_DOCUMENT]->(d:Document)
        RETURN c.domain AS competitor_domain, d.url AS url
        ORDER BY competitor_domain, url
        """
        with self._driver.session(database=self._db) as session:
            result = session.run(cypher, slug=brand_slug)
            return [dict(r) for r in result]

    def latest_prices(self, brand_slug: str, limit: int = 100) -> list[dict[str, object]]:
        """Latest price per (product, retailer) pair for a brand's competitor set."""
        cypher = """
        MATCH (:Brand {slug: $slug})-[:COMPETES_WITH]->(c:Competitor)
              -[:SELLS]->(p:Product)-[:PRICED_AT]->(pp:PricePoint)
              -[:AT_RETAILER]->(r:Retailer)
        WITH c, p, r, pp
        ORDER BY pp.seen_at DESC
        WITH c, p, r, head(collect(pp)) AS latest
        RETURN c.name       AS competitor,
               p.name       AS product,
               p.sku        AS sku,
               p.size       AS size,
               r.domain     AS retailer,
               latest.amount   AS amount,
               latest.currency AS currency,
               latest.seen_at  AS seen_at
        ORDER BY competitor, product, retailer
        LIMIT $limit
        """
        with self._driver.session(database=self._db) as session:
            result = session.run(cypher, slug=brand_slug, limit=limit)
            return [dict(r) for r in result]

    def price_summary(self, brand_slug: str) -> list[dict[str, object]]:
        """Per-competitor price stats (min/avg/max of latest observed prices)."""
        cypher = """
        MATCH (:Brand {slug: $slug})-[:COMPETES_WITH]->(c:Competitor)
              -[:SELLS]->(p:Product)-[:PRICED_AT]->(pp:PricePoint)
        WITH c, p, pp
        ORDER BY pp.seen_at DESC
        WITH c, p, head(collect(pp)) AS latest
        RETURN c.name              AS competitor,
               count(DISTINCT p)   AS products,
               min(latest.amount)  AS min_price,
               avg(latest.amount)  AS avg_price,
               max(latest.amount)  AS max_price,
               latest.currency     AS currency
        ORDER BY avg_price DESC
        """
        with self._driver.session(database=self._db) as session:
            result = session.run(cypher, slug=brand_slug)
            return [dict(r) for r in result]

    def price_history(self, sku: str, days: int = 90) -> list[dict[str, object]]:
        """Chronological price observations for a SKU across all retailers."""
        cypher = """
        MATCH (p:Product {sku: $sku})-[:PRICED_AT]->(pp:PricePoint)-[:AT_RETAILER]->(r:Retailer)
        WHERE pp.seen_at >= datetime() - duration({days: $days})
        RETURN r.domain     AS retailer,
               pp.amount    AS amount,
               pp.currency  AS currency,
               pp.seen_at   AS seen_at
        ORDER BY seen_at ASC, retailer ASC
        """
        with self._driver.session(database=self._db) as session:
            result = session.run(cypher, sku=sku, days=days)
            return [dict(r) for r in result]
