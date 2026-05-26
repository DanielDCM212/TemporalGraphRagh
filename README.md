# Confluence GraphRAG

A temporal knowledge graph system that ingests Confluence meeting minutes and makes them semantically queryable. It extracts entities (application IDs, project IDs, events), builds a time-aware graph, embeds content for semantic search, and exposes everything via a REST API.

## Quick Start

```bash
# 1. Install dependencies
uv sync

# 2. Copy and fill in environment variables
cp .env.example .env

# 3. Start MongoDB
docker run -d -p 27017:27017 mongo

# 4. Start the API
uvicorn confluence_graphrag.api.app:app --reload --port 8000
```

API docs: `http://localhost:8000/docs`

## Ingest a Confluence space

```bash
# Via CLI
python main.py --mode batch --space MY_SPACE

# Via API
curl -X POST http://localhost:8000/ingest/space \
  -H "Content-Type: application/json" \
  -d '{"space_key": "MY_SPACE"}'
```

## Search

```bash
curl -X POST http://localhost:8000/search \
  -H "Content-Type: application/json" \
  -d '{"query": "cancelled applications Q1 2024", "limit": 10}'
```

## Architecture

The system is a 6-stage pipeline. See [`docs/architecture.md`](docs/architecture.md) for the full breakdown.

```
Confluence API
     │
     ▼
Stage 1 · Ingestion          ConfluenceClient + IngestionLog
     │
     ▼
Stage 2 · Parsing            HTMLContentParser → ContentTree
     │
     ▼
Stage 3 · Table Normalization Fingerprint → Cluster → LLM classify → CanonicalSchema
     │
     ▼
Stage 4 · Entity Extraction  Regex + LLM → validate IDs → ExtractedEvents
     │
     ▼
Stage 5 · Graph Construction  TemporalGraphBuilder → MongoDB / Neo4j
     │
     ▼
Stage 6 · Embedding + Search  text-embedding-004 → cosine × temporal decay
```

## Documentation

| Document | Contents |
|---|---|
| [`docs/architecture.md`](docs/architecture.md) | Full pipeline architecture, component roles, data flow |
| [`docs/stages.md`](docs/stages.md) | Detailed breakdown of each stage |
| [`docs/data-model.md`](docs/data-model.md) | MongoDB collections and document schemas |
| [`docs/api.md`](docs/api.md) | REST API reference |
| [`docs/configuration.md`](docs/configuration.md) | All environment variables |
| [`docs/todos.md`](docs/todos.md) | Known gaps, TODOs, and future work |

## Tech Stack

- **Python 3.13**, FastAPI, uvicorn
- **MongoDB** (motor async driver) — primary graph store
- **Neo4j** (optional) — production graph backend + Graphiti episodic memory
- **LangChain + Google AI** — Gemini 2.0 Flash for LLM tasks, text-embedding-004 for embeddings
- **Confluence** — atlassian-python-api + httpx
- **APScheduler** — incremental polling scheduler
