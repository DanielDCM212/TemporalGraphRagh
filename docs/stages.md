# Pipeline Stages

## Stage 1 — Ingestion

**Files:** `confluence_graphrag/ingestion/`

Pulls pages from Confluence and coordinates the downstream pipeline. Two sub-modes share the same downstream stages.

### Batch mode
`BatchIngestor.run()` / `run_pages()`

1. Paginates all pages in a space via `ConfluenceClient.iter_pages()` (50 pages per API call by default).
2. Filters by date range if `--start`/`--end` provided. Date is extracted from the page **title**, not Confluence metadata.
3. Sorts oldest-first so the temporal graph is built in chronological order.
4. Checks `IngestionLog.needs_upsert()` — skips pages already done and unchanged.
5. Flags pages with no parseable date as `NEEDS_REVIEW`.
6. Writes `PROCESSING` log entry, calls `pipeline.ingest(metadata)`, then marks `DONE` or `ERROR`.

### Incremental mode
`IncrementalWatcher` with APScheduler

- Polls every `INCREMENTAL_POLL_INTERVAL_HOURS` (default 24h). First poll runs immediately on startup.
- Re-ingests any page where Confluence `last_modified > ingestion_log.confluence_last_modified`.

### Date extraction from titles
Three patterns are tried in order:
1. `YYYY-MM-DD`
2. `DD/MM/YYYY`
3. `DD-MM-YYYY`

Pages with no match → `page_date = datetime.min` → status `NEEDS_REVIEW`, skipped by pipeline.

---

## Stage 2 — Parsing

**Files:** `confluence_graphrag/parser/`

`HTMLContentParser.parse(html)` converts Confluence storage-format HTML into a structured `ContentTree`.

### What it handles
- **Text blocks** — paragraphs and headings extracted as `TextChunk` with style flags (`NORMAL`, `CANCELLED`, `BOLD`, etc.). Strikethrough text is tagged `CANCELLED`.
- **Tables** — `ParsedTable` with headers, cell matrix, and rowspan/colspan propagation. Propagated cells are marked `is_propagated=True` to avoid duplicate ID extraction downstream.
- **Nested tables** — cells can contain `sub_tables: List[ParsedTable]`, recursively.
- **Attachments** — page-level `AttachmentRef` objects (file metadata + download URL). Attachment content is not processed in this stage (see TODOs).

### Provenance
Every `TextChunk` and `ParsedCell` carries a `Provenance` object that serializes to a path string:
```
page:{page_id}/table:{idx}/row:{r}/col:{c}
```
This path is stored with every extracted event and normalized row for citation in RAG responses.

---

## Stage 3 — Table Normalization

**Files:** `confluence_graphrag/table_normalization/`

Meeting minutes often have the same table (agreements, risks, action items) with slightly different column headers across documents. This stage builds a canonical vocabulary.

### Pipeline

```
ParsedTable
    │
    ▼
TableFingerprinter.fingerprint()
    → TableFingerprint { headers, col_count, row_count, sample_values }
    │
    ▼
FingerprintClusterer
    ├── Batch mode: DBSCAN clustering of all fingerprints
    └── Incremental: best_match() against existing approved schemas
    │
    ▼
TableClassifier (Gemini)
    → CanonicalSchema { table_type, canonical_columns, column_mapping }
    │
    ▼
TableConsolidator.consolidate()
    → List[NormalizedRow] { canonical_field → cell_value }
```

### Similarity metric
Header similarity uses rapidfuzz token set ratio. The auto-assign threshold is 0.70 — tables with a best-match score below this are routed to `unknown_tables` for manual review.

### Schema lifecycle
| Status | Created by | Used when |
|---|---|---|
| `PENDING_APPROVAL` | `BatchNormalizer` | Historical batch — waits for human `approve()` call |
| `APPROVED` | Human via `CanonicalSchemaStore.approve()` | After manual review |
| `AUTO_APPROVED` | `IncrementalNormalizer` | New table type detected in incremental mode |

### Unknown tables
Tables that can't be matched and don't qualify for new schema creation (too few columns/rows, or too similar to existing) go to the `unknown_tables` MongoDB collection with their best-match score and headers for manual triage.

---

## Stage 4 — Entity Extraction

**Files:** `confluence_graphrag/entity_extraction/`

Finds structured entities in the page text: application IDs, project IDs, and meeting events.

