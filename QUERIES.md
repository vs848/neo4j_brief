# Brandgraph — analytic Cypher queries

Ready-to-run Cypher for the brandgraph model. Paste any block into Neo4j
Browser (Neo4j Desktop → your database → **Open**).

All queries are scoped to a single brand via `{slug: 'heineken'}` — swap in
`'nike'` (or any other brand slug) to switch context. `:Keyword`, `:Theme`,
`:Sponsorship`, etc. are shared across brands intentionally, so **always
filter by `:Brand`** when you want a per-brand view.

Graph model recap:

```
(:Brand {slug, name})
  -[:COMPETES_WITH]-> (:Competitor {domain, name, homepage, description})
                        -[:HAS_DOCUMENT]-> (:Document {url, title, content_hash})
                                             -[:HAS_CHUNK]-> (:Chunk {id, position, text})
                        -[:TAGGED_WITH {score}]-> (:Keyword {term})
                        -[:IN_CATEGORY | AT_PRICE_TIER | OWNED_BY | TARGETS |
                           FOR_OCCASION | USES_THEME | SPONSORS | MAKES_CLAIM |
                           PRESENT_ON | PRESENT_IN {mentions}]-> (:<TypedTag> {name})
```

---

## 1. Positioning map — compound tag intersections

*Who competes in the same Category × PriceTier × Audience cell as us?* Direct
vs. adjacent competitor split in a single query.

```cypher
MATCH (:Brand {slug:'heineken'})-[:COMPETES_WITH]->(c:Competitor)
      -[:IN_CATEGORY]->(cat:Category),
      (c)-[:AT_PRICE_TIER]->(pt:PriceTier),
      (c)-[:TARGETS]->(a:Audience)
RETURN cat.name AS category,
       pt.name  AS price_tier,
       a.name   AS audience,
       collect(DISTINCT c.name) AS competitors
ORDER BY size(competitors) DESC;
```

## 2. Whitespace — themes owned by exactly one competitor

Tags with 0–1 competitor mentions = uncontested territory. Highest-value
output for briefs.

```cypher
MATCH (:Brand {slug:'heineken'})-[:COMPETES_WITH]->(c:Competitor)-[:USES_THEME]->(t:Theme)
WITH t, collect(DISTINCT c.name) AS owners
WHERE size(owners) = 1
RETURN t.name AS signature_theme,
       owners[0] AS owned_by
ORDER BY signature_theme;
```

## 3. Table stakes — claims *every* competitor makes

The inverse of #2: must-haves that won't differentiate.

```cypher
MATCH (:Brand {slug:'heineken'})-[:COMPETES_WITH]->(c:Competitor)
WITH count(DISTINCT c) AS total
MATCH (:Brand {slug:'heineken'})-[:COMPETES_WITH]->(c2:Competitor)-[:MAKES_CLAIM]->(cl:Claim)
WITH cl, count(DISTINCT c2) AS n, total
WHERE n = total
RETURN cl.name AS table_stakes_claim;
```

## 4. Audience contention — where you'll fight for share of voice

```cypher
MATCH (:Brand {slug:'heineken'})-[:COMPETES_WITH]->(c:Competitor)-[:TARGETS]->(a:Audience)
RETURN a.name AS audience,
       count(DISTINCT c) AS competitors_targeting,
       collect(DISTINCT c.name)[..5] AS examples
ORDER BY competitors_targeting DESC;
```

## 5. Media footprint — Channels & Sponsorships

Direct input for a media plan: where competitors actually show up.

```cypher
MATCH (:Brand {slug:'heineken'})-[:COMPETES_WITH]->(c:Competitor)-[:PRESENT_ON]->(ch:Channel)
RETURN ch.name AS channel,
       count(DISTINCT c) AS competitors_present,
       collect(DISTINCT c.name) AS who
ORDER BY competitors_present DESC;
```

```cypher
MATCH (:Brand {slug:'heineken'})-[:COMPETES_WITH]->(c:Competitor)-[:SPONSORS]->(s:Sponsorship)
RETURN s.name AS sponsorship,
       count(DISTINCT c) AS competitors_sponsoring,
       collect(DISTINCT c.name) AS who
ORDER BY competitors_sponsoring DESC;
```

