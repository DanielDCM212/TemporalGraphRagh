import asyncio
from datetime import datetime
from typing import Optional

import numpy as np

from .mongo_client import get_db


def _serialize(doc: dict) -> dict:
    """Convert a MongoDB document to a JSON-serializable dict."""
    if not doc:
        return {}
    out = {}
    for k, v in doc.items():
        if k == "_id":
            out[k] = str(v)
        elif k == "embedding":
            # Never return raw embedding vectors to the agent
            continue
        elif isinstance(v, datetime):
            out[k] = v.isoformat()
        elif isinstance(v, dict):
            out[k] = _serialize(v)
        elif isinstance(v, list):
            out[k] = [
                _serialize(i) if isinstance(i, dict)
                else i.isoformat() if isinstance(i, datetime)
                else i
                for i in v
            ]
        else:
            out[k] = v
    return out


# ── Embedding helper ────────────────────────────────────────────────────────

_embedder = None


def _get_embedder():
    global _embedder
    if _embedder is None:
        from confluence_graphrag.graph.embedder import EmbeddingService
        _embedder = EmbeddingService()
    return _embedder


async def _embed_query(text: str) -> list[float]:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _get_embedder().embed, text)


def _cosine_top_k(query_emb: list[float], docs: list[dict], k: int) -> list[tuple[dict, float]]:
    """Return up to k docs ranked by cosine similarity to query_emb."""
    if not docs:
        return []
    q = np.array(query_emb, dtype=np.float32)
    q_norm = np.linalg.norm(q)
    if q_norm < 1e-10:
        return []
    q /= q_norm

    scored = []
    for doc in docs:
        emb = doc.get("embedding")
        if not emb:
            continue
        e = np.array(emb, dtype=np.float32)
        e_norm = np.linalg.norm(e)
        if e_norm < 1e-10:
            continue
        scored.append((doc, float(np.dot(q, e / e_norm))))

    scored.sort(key=lambda x: x[1], reverse=True)
    return scored[:k]


# ── Tools ───────────────────────────────────────────────────────────────────

async def get_project_info(project_id: str) -> dict:
    """
    Retrieve stored information about a project by its ID.

    Args:
        project_id: Project identifier, typically 9 digits (e.g. '000100000').

    Returns:
        Project properties including first_seen, last_seen, raw_ids, and
        validation status. Empty dict if the project is not found.
    """
    db = get_db()
    doc = await db.graph_nodes.find_one(
        {"type": "Project", "properties.project_id": project_id, "is_deleted": False}
    )
    if not doc:
        doc = await db.graph_nodes.find_one(
            {"type": "Project", "properties.raw_ids": project_id, "is_deleted": False}
        )
    return _serialize(doc) if doc else {}


async def get_project_events(
    project_id: str,
    event_type: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    limit: int = 20,
) -> list[dict]:
    """
    Get events for a project ordered from most recent to oldest.

    Args:
        project_id: Project identifier.
        event_type: Optional filter. One of: decision, status_change,
            cancellation, approval, risk, action_item.
        start_date: Inclusive lower bound (ISO date: YYYY-MM-DD).
        end_date: Inclusive upper bound (ISO date: YYYY-MM-DD).
        limit: Maximum number of events to return (default 20).

    Returns:
        List of event dicts. Each entry includes event_type, description,
        timestamp (ISO), provenance path, and associated app_ids.
        Ordered most-recent first.
    """
    db = get_db()

    query: dict = {
        "type": "Event",
        "is_deleted": False,
        "properties.project_ids": project_id,
    }

    if event_type:
        query["properties.event_type"] = event_type

    if start_date or end_date:
        date_filter: dict = {}
        if start_date:
            date_filter["$gte"] = datetime.fromisoformat(start_date)
        if end_date:
            date_filter["$lte"] = datetime.fromisoformat(end_date)
        query["timestamp"] = date_filter

    cursor = db.graph_nodes.find(query).sort("timestamp", -1).limit(limit)
    docs = await cursor.to_list(length=limit)
    return [_serialize(d) for d in docs]


