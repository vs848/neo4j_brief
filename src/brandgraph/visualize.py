"""Export a brand's Neo4j subgraph to a self-contained interactive HTML file.

Uses `pyvis` (which wraps vis.js) to render nodes coloured by label and edges
labelled by relationship type, with the full property dictionary shown on hover.
The generated file is standalone — no server, just open it in a browser.
"""
from __future__ import annotations

import html
import logging
from pathlib import Path
from typing import Any

from pyvis.network import Network

from .graph import GraphStore
from .taxonomies import TAG_SPECS

log = logging.getLogger(__name__)


# Colour + size per node label. Anything unknown falls back to gray.
NODE_STYLE: dict[str, dict[str, Any]] = {
    "Brand":         {"color": "#e63946", "size": 40, "shape": "star"},
    "Competitor":    {"color": "#1d3557", "size": 26, "shape": "dot"},
    "Document":      {"color": "#8d99ae", "size": 12, "shape": "square"},
    "Chunk":         {"color": "#adb5bd", "size":  8, "shape": "square"},
    "Keyword":       {"color": "#2a9d8f", "size": 14, "shape": "triangle"},
    "Theme":         {"color": "#9d4edd", "size": 18, "shape": "hexagon"},
    "Sponsorship":   {"color": "#f77f00", "size": 18, "shape": "diamond"},
    "Occasion":      {"color": "#38b000", "size": 18, "shape": "hexagon"},
    "Category":      {"color": "#0077b6", "size": 20, "shape": "box"},
    "PriceTier":     {"color": "#ff70a6", "size": 18, "shape": "box"},
    "ParentCompany": {"color": "#7f5539", "size": 22, "shape": "box"},
    "Audience":      {"color": "#00b4d8", "size": 18, "shape": "ellipse"},
    "Channel":       {"color": "#ffd60a", "size": 18, "shape": "triangleDown"},
    "Claim":         {"color": "#d62828", "size": 18, "shape": "hexagon"},
    "Market":        {"color": "#606c38", "size": 18, "shape": "ellipse"},
}
_DEFAULT_STYLE = {"color": "#adb5bd", "size": 14, "shape": "dot"}

# Which property to use as the visible node caption, in order of preference.
LABEL_KEYS: tuple[str, ...] = ("name", "term", "title", "domain", "slug", "url", "id")


def _primary_label(labels: list[str]) -> str:
    for lbl in labels:
        if lbl in NODE_STYLE:
            return lbl
    return labels[0] if labels else "Unknown"


def _display_label(props: dict[str, Any]) -> str:
    for key in LABEL_KEYS:
        val = props.get(key)
        if val:
            s = str(val)
            return s if len(s) <= 40 else s[:37] + "…"
    return "?"


def _tooltip(labels: list[str], props: dict[str, Any]) -> str:
    lines = [f"<b>{':'.join(labels)}</b>"]
    for k, v in props.items():
        if k == "text":  # chunks: truncate heavily
            v = str(v)
            v = v if len(v) <= 200 else v[:200] + "…"
        lines.append(f"<i>{html.escape(k)}</i>: {html.escape(str(v))}")
    return "<br>".join(lines)


def _edge_tooltip(rel_type: str, props: dict[str, Any]) -> str:
    if not props:
        return rel_type
    body = "<br>".join(f"<i>{html.escape(k)}</i>: {html.escape(str(v))}" for k, v in props.items())
    return f"<b>{rel_type}</b><br>{body}"


def _rel_types_for_scope(include_docs: bool, include_chunks: bool) -> list[str]:
    types = [rel for _label, rel in TAG_SPECS.values()]
    types.append("TAGGED_WITH")            # competitor -> keyword
    if include_docs:
        types.append("HAS_DOCUMENT")
    if include_chunks:
        types.append("HAS_DOCUMENT")       # chunks live under documents
        types.append("HAS_CHUNK")
    # dedupe while preserving order
    seen: set[str] = set()
    unique: list[str] = []
    for t in types:
        if t not in seen:
            seen.add(t)
            unique.append(t)
    return unique


