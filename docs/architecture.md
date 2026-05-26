# Architecture

## Overview

Confluence GraphRAG is a 6-stage pipeline that transforms raw Confluence meeting minutes into a queryable temporal knowledge graph. Each stage produces a well-defined output consumed by the next.

```
┌─────────────────────────────────────────────────────────────────────┐
│                        Confluence API                                │
└──────────────────────────────┬──────────────────────────────────────┘
                               │ HTML pages + attachments
                               ▼
┌─────────────────────────────────────────────────────────────────────┐
│  Stage 1 · Ingestion                                                 │
│  ConfluenceClient · BatchIngestor · IncrementalWatcher               │
│  IngestionLog (MongoDB)                                              │
└──────────────────────────────┬──────────────────────────────────────┘
                               │ PageMetadata
                               ▼
┌─────────────────────────────────────────────────────────────────────┐
│  Stage 2 · Parsing                                                   │
│  HTMLContentParser                                                   │
└──────────────────────────────┬──────────────────────────────────────┘
                               │ ContentTree
                               ▼
┌─────────────────────────────────────────────────────────────────────┐
│  Stage 3 · Table Normalization                                       │
│  TableFingerprinter · FingerprintClusterer · TableClassifier (LLM)  │
│  CanonicalSchemaStore · IncrementalNormalizer / BatchNormalizer      │
└──────────────────────────────┬──────────────────────────────────────┘
                               │ NormalizedRows + ContentTree
                               ▼
┌─────────────────────────────────────────────────────────────────────┐
│  Stage 4 · Entity Extraction                                         │
│  RegexScanner · LLMEntityExtractor · ValidationGateway              │
└──────────────────────────────┬──────────────────────────────────────┘
                               │ EntitySet
                               ▼
┌─────────────────────────────────────────────────────────────────────┐
│  Stage 5 · Graph Construction                                        │
│  TemporalGraphBuilder · MongoDBAdapter / Neo4jAdapter               │
│  (+ optional Graphiti episodic memory on Neo4j)                      │
└──────────────────────────────┬──────────────────────────────────────┘
                               │ Graph nodes + edges in MongoDB/Neo4j
                               ▼
┌─────────────────────────────────────────────────────────────────────┐
│  Stage 6 · Embedding + Retrieval                                     │
│  EmbeddingService (text-embedding-004) · SemanticRetriever          │
│  combined_score = cosine_similarity × temporal_decay                │
└──────────────────────────────┬──────────────────────────────────────┘
                               │
                               ▼
                         FastAPI layer
                    /ingest/* · /search · /graph/*
```

---

## Component Map

### Ingestion layer (`confluence_graphrag/ingestion/`)

| Class | Responsibility |
|---|---|
| `ConfluenceClient` | Async wrapper over atlassian-python-api. Paginates pages, parses meeting dates from titles, fetches attachments via httpx. |
| `BatchIngestor` | One-shot historical ingest. Pages sorted oldest-first. Idempotent (skips unchanged pages via `IngestionLog`). |
| `IncrementalWatcher` | APScheduler polling every N hours. Re-ingests pages whose Confluence `last_modified` is newer than what the log recorded. |
| `UpsertHandler` | Thin helper wrapping soft-delete + re-ingest of a single page. |
| `IngestionLog` | MongoDB collection (`ingestion_log`) tracking per-page status (`pending → processing → done / error / needs_review`). |

### Parser (`confluence_graphrag/parser/`)

| Class | Responsibility |
|---|---|
| `HTMLContentParser` | Converts Confluence storage-format HTML into a `ContentTree`. Handles rowspan/colspan propagation, nested tables, strikethrough detection (`CANCELLED` style), and attachment references. |

**Output model `ContentTree`:**
- `text_blocks: List[TextChunk]` — paragraph-level text with style and provenance
- `tables: List[ParsedTable]` — structured tables with headers, cells, and sub-tables
- `attachments: List[AttachmentRef]` — page-level attachment metadata

### Table Normalization (`confluence_graphrag/table_normalization/`)

| Class | Responsibility |
|---|---|
| `TableFingerprinter` | Produces a `TableFingerprint` (headers, col/row count, sample values) per table. |
| `FingerprintClusterer` | Groups fingerprints by header similarity using rapidfuzz + DBSCAN. Provides `best_match()` for incremental lookup. |
| `TableClassifier` | One LLM call (Gemini) per cluster batch. Outputs `CanonicalSchema` with `table_type`, `canonical_columns`, and a `column_mapping` dict (raw variant → canonical field). |
| `TableConsolidator` | Applies a `CanonicalSchema` to a `ParsedTable` to produce `NormalizedRow` list. |
| `CanonicalSchemaStore` | MongoDB-backed store for schemas. Supports `save_many`, `list_approved`, `approve`. |
| `BatchNormalizer` | D1 batch mode: fingerprint → cluster → classify → save as `PENDING_APPROVAL`. Human must call `approve()` before consolidation runs. |
| `IncrementalNormalizer` | Incremental mode: auto-assigns tables to nearest schema (threshold ≥ 0.7). Creates new `AUTO_APPROVED` schema if headers look novel. Routes unknowns to `unknown_tables` collection. |

### Entity Extraction (`confluence_graphrag/entity_extraction/`)