## 6. Portfolio / ownership view

`:ParentCompany` links reveal that four "different" competitors may roll up
to one holding company.

```cypher
MATCH (:Brand {slug:'heineken'})-[:COMPETES_WITH]->(c:Competitor)-[:OWNED_BY]->(p:ParentCompany)
RETURN p.name AS parent,
       collect(c.name) AS brands
ORDER BY size(brands) DESC;
```

## 7. Compound "give me competitors that…" filters

Trivial in a graph, painful anywhere else.

```cypher
// Competitors targeting Gen Z, sponsoring football, and claiming sustainability
MATCH (:Brand {slug:'heineken'})-[:COMPETES_WITH]->(c:Competitor)
      -[:TARGETS]->(:Audience {name:'gen z'})
WHERE (c)-[:SPONSORS]->(:Sponsorship {name:'football'})
  AND (c)-[:MAKES_CLAIM]->(:Claim {name:'sustainability'})
RETURN c.name, c.domain;
```

## 8. Evidence retrieval — actual sentences on any topic

Full-text index over `Chunk.text` lets you pull the passages a competitor
wrote about any topic. This is your quotable evidence layer for briefs.

```cypher
CALL db.index.fulltext.queryNodes('chunk_text', 'gen z OR "younger drinkers"') YIELD node, score
MATCH (:Brand {slug:'heineken'})-[:COMPETES_WITH]->(c:Competitor)
      -[:HAS_DOCUMENT]->(d:Document)-[:HAS_CHUNK]->(node)
RETURN c.name    AS competitor,
       d.url     AS url,
       substring(node.text, 0, 220) AS snippet,
       score
ORDER BY score DESC
LIMIT 10;
```

## 9. Keyword co-occurrence — topical territories

Which TF-IDF keywords appear together across competitors → conversation
clusters.

```cypher
MATCH (:Brand {slug:'heineken'})-[:COMPETES_WITH]->(c:Competitor)
      -[:TAGGED_WITH]->(k1:Keyword),
      (c)-[:TAGGED_WITH]->(k2:Keyword)
WHERE k1.term < k2.term
RETURN k1.term AS term_a,
       k2.term AS term_b,
       count(DISTINCT c) AS co_occurrences
ORDER BY co_occurrences DESC
LIMIT 20;
```

## 10. Cross-brand / cross-vertical themes

Because tag nodes are shared, you can see themes that transcend a single
vertical (e.g. "sustainability" showing up in both beer *and* sportswear).

```cypher
MATCH (b:Brand)-[:COMPETES_WITH]->(:Competitor)-[:USES_THEME]->(t:Theme)
WITH t, collect(DISTINCT b.slug) AS brands
WHERE size(brands) > 1
RETURN t.name AS theme, brands
ORDER BY size(brands) DESC, theme;
```

---

## Price-tier analysis

> `:PriceTier` is a **positioning tier**, not a currency value — it comes
> from regex matches on words like `premium`, `mainstream`, `craft`,
> `super-premium / luxury / prestige` in scraped chunks
> (see [src/brandgraph/taxonomies.py](src/brandgraph/taxonomies.py)).
> `mentions` on `:AT_PRICE_TIER` = how loudly a competitor positions itself
> in that tier, not a dollar amount.

### 11. Price-tier landscape for one brand

```cypher
MATCH (:Brand {slug:'heineken'})-[:COMPETES_WITH]->(c:Competitor)
      -[r:AT_PRICE_TIER]->(pt:PriceTier)
RETURN pt.name AS price_tier,
       count(DISTINCT c)        AS competitors,
       sum(r.mentions)          AS total_mentions,
       collect(DISTINCT c.name) AS who
ORDER BY competitors DESC, total_mentions DESC;
```

### 12. Each competitor's primary (loudest) tier

```cypher
MATCH (:Brand {slug:'heineken'})-[:COMPETES_WITH]->(c:Competitor)
      -[r:AT_PRICE_TIER]->(pt:PriceTier)
WITH c, pt, r.mentions AS m
ORDER BY c.name, m DESC
WITH c, collect({tier: pt.name, mentions: m}) AS tiers
RETURN c.name AS competitor,
       tiers[0].tier     AS primary_tier,
       tiers[0].mentions AS primary_mentions,
       tiers             AS all_tiers;
```

