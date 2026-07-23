# brandgraph

Two-step Python pipeline that turns a brand name into a queryable **competitor knowledge graph** in Neo4j — designed to feed downstream brief / audience / media-strategy work.

1. **Discover + scrape**: DuckDuckGo finds competitors → their public pages are fetched (`httpx`) → main content is extracted (`trafilatura`) → text is chunked and keyworded (TF-IDF).
2. **Store as knowledge**: everything lands in Neo4j as a graph you can query later. No LLM in the loop; the graph itself is the reusable knowledge source.

## Graph model

Base (from `ingest`):
```
(:Brand {slug, name})
  -[:COMPETES_WITH]-> (:Competitor {domain, name, homepage, description})
                        -[:HAS_DOCUMENT]-> (:Document {url, title, content_hash})
                                             -[:HAS_CHUNK]-> (:Chunk {id, position, text})
                        -[:TAGGED_WITH {score}]-> (:Keyword {term})
```

Typed layer (from `tag`, adds structured media-analytic entities):
```
(:Competitor) -[:IN_CATEGORY]->    (:Category {name})
              -[:AT_PRICE_TIER]->  (:PriceTier {name})
              -[:OWNED_BY]->       (:ParentCompany {name})
              -[:TARGETS]->        (:Audience {name})
              -[:FOR_OCCASION]->   (:Occasion {name})
              -[:USES_THEME]->     (:Theme {name})
              -[:SPONSORS]->       (:Sponsorship {name})
              -[:MAKES_CLAIM]->    (:Claim {name})
              -[:PRESENT_ON]->     (:Channel {name})
              -[:PRESENT_IN]->     (:Market {name})
```
All typed edges carry a ``mentions`` weight so you can rank confidence.

A full-text index on `Chunk.text` powers ad-hoc retrieval for later brief augmentation.

## Setup

Prereqs: Python 3.11+ and a running local Neo4j.

```bash
cd /Users/um/Documents/neo4j_brief
python -m venv .venv
source .venv/bin/activate
pip install -e .
cp .env.example .env   # already matches: admin / 12345678 @ bolt://localhost:7687
```

If you don't have Neo4j yet, the fastest option is Docker:

```bash
docker run --name neo4j-brandgraph \
  -p 7474:7474 -p 7687:7687 \
  -e NEO4J_AUTH=neo4j/12345678 \
  -d neo4j:5
```

Neo4j Docker only lets you set the password for the built-in `neo4j` user, so either:
- log in once at http://localhost:7474 and `CREATE USER admin SET PASSWORD '12345678' SET PASSWORD CHANGE NOT REQUIRED; GRANT ROLE admin TO admin;`
- **or** just edit `.env` and set `NEO4J_USER=neo4j`.

## Usage

```bash
# 1. Ingest: discover competitors, scrape their sites, load Neo4j
brandgraph ingest "Heineken" --seed-domain heineken.com

# If auto-discovery misses the mark, override it with an explicit list:
brandgraph ingest "Heineken" --seed-domain heineken.com \
  --competitors "ab-inbev.com,carlsberggroup.com,asahigroup-holdings.com,molsoncoors.com,diageo.com,cbrands.com,kirinholdings.com,bostonbeer.com"

# 2. Tag: turn the scraped chunks into typed nodes for media-analytic queries.
#    Adds Theme / Occasion / Sponsorship / Category / Audience / ... nodes.
#    Use --vertical beer for the beer-specific taxonomy overlay.
brandgraph tag "Heineken" --vertical beer

# Explore
brandgraph competitors "Heineken"
brandgraph keywords    "Heineken" -n 40

# Media-analytic views (top rows = crowded territory, bottom = whitespace)
brandgraph tags "Heineken" --type Sponsorship
brandgraph tags "Heineken" --type Theme
brandgraph tags "Heineken" --type Occasion
brandgraph tags "Heineken" --type Channel
brandgraph tag-types                       # list all tag types + samples

# Full-text search across every stored chunk
brandgraph search "sustainability messaging" -n 5

# Wipe and re-ingest
brandgraph ingest "Heineken" --seed-domain heineken.com --reset
```

`brandgraph init-schema` creates all constraints and the full-text index without ingesting.

## Tuning

All tunables live in `.env` (see `.env.example`):

- `MAX_COMPETITORS`, `MAX_PAGES_PER_COMPETITOR` — width/depth of scraping
- `CHUNK_SIZE`, `CHUNK_OVERLAP` — retrieval granularity
- `KEYWORDS_PER_COMPETITOR` — how many TF-IDF terms to tag each competitor with
- `REQUEST_DELAY_SECONDS` — politeness delay between page fetches

## Layout

```
src/brandgraph/
  cli.py         # Typer entry points
  pipeline.py    # discover → scrape → chunk → keyword → write
  search.py      # DuckDuckGo competitor + on-domain page discovery
  scraper.py     # httpx + trafilatura, with retries
  chunker.py     # paragraph-aware overlapping chunks
  keywords.py    # TF-IDF keyword extraction
  taxonomies.py  # dictionary-based tag engine + generic/beer taxonomies
  graph.py       # Neo4j schema + upserts + query helpers
  models.py      # Pydantic data models
  config.py      # env-driven settings
  utils.py       # slug / domain / hash helpers
```

## Adding a new vertical

Verticals are just dictionaries in [src/brandgraph/taxonomies.py](src/brandgraph/taxonomies.py). Copy the `BEER` block, rename it (e.g. `AUTOMOTIVE`), fill in the tag values and regex patterns, add it to the `VERTICALS` mapping, then run `brandgraph tag <brand> --vertical automotive`. No schema change needed — every tag type in `TAG_SPECS` is already wired end-to-end.

# Usage : 
brandgraph ingest "Heineken" --seed-domain heineken.com --reset
brandgraph competitors "Heineken"
brandgraph tag "Heineken" --vertical beer
brandgraph tags "Heineken" --type Sponsorship
brandgraph viz "Heineken" -o heineken.html --open

# example 2 :
brandgraph ingest "Nike" --seed-domain nike.com --reset
brandgraph competitors "Nike"
## Next steps (augmenting a brief)

Everything you need for a brief-augmentation layer is already in the graph:

- `GraphStore.top_shared_keywords(brand_slug)` — themes the competitor set converges on.
- `GraphStore.search_chunks(query)` — retrieve passages from any competitor about a specific topic (positioning, audience, price, sustainability, …).
- Add your own Cypher for whitespace analysis, e.g. keywords *no* competitor tags.

That layer is intentionally not built here for later