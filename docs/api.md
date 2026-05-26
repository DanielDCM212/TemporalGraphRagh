# API Reference

Base URL: `http://localhost:8000`  
Interactive docs: `http://localhost:8000/docs`

---

## Ingestion

### `POST /ingest/space`

Trigger batch ingestion for an entire Confluence space. Runs in background — returns a `job_id` immediately.

**Request body:**
```json
{
  "space_key": "PROJ",
  "start_date": "2023-01-01T00:00:00",   // optional
  "end_date": "2024-12-31T00:00:00"      // optional
}
```

**Response `202`:**
```json
{
  "job_id": "550e8400-e29b-41d4-a716-446655440000",
  "status": "running",
  "started_at": "2024-03-16T11:00:00",
  "finished_at": null,
  "stats": null,
  "error": null
}
```

---

### `POST /ingest/pages`

Ingest a specific list of page IDs. Runs in background.

**Request body:**
```json
{
  "space_key": "PROJ",
  "page_ids": ["12345678", "87654321"]
}
```

**Response `202`:** Same as `/ingest/space`.

---

### `GET /ingest/jobs/{job_id}`

Poll the status of a background ingestion job.

**Response `200`:**
```json
{
  "job_id": "550e8400-...",
  "status": "done",            // running | done | error
  "started_at": "2024-03-16T11:00:00",
  "finished_at": "2024-03-16T11:05:32",
  "stats": { "total": 42, "ok": 41, "errors": 1, "skipped": 0 },
  "error": null
}
```

> **Note:** Jobs are stored in-memory and reset on server restart. See TODOs for a persistent queue.

---

### `GET /ingest/jobs`

List all jobs since last server start.

**Response `200`:** Array of job objects.

---

### `DELETE /ingest/page/{page_id}`

Soft-delete all graph nodes owned by a page. Does not remove the ingestion log entry.

**Response `204`:** No content.

---

### `GET /ingest/log/{page_id}`

Get the ingestion log entry for a single page.

**Response `200`:**
```json
{
  "page_id": "12345678",
  "page_title": "Meeting Minutes 2024-03-15",
  "page_date": "2024-03-15T00:00:00",
  "space_key": "PROJ",
  "status": "done",
  "processed_at": "2024-03-16T11:00:00",
  "attachment_count": 3,
  "retry_count": 0,
  "error_message": null
}
```

**Response `404`:** Page not in ingestion log.

---

### `GET /ingest/errors?max_retry=3`

List page IDs that failed ingestion and have fewer than `max_retry` attempts.

**Response `200`:** `["12345678", "87654321"]`

---

## Retrieval

> Retrieval endpoints require `GOOGLE_API_KEY` to be set. Returns `503` otherwise.

### `POST /search`

Semantic search over all ingested content. Results ranked by `combined_score = cosine_similarity × temporal_decay`.

**Request body:**
```json
{
  "query": "cancelled applications approved in Q1 2024",
  "node_types": ["Event", "ConfPage"],  // optional, default: ["Event", "ConfPage"]
  "include_table_rows": true,           // optional, default: true
  "before_date": "2024-06-01T00:00:00", // optional — temporal filter
  "query_date": null,                   // optional — reference date for decay (default: now)
  "limit": 20                           // 1–100, default: 20
}
```

**Response `200`:**
```json
{
  "query": "cancelled applications approved in Q1 2024",
  "count": 5,
  "results": [
    {
      "node_id": "event_12345678_2",
      "node_type": "Event",
      "text": "[cancellation] App 12345678901 was cancelled\nPage: Minutes 2024-02-10 | Date: 2024-02-10\nApps: 12345678901",
      "timestamp": "2024-02-10T00:00:00",
      "semantic_score": 0.8921,
      "temporal_score": 0.9342,
      "combined_score": 0.8334,
      "properties": {
        "event_type": "cancellation",
        "description": "App 12345678901 was cancelled",
        "app_ids": ["12345678901"],
        "is_cancelled": true,
        "page_id": "12345678"
      }
    }
  ]
}
```

---

### `GET /graph/node/{node_id}`

Fetch a single graph node by its ID.

**Response `200`:**
```json
{
  "id": "app_12345678901",
  "type": "Application",
  "timestamp": "2022-01-15T00:00:00",
  "is_deleted": false,
  "properties": {
    "app_id": "12345678901",
    "validated": true,
    "first_seen": "2022-01-15",
    "last_seen": "2024-03-15"
  }
}
```

**Response `404`:** Node not found.

---

### `GET /graph/app/{app_id}/timeline?before_date=&limit=20`

Return all `Event` nodes that reference an application, ordered newest-first. Useful for the full decision/approval/cancellation history of an app.

**Query params:**
- `before_date` (optional) — only events on or before this date
- `limit` (optional, 1–200, default 20)

**Response `200`:**
```json
{
  "app_id": "12345678901",
  "before_date": null,
  "count": 3,
  "events": [
    {
      "id": "event_99887766_0",
      "type": "Event",
      "timestamp": "2024-03-15T00:00:00",
      "is_deleted": false,
      "properties": {
        "event_type": "approval",
        "description": "Application approved for production deployment",
        "is_cancelled": false,
        "app_ids": ["12345678901"]
      }
    }
  ]
}
```

---

## Meta

### `GET /health`

**Response `200`:** `{"status": "ok"}`