async def get_applications_for_project(project_id: str) -> list[dict]:
    """
    Get all applications linked to a project.

    Applications are discovered through events that co-reference both the
    project and one or more application IDs in the same meeting minute.

    Args:
        project_id: Project identifier.

    Returns:
        List of application dicts with app_id, name, first_seen, last_seen,
        and validation status.
    """
    db = get_db()

    cursor = db.graph_nodes.find(
        {
            "type": "Event",
            "is_deleted": False,
            "properties.project_ids": project_id,
            "properties.app_ids": {"$exists": True, "$ne": []},
        },
        {"properties.app_ids": 1},
    )
    events = await cursor.to_list(length=None)

    app_ids = {
        aid
        for event in events
        for aid in event.get("properties", {}).get("app_ids", [])
    }

    if not app_ids:
        return []

    cursor = db.graph_nodes.find(
        {
            "type": "Application",
            "is_deleted": False,
            "properties.app_id": {"$in": list(app_ids)},
        }
    )
    apps = await cursor.to_list(length=None)
    return [_serialize(a) for a in apps]


async def get_application_events(
    app_id: str,
    event_type: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    limit: int = 20,
) -> list[dict]:
    """
    Get events for an application ordered from most recent to oldest.

    Args:
        app_id: Application identifier (typically 11 digits).
        event_type: Optional filter. One of: decision, status_change,
            cancellation, approval, risk, action_item.
        start_date: Inclusive lower bound (ISO date: YYYY-MM-DD).
        end_date: Inclusive upper bound (ISO date: YYYY-MM-DD).
        limit: Maximum number of events to return (default 20).

    Returns:
        List of event dicts ordered most-recent first.
    """
    db = get_db()

    query: dict = {
        "type": "Event",
        "is_deleted": False,
        "properties.app_ids": app_id,
    }

    if event_type:
        query["properties.event_type"] = event_type

    if start_date or end_date:
        date_filter: dict = {}
        if start_date:
            date_filter["$gte"] = datetime.fromisoformat(start_date)
        if end_date:
            date_filter["$lte"] = datetime.fromisoformat(end_date)
        query["timestamp"] = date_filter

    cursor = db.graph_nodes.find(query).sort("timestamp", -1).limit(limit)
    docs = await cursor.to_list(length=limit)
    return [_serialize(d) for d in docs]


async def get_project_latest_snapshot(project_id: str) -> dict:
    """
    Get a full snapshot of the most recent meeting where a project was discussed.

    Steps performed:
    1. Find the latest event that references the project to determine the most
       recent meeting date and its source page_id.
    2. Fetch ALL events from that same meeting page that reference this project.
    3. Also fetch the ConfPage node for metadata (title, date, url).

    Use this tool when the user asks for the "current status", "latest update",
    or "what is happening now" for a project. Do NOT filter by event type —
    return everything from that latest meeting so a full summary can be generated.

    Args:
        project_id: Project identifier.

    Returns:
        Dict with keys:
          - 'meeting_date': ISO date of the latest meeting
          - 'page_title': title of the meeting-minute page
          - 'page_id': Confluence page ID
          - 'events': list of all events from that meeting referencing this project
    """
    db = get_db()

    latest = await db.graph_nodes.find_one(
        {"type": "Event", "is_deleted": False, "properties.project_ids": project_id},
        sort=[("timestamp", -1)],
    )
    if not latest:
        return {"meeting_date": None, "page_title": None, "page_id": None, "events": []}

    page_id = latest.get("properties", {}).get("page_id")
    meeting_date = latest.get("timestamp")

    cursor = db.graph_nodes.find(
        {
            "type": "Event",
            "is_deleted": False,
            "properties.page_id": page_id,
            "properties.project_ids": project_id,
        }
    ).sort("timestamp", 1)
    events = await cursor.to_list(length=None)

    page_node = await db.graph_nodes.find_one(
        {"type": "ConfPage", "_id": f"page_{page_id}", "is_deleted": False}
    )

    return {
        "meeting_date": meeting_date.isoformat() if isinstance(meeting_date, datetime) else meeting_date,
        "page_title": page_node.get("properties", {}).get("title") if page_node else None,
        "page_id": page_id,
        "events": [_serialize(e) for e in events],
    }


