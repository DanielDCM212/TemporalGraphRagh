"""
Graph Explorer — Streamlit UI for the Confluence GraphRAG knowledge graph.

Run with:
    streamlit run ui/graph_explorer.py
"""
from __future__ import annotations

import os
import tempfile
from typing import Any, Dict, List, Optional, Set

import streamlit as st
import streamlit.components.v1 as components
from dotenv import load_dotenv
from pymongo import MongoClient
from pyvis.network import Network

load_dotenv()

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ALL_NODE_TYPES = ["ConfPage", "Table", "Event", "Application", "Project", "Attachment"]

NODE_COLORS: Dict[str, str] = {
    "ConfPage":    "#4A90D9",   # blue
    "Table":       "#27AE60",   # green
    "Event":       "#F39C12",   # orange
    "Application": "#E74C3C",   # red
    "Project":     "#9B59B6",   # purple
    "Attachment":  "#1ABC9C",   # teal
}
DEFAULT_COLOR = "#95A5A6"  # grey for unknown types

NODE_SIZES: Dict[str, int] = {
    "ConfPage":    30,
    "Application": 25,
    "Project":     25,
    "Event":       18,
    "Table":       16,
    "Attachment":  16,
}
DEFAULT_SIZE = 14

# ---------------------------------------------------------------------------
# MongoDB helpers  (cached so reconnection is cheap across reruns)
# ---------------------------------------------------------------------------

@st.cache_resource
def get_db(uri: str, db_name: str):
    client = MongoClient(uri, serverSelectionTimeoutMS=5000)
    return client[db_name]


def load_nodes(
    db,
    types: List[str],
    include_deleted: bool,
    limit: int,
) -> List[Dict]:
    query: Dict[str, Any] = {"type": {"$in": types}}
    if not include_deleted:
        query["is_deleted"] = False
    return list(db["graph_nodes"].find(query).limit(limit))


def load_edges_for(db, node_ids: Set[str]) -> List[Dict]:
    """Return edges where both endpoints are in *node_ids*."""
    if not node_ids:
        return []
    id_list = list(node_ids)
    return list(db["graph_edges"].find({
        "source_id": {"$in": id_list},
        "target_id": {"$in": id_list},
    }))

# ---------------------------------------------------------------------------
# Display helpers
# ---------------------------------------------------------------------------

def _node_label(doc: Dict) -> str:
    t = doc.get("type", "")
    p = doc.get("properties", {})
    nid = str(doc["_id"])

    if t == "ConfPage":
        return (p.get("title") or nid)[:40]
    if t == "Application":
        return f"App\n{p.get('app_id', nid)}"
    if t == "Project":
        return f"Proj\n{p.get('project_id', nid)}"
    if t == "Event":
        etype = p.get("event_type", "event")
        desc  = (p.get("description") or "")[:28]
        return f"[{etype}]\n{desc}"
    if t == "Table":
        return f"Table {p.get('table_index', '')}\n({p.get('page_id', '')[:8]})"
    if t == "Attachment":
        return (p.get("filename") or nid)[:30]
    return nid[:35]


def _node_tooltip(doc: Dict) -> str:
    t   = doc.get("type", "?")
    p   = doc.get("properties", {})
    nid = str(doc["_id"])
    ts  = doc.get("timestamp")
    deleted = doc.get("is_deleted", False)

    lines = [
        f"<b>ID:</b> {nid}",
        f"<b>Type:</b> {t}",
    ]
    if ts:
        lines.append(f"<b>Date:</b> {ts.strftime('%Y-%m-%d') if hasattr(ts, 'strftime') else ts}")
    if deleted:
        lines.append("<b style='color:red'>DELETED</b>")

    # Type-specific key properties
    if t == "ConfPage":
        lines.append(f"<b>Title:</b> {p.get('title','')}")
    elif t == "Application":
        lines.append(f"<b>app_id:</b> {p.get('app_id','')}")
        lines.append(f"<b>first_seen:</b> {p.get('first_seen','')}")
        lines.append(f"<b>last_seen:</b> {p.get('last_seen','')}")
    elif t == "Project":
        lines.append(f"<b>project_id:</b> {p.get('project_id','')}")
        lines.append(f"<b>first_seen:</b> {p.get('first_seen','')}")
    elif t == "Event":
        lines.append(f"<b>type:</b> {p.get('event_type','')}")
        lines.append(f"<b>cancelled:</b> {p.get('is_cancelled', False)}")
        lines.append(f"<b>description:</b> {(p.get('description') or '')[:120]}")
        apps = p.get("app_ids", [])
        if apps:
            lines.append(f"<b>apps:</b> {', '.join(apps)}")
    elif t == "Table":
        lines.append(f"<b>headers:</b> {', '.join(p.get('headers', [])[:6])}")
        lines.append(f"<b>rows:</b> {p.get('row_count','')}")
    elif t == "Attachment":
        lines.append(f"<b>file:</b> {p.get('filename','')}")
        lines.append(f"<b>origin:</b> {p.get('origin','')}")
        if p.get("origin") == "table":
            lines.append(
                f"<b>location:</b> table {p.get('table_index')} "
                f"row {p.get('row')} col {p.get('col')}"
            )

    return "<br>".join(lines)


