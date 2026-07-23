"""Typer-based CLI for the brandgraph pipeline."""
from __future__ import annotations

import logging
import webbrowser
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from .graph import GraphStore
from .pipeline import run_ingest
from .price_scraper import PriceScraper
from .taxonomies import TAG_SPECS, TaggingEngine, build_taxonomy
from .utils import slugify
from .visualize import render_html

app = typer.Typer(
    add_completion=False,
    help="Discover competitors for a brand and build a Neo4j knowledge graph.",
)
console = Console()


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )


@app.command()
def ingest(
    brand: str = typer.Argument(..., help="Brand name, e.g. 'Nike'."),
    seed_domain: Optional[str] = typer.Option(
        None, "--seed-domain", help="The brand's own root domain, e.g. nike.com (to filter it out)."
    ),
    competitors: Optional[str] = typer.Option(
        None,
        "--competitors",
        help=(
            "Comma-separated list of competitor domains to use instead of auto-discovery, "
            "e.g. 'carlsberg.com,corona.com,guinness.com'. Bypasses DuckDuckGo entirely."
        ),
    ),
    reset: bool = typer.Option(False, "--reset", help="Delete this brand's existing subgraph first."),
    verbose: bool = typer.Option(False, "-v", "--verbose"),
) -> None:
    """Run steps 1 + 2: scrape competitor data and write it to Neo4j."""
    _setup_logging(verbose)
    domain_list = [d.strip() for d in competitors.split(",")] if competitors else None
    stats = run_ingest(
        brand,
        seed_domain=seed_domain,
        reset=reset,
        competitor_domains=domain_list,
    )

    console.print(f"[bold green]Ingest complete for[/] {brand}")
    console.print(
        f"competitors: {stats.competitors}  documents: {stats.documents}  "
        f"chunks: {stats.chunks}  keywords: {stats.keywords}  failed: {len(stats.failed)}"
    )


@app.command("competitors")
def list_competitors(
    brand: str = typer.Argument(..., help="Brand name to inspect."),
) -> None:
    """Show competitors currently stored in the graph for ``brand``."""
    slug = slugify(brand)
    with GraphStore() as store:
        rows = store.list_competitors(slug)

    if not rows:
        console.print(f"[yellow]No competitors found for '{brand}'. Did you run `ingest`?[/]")
        raise typer.Exit(code=1)

    table = Table(title=f"Competitors of {brand}")
    table.add_column("Name")
    table.add_column("Domain")
    table.add_column("Docs", justify="right")
    for r in rows:
        table.add_row(str(r["name"]), str(r["domain"]), str(r["documents"]))
    console.print(table)


@app.command("keywords")
def top_keywords_cmd(
    brand: str = typer.Argument(..., help="Brand name to summarise."),
    limit: int = typer.Option(30, "--limit", "-n"),
) -> None:
    """Show keywords shared across the brand's competitor set."""
    slug = slugify(brand)
    with GraphStore() as store:
        rows = store.top_shared_keywords(slug, limit=limit)

    if not rows:
        console.print(f"[yellow]No keywords found for '{brand}'.[/]")
        raise typer.Exit(code=1)

    table = Table(title=f"Top shared keywords for {brand}")
    table.add_column("Term")
    table.add_column("# competitors", justify="right")
    table.add_column("Total score", justify="right")
    for r in rows:
        table.add_row(str(r["term"]), str(r["competitors"]), f"{float(r['total_score']):.2f}")
    console.print(table)


@app.command()
def search(
    query: str = typer.Argument(..., help="Free-text query, e.g. 'sustainability messaging'."),
    limit: int = typer.Option(10, "--limit", "-n"),
) -> None:
    """Full-text search across every stored competitor chunk."""
    with GraphStore() as store:
        rows = store.search_chunks(query, limit=limit)

    if not rows:
        console.print("[yellow]No matches.[/]")
        raise typer.Exit(code=1)

    for r in rows:
        console.rule(f"[bold]{r['competitor']}[/]  ({r['domain']})  score={float(r['score']):.2f}")
        console.print(f"[dim]{r['url']}[/]")
        console.print(str(r["text"]))