async def search_meeting_content(
    query_text: str,
    project_id: Optional[str] = None,
    app_id: Optional[str] = None,
    limit: int = 10,
) -> list[dict]:
    """
    Semantic search across meeting-minute table rows using embedding similarity.

    Embeds query_text with the same model used at ingestion time (text-embedding-004)
    and ranks stored row embeddings by cosine similarity. Use this for open-ended
    queries not tied to a specific event type, or when content may span many rows.

    Args:
        query_text: Natural-language question or phrase to search for.
        project_id: Optional project ID to restrict the search to pages that
            reference that project.
        app_id: Optional application ID to restrict similarly.
        limit: Maximum results to return (default 10).

    Returns:
        List of matching row records with 'text', 'table_id', 'page_id',
        'row_index', 'timestamp', and 'similarity_score'. Ordered by
        descending semantic similarity.
    """
    db = get_db()

    # Optionally restrict to pages that reference the given project / app
    page_ids: Optional[list] = None
    if project_id or app_id:
        event_filter: dict = {"type": "Event", "is_deleted": False}
        if project_id:
            event_filter["properties.project_ids"] = project_id
        if app_id:
            event_filter["properties.app_ids"] = app_id

        events = await db.graph_nodes.find(
            event_filter, {"properties.page_id": 1}
        ).to_list(length=None)

        page_ids = list(
            {
                e["properties"]["page_id"]
                for e in events
                if "page_id" in e.get("properties", {})
            }
        )

    # Load candidate rows (with their embeddings)
    row_query: dict = {"is_deleted": False, "embedding": {"$exists": True}}
    if page_ids is not None:
        if not page_ids:
            return []  # project/app exists but has no associated pages
        row_query["page_id"] = {"$in": page_ids}

    docs = await db.graph_row_embeddings.find(
        row_query,
        {"text": 1, "table_id": 1, "page_id": 1, "row_index": 1, "timestamp": 1, "embedding": 1},
    ).to_list(length=None)

    # Embed the query and rank
    query_emb = await _embed_query(query_text)
    ranked = _cosine_top_k(query_emb, docs, limit)

    results = []
    for doc, score in ranked:
        entry = _serialize(doc)
        entry["similarity_score"] = round(score, 4)
        results.append(entry)
    return results


async def search_graph_nodes(
    query_text: str,
    node_types: Optional[list[str]] = None,
    limit: int = 10,
) -> list[dict]:
    """
    Semantic search across the knowledge graph nodes using embedding similarity.

    Searches ConfPage, Event, Application, and Project nodes stored in graph_nodes.
    Use this to find relevant meetings, decisions, or entities when you don't yet
    have a specific project_id or app_id to start from.

    Args:
        query_text: Natural-language question or phrase.
        node_types: Optional list to restrict search to specific node types.
            Valid values: 'ConfPage', 'Event', 'Application', 'Project'.
            Defaults to ['ConfPage', 'Event'].
        limit: Maximum results to return (default 10).

    Returns:
        List of node dicts with 'type', 'properties', 'timestamp', and
        'similarity_score'. Ordered by descending semantic similarity.
    """
    db = get_db()

    types = node_types if node_types else ["ConfPage", "Event"]

    docs = await db.graph_nodes.find(
        {
            "type": {"$in": types},
            "is_deleted": False,
            "embedding": {"$exists": True},
        },
        {"type": 1, "properties": 1, "timestamp": 1, "embedding": 1},
    ).to_list(length=None)

    query_emb = await _embed_query(query_text)
    ranked = _cosine_top_k(query_emb, docs, limit)

    results = []
    for doc, score in ranked:
        entry = _serialize(doc)
        entry["similarity_score"] = round(score, 4)
        results.append(entry)
    return results
