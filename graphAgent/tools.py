from datetime import datetime
from typing import Optional

from .mongo_client import get_db


def _serialize(doc: dict) -> dict:
    """Convert a MongoDB document to a JSON-serializable dict."""
    if not doc:
        return {}
    out = {}
    for k, v in doc.items():
        if k == "_id":
            out[k] = str(v)
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

    # Collect unique app_ids from events that reference this project
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

    # Step 1: find the most recent event for this project
    latest = await db.graph_nodes.find_one(
        {"type": "Event", "is_deleted": False, "properties.project_ids": project_id},
        sort=[("timestamp", -1)],
    )
    if not latest:
        return {"meeting_date": None, "page_title": None, "page_id": None, "events": []}

    page_id = latest.get("properties", {}).get("page_id")
    meeting_date = latest.get("timestamp")

    # Step 2: fetch all events from that page that reference this project
    cursor = db.graph_nodes.find(
        {
            "type": "Event",
            "is_deleted": False,
            "properties.page_id": page_id,
            "properties.project_ids": project_id,
        }
    ).sort("timestamp", 1)
    events = await cursor.to_list(length=None)

    # Step 3: fetch ConfPage metadata
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
    Full-text search across meeting-minute table rows.

    Useful for open-ended queries that are not tied to a specific event type,
    or when the user asks about content that may be spread across multiple rows.

    Args:
        query_text: Keywords or phrase to search for.
        project_id: Optional project ID to narrow results to pages that
            reference that project.
        app_id: Optional application ID to narrow results similarly.
        limit: Maximum results (default 10).

    Returns:
        List of matching row-level records with 'text', 'table_id',
        'page_id', and 'timestamp'. Ordered most-recent first.
    """
    db = get_db()

    base_query: dict = {
        "is_deleted": False,
        "text": {"$regex": query_text, "$options": "i"},
    }

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
        if page_ids:
            base_query["page_id"] = {"$in": page_ids}

    cursor = (
        db.graph_row_embeddings.find(
            base_query,
            {"text": 1, "table_id": 1, "page_id": 1, "timestamp": 1, "row_index": 1},
        )
        .sort("timestamp", -1)
        .limit(limit)
    )
    docs = await cursor.to_list(length=limit)
    return [_serialize(d) for d in docs]