def _short_display_name(doc: Dict) -> str:
    """One-line name for selectbox lists."""
    t = doc.get("type", "")
    p = doc.get("properties", {})
    nid = str(doc["_id"])
    if t == "ConfPage":
        return f"[{t}] {(p.get('title') or nid)[:50]}"
    if t == "Application":
        return f"[{t}] {p.get('app_id', nid)}"
    if t == "Project":
        return f"[{t}] {p.get('project_id', nid)}"
    if t == "Event":
        return f"[{t}] {p.get('event_type','')} — {(p.get('description') or '')[:40]}"
    if t == "Table":
        return f"[{t}] index={p.get('table_index','')} page={p.get('page_id','')[:10]}"
    if t == "Attachment":
        return f"[{t}] {p.get('filename', nid)}"
    return f"[{t}] {nid[:50]}"

# ---------------------------------------------------------------------------
# Graph builder
# ---------------------------------------------------------------------------

def build_network(nodes: List[Dict], edges: List[Dict], height: int) -> Network:
    net = Network(
        height=f"{height}px",
        width="100%",
        directed=True,
        bgcolor="#1a1a2e",
        font_color="white",
    )
    net.set_options("""
    {
      "physics": {
        "enabled": true,
        "solver": "forceAtlas2Based",
        "forceAtlas2Based": {
          "gravitationalConstant": -60,
          "centralGravity": 0.005,
          "springLength": 120,
          "springConstant": 0.05,
          "damping": 0.4
        },
        "stabilization": {"iterations": 200, "updateInterval": 25}
      },
      "edges": {
        "arrows": {"to": {"enabled": true, "scaleFactor": 0.6}},
        "color": {"opacity": 0.6},
        "smooth": {"type": "continuous"},
        "font": {"size": 9, "color": "#cccccc", "strokeWidth": 0}
      },
      "nodes": {
        "font": {"size": 11, "multi": true},
        "borderWidth": 2,
        "borderWidthSelected": 3
      },
      "interaction": {
        "hover": true,
        "tooltipDelay": 80,
        "navigationButtons": true,
        "keyboard": true
      }
    }
    """)

    for doc in nodes:
        nid  = str(doc["_id"])
        t    = doc.get("type", "Unknown")
        deleted = doc.get("is_deleted", False)
        color = NODE_COLORS.get(t, DEFAULT_COLOR)
        if deleted:
            color = "#555555"
        net.add_node(
            nid,
            label=_node_label(doc),
            title=_node_tooltip(doc),
            color={"background": color, "border": "#ffffff", "highlight": {"background": color, "border": "#ffff00"}},
            size=NODE_SIZES.get(t, DEFAULT_SIZE),
            shape="dot" if t not in ("ConfPage", "Application", "Project") else "ellipse",
        )

    for edge in edges:
        src = edge.get("source_id", "")
        tgt = edge.get("target_id", "")
        rel = edge.get("relation", "")
        if src and tgt:
            net.add_edge(src, tgt, label=rel, title=rel)

    return net


def render_network(net: Network) -> None:
    with tempfile.NamedTemporaryFile(
        delete=False, suffix=".html", mode="w", encoding="utf-8"
    ) as f:
        net.save_graph(f.name)
        html = open(f.name, encoding="utf-8").read()
    components.html(html, height=net.height.replace("px", ""), scrolling=False)  # type: ignore[arg-type]

# ---------------------------------------------------------------------------
# Legend
# ---------------------------------------------------------------------------

