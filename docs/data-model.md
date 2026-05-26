# Data Model

All persistent state lives in MongoDB. The default database is `confluence_graphrag`.

---

## Collections

### `ingestion_log`

Tracks the ingestion status of every Confluence page. Source of truth for idempotency.

```json
{
  "_id": ObjectId,
  "page_id": "12345678",
  "page_title": "Meeting Minutes 2024-03-15",
  "page_date": ISODate("2024-03-15T00:00:00Z"),
  "space_key": "PROJ",
  "confluence_last_modified": ISODate("2024-03-16T10:30:00Z"),
  "processed_at": ISODate("2024-03-16T11:00:00Z"),
  "status": "done",               // pending | processing | done | error | needs_review
  "error_message": null,
  "attachment_count": 3,
  "retry_count": 0
}
```

**Indexes:** `page_id` (unique), `status`, `space_key`

---

### `graph_nodes`

All knowledge graph nodes. Page-owned types (`ConfPage`, `Table`, `Event`) are soft-deleted on re-ingest. Global types (`Application`, `Project`) are merged.

```json
{
  "_id": "page_12345678",
  "type": "ConfPage",             // ConfPage | Table | Application | Project | Event
  "timestamp": ISODate("2024-03-15T00:00:00Z"),
  "is_deleted": false,
  "embedding": [0.012, -0.034, ...],   // 768 floats, present if GOOGLE_API_KEY set
  "embedding_text": "Title: Meeting...",
  "properties": {
    "page_id": "12345678",
    "title": "Meeting Minutes 2024-03-15",
    "date": "2024-03-15",
    "has_cancelled": false
  }
}
```

**Properties by type:**

| Type | Properties |
|---|---|
| `ConfPage` | `page_id`, `title`, `date`, `has_cancelled` |
| `Table` | `table_id`, `table_index`, `headers`, `row_count`, `col_count`, `page_id` |
| `Application` | `app_id`, `validated`, `first_seen`, `last_seen` |
| `Project` | `project_id`, `validated`, `first_seen`, `last_seen` |
| `Event` | `event_type`, `description`, `is_cancelled`, `provenance`, `app_ids`, `project_ids`, `page_id` |

**Indexes:** `type`, `is_deleted`, `timestamp`, `properties.page_id`, `properties.app_id`, `properties.project_id`, partial index on nodes with `embedding` field.

---

### `graph_edges`

Relationships between nodes.

```json
{
  "_id": "page_12345678__CONTAINS__12345678_table_0",
  "source_id": "page_12345678",
  "target_id": "12345678_table_0",
  "relation": "CONTAINS",         // CONTAINS | HAS_EVENT | REFERENCES_APP | REFERENCES_PROJ
  "properties": { "order": 0 }
}
```

**Indexes:** `source_id`, `target_id`, `relation`

---

### `graph_row_embeddings`

Per-row embeddings for all table cells. Separate collection because one table can have hundreds of rows.

```json
{
  "_id": "12345678_table_0__row_2",
  "table_id": "12345678_table_0",
  "page_id": "12345678",
  "row_index": 2,
  "text": "agreement: Deploy app X | responsible: Juan | due_date: 2024-06-01 | status: pending",
  "embedding": [0.021, -0.018, ...],
  "timestamp": ISODate("2024-03-15T00:00:00Z"),
  "is_deleted": false
}
```

**Indexes:** `page_id`, `table_id`, `timestamp`, `is_deleted`

---

### `canonical_schemas`

Table schemas produced by the LLM classifier. Reviewed and approved before consolidation in batch mode.

```json
{
  "_id": "uuid-string",
  "schema_id": "uuid-string",
  "table_type": "agreements",
  "description": "Table of meeting agreements and commitments",
  "canonical_columns": ["agreement", "responsible", "due_date", "status", "app_id"],
  "column_mapping": {
    "Acuerdo": "agreement",
    "Agreement": "agreement",
    "Responsable": "responsible",
    "Fecha": "due_date",
    "Estatus": "status"
  },
  "status": "approved",           // pending_approval | approved | auto_approved
  "approved_by": "user@company.com",
  "approved_at": ISODate("2024-03-20T09:00:00Z"),
  "version": 1
}
```

---

### `unknown_tables`

Tables that the normalizer couldn't match to any schema and didn't qualify for new schema creation.

```json
{
  "table_id": "12345678_table_3",
  "page_id": "12345678",
  "page_date": "2024-03-15",
  "raw_headers": ["Nro.", "Asunto", "Área"],
  "best_match_type": "agreements",
  "best_match_score": 0.42,
  "sample_values": [["1", "Deploy infra", "IT"]],
  "provenance_path": "page:12345678/table:3"
}
```

**Action:** Manual triage — either extend an existing schema's `column_mapping` or create a new `CanonicalSchema`.

---

### `unvalidated_ids`

IDs detected by regex or LLM that failed external API validation.

```json
{
  "candidate_id": "12345678901",
  "id_type": "app_id",
  "provenance_path": "page:12345678/table:1/row:3/col:2",
  "context": "Application 12345678901 was approved for",
  "page_id": "12345678",
  "detected_at": ISODate("2024-03-16T11:00:00Z"),
  "status": "pending_review"      // pending_review | confirmed_manual | rejected
}
```

**Action:** Manual review — update `status` to `confirmed_manual` if the ID is real, `rejected` if it's noise.

---

## Node ID Conventions

| Type | Format |
|---|---|
| ConfPage | `page_{page_id}` |
| Table | `{page_id}_table_{table_index}` |
| Application | `app_{app_id}` |
| Project | `proj_{project_id}` |
| Event | `event_{page_id}_{event_index}` |
| RowEmbedding | `{table_id}__row_{row_index}` |
| Edge | `{source_id}__{relation}__{target_id}` |

---

## Soft Delete

Re-ingesting a page is safe. The flow:

1. `soft_delete_page(page_id)` sets `is_deleted = true` on all `ConfPage`, `Table`, and `Event` nodes owned by that page.
2. Row embeddings for those tables are also marked `is_deleted = true`.
3. New nodes are upserted with `is_deleted = false`.
4. `Application` and `Project` nodes are **never** soft-deleted — only their `last_seen` timestamp is updated.

All read queries (traversal, vector search, temporal context) filter `is_deleted: false`.