@app.command()
def tag(
    brand: str = typer.Argument(..., help="Brand name to tag (must already be ingested)."),
    vertical: str = typer.Option(
        "generic",
        "--vertical",
        help="Taxonomy overlay to apply on top of the generic dictionary. Currently: generic, beer.",
    ),
    verbose: bool = typer.Option(False, "-v", "--verbose"),
) -> None:
    """
    Walk every competitor's chunks and materialise typed nodes.

    Adds ``(:Theme)``, ``(:Occasion)``, ``(:Sponsorship)``, ``(:Category)``,
    ``(:PriceTier)``, ``(:ParentCompany)``, ``(:Audience)``, ``(:Channel)``,
    ``(:Claim)`` and ``(:Market)`` nodes with ``mentions``-weighted edges
    from each competitor. No re-scraping; pure regex over stored text.
    """
    _setup_logging(verbose)
    engine = TaggingEngine(vertical=vertical)
    slug = slugify(brand)

    total_pairs = 0
    with GraphStore() as store:
        store.ensure_schema()
        rows = store.iter_competitor_texts(slug)
        if not rows:
            console.print(f"[yellow]No competitors found for '{brand}'. Run `ingest` first.[/]")
            raise typer.Exit(code=1)

        for row in rows:
            text = str(row.get("text") or "")
            domain = str(row["domain"])
            hits = engine.extract(text)
            pair_count = sum(len(v) for v in hits.counts.values())
            written = store.upsert_tags(domain, hits)
            total_pairs += written
            console.print(
                f"[cyan]{row['name']}[/]  ({domain})  chunks={row['chunk_count']}  "
                f"tags={pair_count}  written={written}"
            )

    console.print(
        f"[bold green]Tagging complete[/]  vertical={vertical}  "
        f"total tag pairs written: {total_pairs}"
    )


@app.command()
def tags(
    brand: str = typer.Argument(..., help="Brand name to summarise."),
    type: str = typer.Option(  # noqa: A002 - matches CLI UX
        "Theme",
        "--type",
        "-t",
        help=f"Tag type to aggregate. One of: {', '.join(TAG_SPECS)}.",
    ),
    limit: int = typer.Option(30, "--limit", "-n"),
) -> None:
    """
    Aggregate a tag type across a brand's competitor set.

    Top rows = crowded territory (many competitors use the same tag).
    Bottom rows = whitespace signals (only 1-2 competitors use it).
    """
    slug = slugify(brand)
    with GraphStore() as store:
        try:
            rows = store.tag_counts(slug, tag_type=type, limit=limit)
        except ValueError as e:
            console.print(f"[red]{e}[/]")
            raise typer.Exit(code=2) from None

    if not rows:
        console.print(
            f"[yellow]No '{type}' tags found for '{brand}'. "
            "Did you run `brandgraph tag`?[/]"
        )
        raise typer.Exit(code=1)

    table = Table(title=f"{type} landscape for {brand}")
    table.add_column(type)
    table.add_column("# competitors", justify="right")
    table.add_column("Total mentions", justify="right")
    table.add_column("Examples")
    for r in rows:
        examples = ", ".join(str(x) for x in (r.get("example_competitors") or []))
        table.add_row(
            str(r["name"]),
            str(r["competitors"]),
            str(r["total_mentions"]),
            examples,
        )
    console.print(table)


@app.command("tag-types")
def tag_types() -> None:
    """List every supported tag type + a couple of taxonomy entries per type."""
    tax = build_taxonomy("beer")  # richest sample
    table = Table(title="Tag types (with sample values)")
    table.add_column("Type")
    table.add_column("Node label")
    table.add_column("Relationship")
    table.add_column("Sample values")
    for tag_type, (label, rel) in TAG_SPECS.items():
        sample = list(tax.get(tag_type, {}).keys())[:4]
        table.add_row(tag_type, label, rel, ", ".join(sample) or "—")
    console.print(table)


@app.command("init-schema")
def init_schema() -> None:
    """Create Neo4j constraints and full-text index without ingesting anything."""
    with GraphStore() as store:
        store.ensure_schema()
    console.print("[green]Schema ensured.[/]")


@app.command()
def prices(
    brand: str = typer.Argument(..., help="Brand name (must already be ingested)."),
    extra_urls: Optional[str] = typer.Option(
        None,
        "--extra-urls",
        help=(
            "Comma-separated additional URLs to scrape for prices (e.g. retailer "
            "product pages). Useful when competitors don't publish JSON-LD on "
            "their own domain."
        ),
    ),
    verbose: bool = typer.Option(False, "-v", "--verbose"),
) -> None:
    """
    Extract ``schema.org`` ``Product`` markup from every stored document for
    ``brand`` and attach ``(:Product)-[:PRICED_AT]->(:PricePoint)`` chains.

    Re-runs are safe: existing products are updated, price observations are
    appended (so you get history over time).
    """
    _setup_logging(verbose)
    slug = slugify(brand)

    total_products = 0
    total_price_points = 0
    pages_scanned = 0
    extra = [u.strip() for u in extra_urls.split(",")] if extra_urls else []

    with GraphStore() as store:
        store.ensure_schema()
        doc_rows = store.document_urls_for_brand(slug)
        if not doc_rows and not extra:
            console.print(
                f"[yellow]No documents found for '{brand}'. Run `ingest` first, "
                "or pass --extra-urls.[/]"
            )
            raise typer.Exit(code=1)

        with PriceScraper() as scraper:
            # 1. re-scan every stored competitor document for JSON-LD Product data
            for row in doc_rows:
                url = str(row["url"])
                competitor_domain = str(row["competitor_domain"])
                pairs = scraper.extract(url, competitor_domain=competitor_domain)
                pages_scanned += 1
                if not pairs:
                    continue
                seen_products: set[str] = set()
                for product, price_point in pairs:
                    if product.id not in seen_products:
                        store.upsert_product(product)
                        seen_products.add(product.id)
                        total_products += 1
                    store.upsert_price_point(price_point)
                    total_price_points += 1
                console.print(
                    f"[cyan]{competitor_domain}[/]  {url}  "
                    f"products={len(seen_products)} prices={len(pairs)}"
                )

            # 2. optional extra URLs — the caller decides which competitor they map to
            #    by prefixing the URL with 'domain=' (e.g. 'carlsberg.com=https://...').
            for entry in extra:
                if "=" not in entry:
                    console.print(
                        f"[red]--extra-urls entry '{entry}' must be "
                        "'competitor_domain=url'; skipping.[/]"
                    )
                    continue
                competitor_domain, url = entry.split("=", 1)
                pairs = scraper.extract(url.strip(), competitor_domain=competitor_domain.strip())
                pages_scanned += 1
                seen_products = set()
                for product, price_point in pairs:
                    if product.id not in seen_products:
                        store.upsert_product(product)
                        seen_products.add(product.id)
                        total_products += 1
                    store.upsert_price_point(price_point)
                    total_price_points += 1
                console.print(
                    f"[cyan]{competitor_domain}[/]  {url}  "
                    f"products={len(seen_products)} prices={len(pairs)}"
                )

    console.print(
        f"[bold green]Prices ingested[/]  pages={pages_scanned}  "
        f"products={total_products}  price_points={total_price_points}"
    )