### 13. Price-tier × Category positioning matrix

```cypher
MATCH (:Brand {slug:'heineken'})-[:COMPETES_WITH]->(c:Competitor)
      -[:AT_PRICE_TIER]->(pt:PriceTier),
      (c)-[:IN_CATEGORY]->(cat:Category)
RETURN cat.name AS category,
       pt.name  AS price_tier,
       count(DISTINCT c)        AS competitors,
       collect(DISTINCT c.name) AS who
ORDER BY category, competitors DESC;
```

### 14. Whitespace — tiers nobody in the set is playing in

```cypher
MATCH (pt:PriceTier)
WHERE NOT EXISTS {
  MATCH (:Brand {slug:'heineken'})-[:COMPETES_WITH]->(:Competitor)-[:AT_PRICE_TIER]->(pt)
}
RETURN pt.name AS unoccupied_tier;
```

### 15. Direct price competitors (same tier as your brand's dominant tier)

Swap `'premium'` for whichever tier you want to inspect.

```cypher
MATCH (:Brand {slug:'heineken'})-[:COMPETES_WITH]->(c:Competitor)
      -[r:AT_PRICE_TIER]->(:PriceTier {name:'premium'})
RETURN c.name AS competitor,
       c.domain,
       r.mentions AS premium_signal
ORDER BY premium_signal DESC;
```

### 16. Evidence — actual sentences a competitor uses to claim a tier

```cypher
MATCH (c:Competitor {domain:'carlsberg.com'})
      -[:HAS_DOCUMENT]->(d:Document)-[:HAS_CHUNK]->(ch:Chunk)
WHERE toLower(ch.text) =~ '.*\\b(super[- ]?premium|luxury|prestige)\\b.*'
RETURN d.url,
       substring(ch.text, 0, 220) AS snippet
LIMIT 10;
```

---

## Product & PricePoint (real prices)

Populated by `brandgraph prices <brand>`, which parses `schema.org` `Product`
markup out of every stored page and appends a `:PricePoint` observation per
scrape.

### 17. Latest price per product / retailer, for one brand

```cypher
MATCH (:Brand {slug:'heineken'})-[:COMPETES_WITH]->(c:Competitor)
      -[:SELLS]->(p:Product)-[:PRICED_AT]->(pp:PricePoint)-[:AT_RETAILER]->(r:Retailer)
WITH c, p, r, pp
ORDER BY pp.seen_at DESC
WITH c, p, r, head(collect(pp)) AS latest
RETURN c.name       AS competitor,
       p.name       AS product,
       p.sku        AS sku,
       r.domain     AS retailer,
       latest.amount   AS amount,
       latest.currency AS currency,
       latest.seen_at  AS seen_at
ORDER BY competitor, product, retailer;
```

### 18. Price band per category (latest observations only)

```cypher
MATCH (:Brand {slug:'heineken'})-[:COMPETES_WITH]->(c:Competitor)
      -[:SELLS]->(p:Product)-[:PRICED_AT]->(pp:PricePoint)
WITH c, p, pp ORDER BY pp.seen_at DESC
WITH c, p, head(collect(pp)) AS latest
MATCH (c)-[:IN_CATEGORY]->(cat:Category)
RETURN cat.name AS category,
       min(latest.amount) AS min_price,
       avg(latest.amount) AS avg_price,
       max(latest.amount) AS max_price,
       latest.currency    AS currency,
       count(DISTINCT p)  AS products
ORDER BY avg_price DESC;
```

### 19. Cheapest retailer for a specific SKU right now

```cypher
MATCH (p:Product {sku:'HNK-330-6PK'})-[:PRICED_AT]->(pp:PricePoint)-[:AT_RETAILER]->(r:Retailer)
WITH r, pp ORDER BY pp.seen_at DESC
WITH r, head(collect(pp)) AS latest
RETURN r.name        AS retailer,
       latest.amount AS amount,
       latest.currency,
       latest.seen_at
ORDER BY latest.amount ASC;
```