| Class | Responsibility |
|---|---|
| `RegexScanner` | Regex patterns for 11-digit `app_id` and 9-digit `project_id`. Fast, zero-cost, runs first. |
| `LLMEntityExtractor` | One Gemini call per page. Catches non-standard ID formats and extracts structured events (`decision`, `approval`, `cancellation`, `risk`, `action_item`, `status_change`). |
| `ValidationGateway` | Calls external validation API for each candidate ID. Semaphore-limited concurrency (configurable). Returns `ValidatedId` with `is_valid` flag. |
| `PageEntityExtractor` | Orchestrates the four steps above. Persists unvalidated IDs to `unvalidated_ids` collection. Returns `EntitySet`. |

### Graph (`confluence_graphrag/graph/`)

| Class | Responsibility |
|---|---|
| `TemporalGraphBuilder` | Stage 5 + 6 orchestrator. Upserts nodes and edges, runs embedding. |
| `MongoDBAdapter` | Default graph store. `graph_nodes` + `graph_edges` collections. Python-side cosine for vector search. |
| `Neo4jAdapter` | Production swap target. Full Cypher implementation. Vector search stubs (TODO). |
| `EmbeddingService` | Lazy wrapper over `GoogleGenerativeAIEmbeddings`. Batches all texts in one API call per page. |
| `SemanticRetriever` | Embeds a query, runs `vector_search_nodes` + `vector_search_rows`, re-ranks by `combined_score = cosine × temporal_decay`. |
| `scoring.py` | `temporal_score()` — exponential decay with 365-day half-life. `combined_score()` — semantic × temporal. |

### API (`confluence_graphrag/api/`)

| File | Responsibility |
|---|---|
| `app.py` | FastAPI app with lifespan. Initializes all singletons on startup, tears them down on shutdown. |
| `deps.py` | Typed FastAPI dependencies: `PipelineDep`, `AdapterDep`, `RetrieverDep`, `JobsDep`. |
| `schemas.py` | All request/response Pydantic models. |
| `routes/ingestion.py` | `/ingest/*` endpoints. Jobs run via `asyncio.create_task`. |
| `routes/retrieval.py` | `/search`, `/graph/node/{id}`, `/graph/app/{id}/timeline`. |

---

## Data Flow: Single Page Ingest

```
ConfluenceClient.get_page(page_id)
        │
        │  PageMetadata { page_id, title, space_key, html_content, page_date, attachments }
        ▼
HTMLContentParser.parse(html_content)
        │
        │  ContentTree { text_blocks, tables, attachments }
        ▼
IncrementalNormalizer.normalize_table(table)  [for each table]
        │
        │  NormalizedRow list (or None → unknown_tables)
        ▼
PageEntityExtractor.extract(content_tree)
        │
        │  EntitySet { page_id, page_date, app_ids, project_ids, events }
        ▼
TemporalGraphBuilder.ingest_page(entity_set, content_tree)
        ├── soft_delete_page(page_id)          [idempotency]
        ├── upsert ConfPage node
        ├── upsert Table nodes + CONTAINS edges
        ├── upsert Application nodes + REFERENCES_APP edges
        ├── upsert Project nodes + REFERENCES_PROJ edges
        ├── upsert Event nodes + HAS_EVENT edges
        ├── _embed_page()                       [Stage 6]
        │       ├── embed ConfPage text
        │       ├── embed all Event descriptions
        │       └── embed all table rows (→ graph_row_embeddings)
        └── _add_graphiti_episode()             [optional, Neo4j]
```

---

## Graph Schema

### Node types

| Type | `_id` format | Key properties |
|---|---|---|
| `ConfPage` | `page_{page_id}` | `page_id`, `title`, `date`, `has_cancelled` |
| `Table` | `{page_id}_table_{idx}` | `table_id`, `headers`, `row_count`, `page_id` |
| `Application` | `app_{app_id}` | `app_id`, `validated`, `first_seen`, `last_seen` |
| `Project` | `proj_{proj_id}` | `project_id`, `validated`, `first_seen`, `last_seen` |
| `Event` | `event_{page_id}_{idx}` | `event_type`, `description`, `app_ids`, `is_cancelled`, `page_id` |

All nodes carry `timestamp` (meeting date), `is_deleted`, and optionally `embedding` + `embedding_text`.

### Edge types

| Relation | Source → Target |
|---|---|
| `CONTAINS` | ConfPage → Table |
| `HAS_EVENT` | ConfPage → Event, Application → Event, Project → Event |
| `REFERENCES_APP` | ConfPage → Application |
| `REFERENCES_PROJ` | ConfPage → Project |

---

## Two Operating Modes

### Batch (historical)
```
python main.py --mode batch --space MY_SPACE [--start YYYY-MM-DD] [--end YYYY-MM-DD]
```
- Fetches all pages, sorts oldest-first
- Table schemas saved as `PENDING_APPROVAL` — human must approve before consolidation
- Use `BatchNormalizer.prepare_schemas()` + `CanonicalSchemaStore.approve()` workflow

### Incremental (continuous)
```
python main.py --mode incremental --space MY_SPACE
```
- Polls every `INCREMENTAL_POLL_INTERVAL_HOURS` (default 24h)
- Schemas are `AUTO_APPROVED` — no human gate
- Unknown tables routed to `unknown_tables` collection