@app.command("price-summary")
def price_summary_cmd(
    brand: str = typer.Argument(..., help="Brand name to summarise prices for."),
) -> None:
    """Per-competitor min/avg/max of latest observed prices."""
    slug = slugify(brand)
    with GraphStore() as store:
        rows = store.price_summary(slug)

    if not rows:
        console.print(
            f"[yellow]No prices found for '{brand}'. Run `brandgraph prices` first.[/]"
        )
        raise typer.Exit(code=1)

    table = Table(title=f"Latest-price summary for {brand}")
    table.add_column("Competitor")
    table.add_column("Products", justify="right")
    table.add_column("Min", justify="right")
    table.add_column("Avg", justify="right")
    table.add_column("Max", justify="right")
    table.add_column("Ccy")
    for r in rows:
        table.add_row(
            str(r["competitor"]),
            str(r["products"]),
            f"{float(r['min_price']):.2f}",
            f"{float(r['avg_price']):.2f}",
            f"{float(r['max_price']):.2f}",
            str(r["currency"] or ""),
        )
    console.print(table)


@app.command("price-history")
def price_history_cmd(
    sku: str = typer.Argument(..., help="Product SKU to trace."),
    days: int = typer.Option(90, "--days", help="Look-back window in days."),
) -> None:
    """Show every price observation for a SKU across all retailers."""
    with GraphStore() as store:
        rows = store.price_history(sku, days=days)

    if not rows:
        console.print(f"[yellow]No price observations for SKU '{sku}' in the last {days} days.[/]")
        raise typer.Exit(code=1)

    table = Table(title=f"Price history for {sku} (last {days}d)")
    table.add_column("Seen at")
    table.add_column("Retailer")
    table.add_column("Amount", justify="right")
    table.add_column("Ccy")
    for r in rows:
        table.add_row(
            str(r["seen_at"]),
            str(r["retailer"]),
            f"{float(r['amount']):.2f}",
            str(r["currency"] or ""),
        )
    console.print(table)


@app.command()
def viz(
    brand: str = typer.Argument(..., help="Brand name to visualise."),
    output: Path = typer.Option(
        Path("brandgraph.html"),
        "--output",
        "-o",
        help="Where to write the standalone HTML file.",
    ),
    include_docs: bool = typer.Option(
        False,
        "--include-docs/--no-docs",
        help="Include :Document nodes in the visualisation.",
    ),
    include_chunks: bool = typer.Option(
        False,
        "--include-chunks/--no-chunks",
        help="Include :Chunk nodes (implies --include-docs; can be many).",
    ),
    physics: bool = typer.Option(
        True,
        "--physics/--no-physics",
        help="Enable the force-directed layout animation.",
    ),
    open_browser: bool = typer.Option(
        False,
        "--open",
        help="Open the resulting HTML file in the default browser after writing.",
    ),
) -> None:
    """
    Export the brand's subgraph as a self-contained interactive HTML file.

    Nodes are coloured/shaped by label; hover for full properties. Uses pyvis
    (vis.js under the hood) — the file has no external dependencies once
    written and works fully offline.
    """
    slug = slugify(brand)
    try:
        path = render_html(
            slug,
            output_path=output,
            include_docs=include_docs or include_chunks,
            include_chunks=include_chunks,
            physics=physics,
        )
    except RuntimeError as e:
        console.print(f"[red]{e}[/]")
        raise typer.Exit(code=1) from None

    console.print(f"[green]Wrote[/] {path}")
    if open_browser:
        webbrowser.open(path.as_uri())


if __name__ == "__main__":  # pragma: no cover
    app()