def render_legend() -> None:
    cols = st.columns(len(ALL_NODE_TYPES))
    for col, t in zip(cols, ALL_NODE_TYPES):
        color = NODE_COLORS.get(t, DEFAULT_COLOR)
        col.markdown(
            f"<span style='display:inline-block;width:14px;height:14px;"
            f"border-radius:50%;background:{color};margin-right:5px'></span>"
            f"**{t}**",
            unsafe_allow_html=True,
        )

# ---------------------------------------------------------------------------
# Streamlit layout
# ---------------------------------------------------------------------------

def main() -> None:
    st.set_page_config(
        page_title="Graph Explorer",
        page_icon="🕸",
        layout="wide",
    )
    st.title("🕸 Knowledge Graph Explorer")

    # ── Sidebar ──────────────────────────────────────────────────────────────
    with st.sidebar:
        st.header("Connection")
        mongo_uri = st.text_input(
            "MongoDB URI",
            value=os.getenv("MONGODB_URI", "mongodb://localhost:27017"),
            type="password",
        )
        db_name = st.text_input(
            "Database",
            value=os.getenv("MONGODB_DB", "confluence_graphrag"),
        )

        st.divider()
        st.header("Node filters")

        selected_types = st.multiselect(
            "Node types",
            options=ALL_NODE_TYPES,
            default=["ConfPage", "Event", "Application", "Project"],
        )

        include_deleted = st.checkbox("Include deleted nodes", value=False)

        max_nodes = st.slider("Max nodes to load", min_value=10, max_value=500, value=150, step=10)

        st.divider()
        st.header("Display")
        graph_height = st.slider("Graph height (px)", 400, 900, 650, step=50)

    if not selected_types:
        st.info("Select at least one node type in the sidebar to get started.")
        return

    # ── Load data ─────────────────────────────────────────────────────────────
    try:
        db = get_db(mongo_uri, db_name)
        # Ping to fail fast on bad connection
        db.client.admin.command("ping")
    except Exception as exc:
        st.error(f"Cannot connect to MongoDB: {exc}")
        return

    all_nodes = load_nodes(db, selected_types, include_deleted, max_nodes)

    if not all_nodes:
        st.warning("No nodes found for the selected types and filters.")
        return

    # ── Node selector ─────────────────────────────────────────────────────────
    display_names = [_short_display_name(n) for n in all_nodes]
    id_by_display = {name: str(n["_id"]) for name, n in zip(display_names, all_nodes)}
    node_by_id    = {str(n["_id"]): n for n in all_nodes}

    with st.expander(f"Filter to specific nodes  ({len(all_nodes)} loaded)", expanded=False):
        selected_display = st.multiselect(
            "Show only these nodes (leave empty = show all)",
            options=display_names,
            default=[],
            help="Type to search. Leave blank to show all loaded nodes.",
        )

    if selected_display:
        visible_ids   = {id_by_display[d] for d in selected_display}
        visible_nodes = [node_by_id[nid] for nid in visible_ids]
    else:
        visible_ids   = set(id_by_display.values())
        visible_nodes = all_nodes

    # ── Load edges between visible nodes ─────────────────────────────────────
    edges = load_edges_for(db, visible_ids)

    # ── Stats ─────────────────────────────────────────────────────────────────
    counts_by_type: Dict[str, int] = {}
    for n in visible_nodes:
        counts_by_type[n.get("type", "?")] = counts_by_type.get(n.get("type", "?"), 0) + 1

    stat_cols = st.columns(len(counts_by_type) + 1)
    stat_cols[0].metric("Edges", len(edges))
    for col, (t, cnt) in zip(stat_cols[1:], sorted(counts_by_type.items())):
        col.metric(t, cnt)

    st.divider()

    # ── Legend ────────────────────────────────────────────────────────────────
    render_legend()
    st.divider()

    # ── Graph ─────────────────────────────────────────────────────────────────
    net = build_network(visible_nodes, edges, graph_height)
    render_network(net)

    # ── Node detail table ─────────────────────────────────────────────────────
    with st.expander("Node details table", expanded=False):
        rows = []
        for n in visible_nodes:
            p = n.get("properties", {})
            rows.append({
                "ID":      str(n["_id"]),
                "Type":    n.get("type", ""),
                "Name":    _node_label(n).replace("\n", " "),
                "Date":    str(n.get("timestamp", ""))[:10],
                "Deleted": n.get("is_deleted", False),
                "page_id": p.get("page_id", ""),
            })
        st.dataframe(rows, use_container_width=True)


if __name__ == "__main__":
    main()