### Step 1 — Regex scan
`RegexScanner` runs first. Fast and deterministic.
- `app_id`: exactly 11 consecutive digits
- `project_id`: exactly 9 consecutive digits

Propagated table cells are skipped to avoid counting the same ID multiple times from rowspan-expanded cells.

### Step 2 — LLM extraction (one call per page)
`LLMEntityExtractor` sends up to 8,000 characters of page text to Gemini. It returns:
- `additional_app_ids` — IDs in non-standard formats (e.g. `"APP: 123.456.789.01"`, `"PRJ-123/456/789"`)
- `events` — structured events with type, description, linked IDs, and cancellation flag

Event types: `decision`, `approval`, `cancellation`, `status_change`, `risk`, `action_item`

### Step 3 — Validation
`ValidationGateway` calls an external REST API to confirm each candidate ID. Runs concurrently with a configurable semaphore (`VALIDATION_CONCURRENCY`, default 10).

IDs that fail validation go to `unvalidated_ids` collection with the surrounding text context for manual review.

### Step 4 — EntitySet
The final output combines validated IDs and LLM-detected events:
```python
EntitySet {
    page_id, page_date,
    app_ids: List[str],        # validated 11-digit IDs
    project_ids: List[str],    # validated 9-digit IDs
    unvalidated_ids: List[CandidateId],
    events: List[ExtractedEvent],
}
```

---

## Stage 5 — Graph Construction

**Files:** `confluence_graphrag/graph/`

`TemporalGraphBuilder.ingest_page()` builds the knowledge graph from `EntitySet` + `ContentTree`.

### Node upsert order
1. **Soft-delete** all existing nodes owned by this page (`ConfPage`, `Table`, `Event`) — ensures re-ingest is idempotent.
2. **ConfPage** — one node per page.
3. **Table** nodes — one per `ParsedTable`, with `CONTAINS` edge from the page.
4. **Application** nodes — global (never soft-deleted). Merges `first_seen`/`last_seen` timestamps across pages.
5. **Project** nodes — same merge logic as Application.
6. **Event** nodes — one per extracted event. Gets `HAS_EVENT` edges from page, from each referenced Application, and from each referenced Project.

### Temporal metadata
Every node stores `timestamp = page_date` (the meeting date, not ingest time). This is what drives the temporal decay scoring in Stage 6.

### Graphiti (optional)
If `GRAPHITI_ENABLED=true` and a Neo4j URI is configured, `_add_graphiti_episode()` sends a text summary of the page to Graphiti's episodic memory layer. This enables relationship extraction and knowledge graph reasoning via Graphiti's LLM pipeline.

### Backend swap
`MongoDBAdapter` is the default. `Neo4jAdapter` is the production target — same interface, no changes needed in `TemporalGraphBuilder`.

---

## Stage 6 — Embedding and Retrieval

**Files:** `confluence_graphrag/graph/embedder.py`, `retriever.py`, `scoring.py`

Adds vector representations to the graph and enables semantic search.

### What gets embedded (per page, one batched API call)

| Source | Text format | Storage |
|---|---|---|
| `ConfPage` | `Title: …\nDate: …\n{text_blocks}` | `graph_nodes.embedding` |
| Each `Event` | `[type] description\nPage: … \| Date: …\nApps: …` | `graph_nodes.embedding` |
| Each table row | `header: cell \| header: cell \| …` | `graph_row_embeddings` collection |

Model: **text-embedding-004** (768-dimensional vectors) via `langchain_google_genai`.

Embedding is optional — if `GOOGLE_API_KEY` is not set, the pipeline runs without embeddings and the `/search` endpoint returns `503`.

### Retrieval scoring

```
combined_score = cosine_similarity(query, content) × temporal_score(event_date)
```

`temporal_score` uses exponential decay with a 365-day half-life:
```
score = e^(-ln(2) × days_elapsed / 365)
```

A decision from last month scores much higher than an identical decision from 3 years ago.

### Search flow

1. Embed the query string with `text-embedding-004`.
2. Fetch all candidate nodes of requested types with an `embedding` field.
3. Compute cosine similarity in Python (NumPy).
4. Fetch all non-deleted row embeddings.
5. Compute cosine similarity for rows.
6. Merge, apply temporal decay, sort by `combined_score`, return top-k.

**Upgrade path:** Replace steps 2–4 with MongoDB Atlas `$vectorSearch` — zero changes outside `MongoDBAdapter`.
