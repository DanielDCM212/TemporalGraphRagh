# TODOs and Known Gaps

This document tracks gaps, incomplete features, and future work. Items are grouped by priority and area.

---

## High Priority

### Attachment Processing (Next Stage)
**Status:** Intentionally deferred.

The parser already extracts `AttachmentRef` objects (filename, URL, media type) but their content is never downloaded or processed. Meeting minutes often contain critical information in attached PDFs, Excel files, and Word documents.

**What needs to be built:**
- `AttachmentProcessor` that dispatches by `AttachmentType` (PDF → pdfplumber, Excel → openpyxl/pandas, DOCX → python-docx, PPTX → python-pptx)
- Image handling (OCR or vision model — `pillow` already installed)
- Content extracted from attachments should flow into Stage 4 (entity extraction) and Stage 6 (embeddings)
- Provenance path already supports `attachment:{attachment_id}` — just needs to be populated

**Files to create:** `confluence_graphrag/attachment_processor/`  
**Dependencies already in pyproject.toml:** pdfplumber, openpyxl, python-docx, python-pptx, pillow

---

### Atlas Vector Search Migration
**Status:** Python-side cosine works for small datasets. Breaks at scale.

Current `vector_search_nodes()` and `vector_search_rows()` in `MongoDBAdapter` fetch **all** embedding documents and compute cosine in Python. This is O(N × 768) per query.

**Migration path:**
1. Move to MongoDB Atlas (requires M10+ cluster)
2. Create vector search index via Atlas UI or CLI:
   ```json
   {
     "name": "embedding_vector_index",
     "type": "vectorSearch",
     "definition": {
       "fields": [
         { "type": "vector", "path": "embedding", "numDimensions": 768, "similarity": "cosine" },
         { "type": "filter", "path": "type" },
         { "type": "filter", "path": "is_deleted" },
         { "type": "filter", "path": "timestamp" }
       ]
     }
   }
   ```
3. Replace Python cosine in `MongoDBAdapter` with `$vectorSearch` aggregation stage
4. No changes needed in `SemanticRetriever` or routes

**Files to modify:** `confluence_graphrag/graph/mongodb_adapter.py` only

---

### Persistent Job Queue
**Status:** Ingestion jobs are stored in memory (`app.state.jobs`). Restarting the server loses all job history.

**Migration path:** Replace `asyncio.create_task` + in-memory dict with Celery + Redis (both already in `pyproject.toml`).

**Files to modify:** `confluence_graphrag/api/routes/ingestion.py`, `app.py`

---

## Medium Priority

### Neo4j Vector Index
**Status:** `Neo4jAdapter` raises `NotImplementedError` for all four embedding methods.

The MongoDB embedding pipeline works end-to-end. When the team moves to Neo4j as the primary backend, the vector methods need to be implemented using Neo4j GDS vector index or a dedicated vector store sidecar.

**Files to modify:** `confluence_graphrag/graph/neo4j_adapter.py`

---

### IncrementalWatcher Not Wired into API
**Status:** The incremental watcher is only available via CLI (`python main.py --mode incremental`).

The FastAPI app has no endpoint to start/stop the incremental watcher, which means long-running monitoring requires keeping the CLI process alive separately.

**What to add:**
- `POST /ingest/watcher/start` — start the APScheduler watcher for a space
- `POST /ingest/watcher/stop` — stop it
- `GET /ingest/watcher/status` — running/stopped + next scheduled run

**Files to modify:** `confluence_graphrag/api/routes/ingestion.py`, `app.py`

---

### Schema Approval UI / Endpoint
**Status:** Schema approval is done manually by editing MongoDB documents.

During batch ingestion, `BatchNormalizer` creates schemas with `status=PENDING_APPROVAL`. There is no API to list pending schemas or approve them.

**What to add:**
- `GET /schemas/pending` — list schemas awaiting approval
- `POST /schemas/{schema_id}/approve` — approve a schema
- `PUT /schemas/{schema_id}` — edit column mapping before approving

**Files to create:** `confluence_graphrag/api/routes/schemas.py`

---

### API Authentication
**Status:** The API has no authentication. Anyone with network access can trigger ingestion or read graph data.

Add Bearer token middleware or OAuth2 depending on deployment context. FastAPI has built-in support for both.

---

### Retry Mechanism for Failed Pages
**Status:** Failed pages are logged with `status=error` and `retry_count` is incremented, but there is no automatic retry.

`IngestionLog.list_errors()` already returns retryable page IDs. A background task or scheduled job should periodically call this and re-submit those pages to the pipeline.

---

## Low Priority

### Embedding Model Versioning
**Status:** All embeddings use `text-embedding-004` (768 dimensions). If the model changes, old and new embeddings are incompatible and cosine similarity comparisons break silently.

**Mitigation:**
- Store `embedding_model` field alongside every embedding vector
- Add a migration job that re-embeds all documents when the model changes
- Consider adding a `embedding_version` field to `graph_nodes` and `graph_row_embeddings`

---

### `unvalidated_ids` Review Workflow
**Status:** Unvalidated IDs are written to MongoDB but there is no API to review or resolve them.

The `status` field supports `pending_review`, `confirmed_manual`, and `rejected` but nothing sets it to anything other than `pending_review`.

**What to add:** An API endpoint to list and update unvalidated ID statuses.

---

### `unknown_tables` Review Workflow
**Status:** Same situation as `unvalidated_ids`. Tables go in but nothing brings them back out for triage.

---

### Confluence Server / Data Center Support
**Status:** `CONFLUENCE_CLOUD=false` is supported in config but not tested. The Confluence API differs between Cloud and Server for some endpoints (e.g. attachment downloads).

---

### Structured Logging / Observability
**Status:** All logging uses Python's standard `logging` module with a basic console formatter. No structured logs, no tracing, no metrics endpoint.

`loguru` is already installed — consider migrating and adding a `/metrics` endpoint (Prometheus format) tracking: pages ingested, embeddings generated, search queries, validation API latency.

---

### Test Coverage
**Status:** No tests exist yet (pytest and pytest-asyncio are in dev dependencies).

**Critical paths to cover first:**
1. `HTMLContentParser` — rowspan/colspan propagation, cancelled detection
2. `IncrementalNormalizer` — threshold matching logic, unknown table routing
3. `SemanticRetriever` — combined score calculation
4. `MongoDBAdapter` — soft delete idempotency
5. API route integration tests against a real MongoDB (use `mongomock-motor` or testcontainers)