def _build_subgraph_query(rel_types: list[str]) -> str:
    """
    Return a Cypher query that emits the brand's subgraph as node / rel dicts.

    Relationship types are compiled into the query text (not parameters) because
    Cypher doesn't accept parametrised rel types. All values come from our own
    ``TAG_SPECS`` — never from user input — so it's safe.
    """
    rel_filter = "|".join(rel_types)
    return f"""
    MATCH (b:Brand {{slug: $slug}})
    OPTIONAL MATCH (b)-[bc:COMPETES_WITH]->(c:Competitor)
    OPTIONAL MATCH (c)-[cr:{rel_filter}]->(o)
    WITH collect(DISTINCT b)  AS brands,
         collect(DISTINCT c)  AS competitors,
         collect(DISTINCT o)  AS others,
         collect(DISTINCT bc) AS brand_rels,
         collect(DISTINCT cr) AS competitor_rels
    WITH brands + competitors + others AS all_nodes,
         brand_rels + competitor_rels  AS all_rels
    RETURN
      [n IN all_nodes WHERE n IS NOT NULL |
        {{id: elementId(n), labels: labels(n), props: properties(n)}}] AS nodes,
      [r IN all_rels WHERE r IS NOT NULL |
        {{source: elementId(startNode(r)),
          target: elementId(endNode(r)),
          type: type(r),
          props: properties(r)}}] AS relationships
    """


def fetch_subgraph(
    brand_slug: str,
    include_docs: bool = False,
    include_chunks: bool = False,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Pull nodes + relationships for a brand's subgraph in a single round-trip."""
    rel_types = _rel_types_for_scope(include_docs, include_chunks)
    cypher = _build_subgraph_query(rel_types)
    with GraphStore() as store:
        with store._driver.session(database=store._db) as session:  # noqa: SLF001 - trusted internal use
            record = session.run(cypher, slug=brand_slug).single()
    if record is None:
        return [], []
    return list(record["nodes"] or []), list(record["relationships"] or [])


def render_html(
    brand_slug: str,
    output_path: Path,
    include_docs: bool = False,
    include_chunks: bool = False,
    height: str = "900px",
    physics: bool = True,
) -> Path:
    """
    Render the subgraph for ``brand_slug`` as a standalone HTML file.

    Returns the resolved output path.
    """
    nodes, edges = fetch_subgraph(brand_slug, include_docs=include_docs, include_chunks=include_chunks)
    if not nodes:
        raise RuntimeError(
            f"No graph found for brand slug '{brand_slug}'. "
            "Run `brandgraph ingest` (and optionally `brandgraph tag`) first."
        )

    net = Network(
        height=height,
        width="100%",
        directed=True,
        notebook=False,
        cdn_resources="in_line",
        bgcolor="#ffffff",
        font_color="#212529",
    )
    net.force_atlas_2based(gravity=-45, central_gravity=0.008, spring_length=140)
    if not physics:
        net.toggle_physics(False)

    seen_ids: set[str] = set()
    for n in nodes:
        node_id = n["id"]
        if node_id in seen_ids:
            continue
        seen_ids.add(node_id)
        labels = list(n.get("labels") or [])
        props = dict(n.get("props") or {})
        primary = _primary_label(labels)
        style = NODE_STYLE.get(primary, _DEFAULT_STYLE)
        net.add_node(
            node_id,
            label=_display_label(props),
            title=_tooltip(labels, props),
            color=style["color"],
            size=style["size"],
            shape=style["shape"],
            group=primary,
        )

    for e in edges:
        src, tgt = e["source"], e["target"]
        if src not in seen_ids or tgt not in seen_ids:
            continue
        rel_type = e["type"]
        rel_props = dict(e.get("props") or {})
        net.add_edge(
            src,
            tgt,
            label=rel_type,
            title=_edge_tooltip(rel_type, rel_props),
            arrows="to",
            value=float(rel_props.get("mentions", rel_props.get("score", 1))),
        )

    output_path = output_path.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    # pyvis writes files; use write_html to avoid IPython/notebook branching.
    net.write_html(str(output_path), notebook=False, open_browser=False)
    log.info("wrote %d nodes and %d edges to %s", len(seen_ids), len(edges), output_path)
    return output_path