### 20. Price movement over the last 90 days per competitor

```cypher
MATCH (c:Competitor)-[:SELLS]->(p:Product)-[:PRICED_AT]->(pp:PricePoint)
WHERE pp.seen_at >= datetime() - duration('P90D')
RETURN c.name  AS competitor,
       p.name  AS product,
       min(pp.amount) AS low,
       max(pp.amount) AS high,
       max(pp.amount) - min(pp.amount) AS spread,
       count(pp) AS observations
ORDER BY spread DESC
LIMIT 20;
```

### 21. Positioning tier vs. actual median price (sanity-check the taxonomy)

Where the tagger says "premium" but real prices are cheap → the taxonomy needs
tuning, or the competitor is using the word aspirationally.

```cypher
MATCH (:Brand {slug:'heineken'})-[:COMPETES_WITH]->(c:Competitor)
      -[:AT_PRICE_TIER]->(pt:PriceTier),
      (c)-[:SELLS]->(:Product)-[:PRICED_AT]->(pp:PricePoint)
WITH pt.name AS tier, pp.amount AS price
RETURN tier,
       percentileCont(price, 0.5) AS median_price,
       count(price) AS observations
ORDER BY median_price;
```

---

## Bonus — supporting queries

### Seed a tiny sample (so queries return rows before ingest)

```cypher
MERGE (b:Brand {slug: 'heineken'}) SET b.name = 'Heineken'
MERGE (c:Competitor {domain: 'carlsberg.com'})
  SET c.name = 'Carlsberg',
      c.homepage = 'https://www.carlsberg.com',
      c.description = 'Danish multinational brewer.'
MERGE (b)-[:COMPETES_WITH]->(c)
MERGE (d:Document {url: 'https://www.carlsberg.com/about'})
  SET d.title = 'About Carlsberg', d.content_hash = 'abc123'
MERGE (c)-[:HAS_DOCUMENT]->(d)
MERGE (ch:Chunk {id: 'abc123::0'})
  SET ch.position = 0,
      ch.text = 'Carlsberg Group is a Danish multinational brewer founded in 1847.'
MERGE (d)-[:HAS_CHUNK]->(ch)
MERGE (k:Keyword {term: 'lager'})
MERGE (c)-[r:TAGGED_WITH]->(k) SET r.score = 0.87;
```

### Top competitors for a brand, with their keyword tags

```cypher
MATCH (b:Brand {slug: 'heineken'})-[:COMPETES_WITH]->(c:Competitor)
OPTIONAL MATCH (c)-[t:TAGGED_WITH]->(k:Keyword)
RETURN c.name   AS competitor,
       c.domain AS domain,
       collect({term: k.term, score: t.score}) AS keywords
ORDER BY competitor;
```

### Full path from a brand down to chunks (great for the graph view)

```cypher
MATCH p = (b:Brand {slug: 'heineken'})
          -[:COMPETES_WITH]->(:Competitor)
          -[:HAS_DOCUMENT]->(:Document)
          -[:HAS_CHUNK]->(:Chunk)
RETURN p
LIMIT 25;
```

### Per-brand housekeeping counts

```cypher
MATCH (b:Brand {slug: 'heineken'})
OPTIONAL MATCH (b)-[:COMPETES_WITH]->(c:Competitor)
OPTIONAL MATCH (c)-[:HAS_DOCUMENT]->(d:Document)
OPTIONAL MATCH (d)-[:HAS_CHUNK]->(ch:Chunk)
OPTIONAL MATCH (c)-[:TAGGED_WITH]->(k:Keyword)
RETURN count(DISTINCT b)  AS brands,
       count(DISTINCT c)  AS competitors,
       count(DISTINCT d)  AS documents,
       count(DISTINCT ch) AS chunks,
       count(DISTINCT k)  AS keywords;
```

> Tip: in Neo4j Browser, run the *full-path* query then click any node and
> hit the graph icon to expand — you'll see the full
> `Brand → Competitor → Document → Chunk` chain plus the `Keyword` /
> `:Theme` / `:Sponsorship` tags branching off.
