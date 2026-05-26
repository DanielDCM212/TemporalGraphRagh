# Configuration

All configuration is read from a `.env` file in the project root. Copy `.env.example` and fill in your values.

```bash
cp .env.example .env
```

---

## Confluence

| Variable | Required | Default | Description |
|---|---|---|---|
| `CONFLUENCE_URL` | Yes | — | Base URL of your Confluence instance (e.g. `https://company.atlassian.net`) |
| `CONFLUENCE_USERNAME` | Yes | — | Confluence account email |
| `CONFLUENCE_API_TOKEN` | Yes | — | Confluence API token (create at id.atlassian.com/manage-profile/security/api-tokens) |
| `CONFLUENCE_CLOUD` | No | `true` | Set to `false` for Confluence Server / Data Center |

---

## MongoDB

| Variable | Required | Default | Description |
|---|---|---|---|
| `MONGODB_URI` | No | `mongodb://localhost:27017` | MongoDB connection string |
| `MONGODB_DB` | No | `confluence_graphrag` | Database name |

---

## Google AI (Gemini)

| Variable | Required | Default | Description |
|---|---|---|---|
| `GOOGLE_API_KEY` | No | — | Google AI API key. Required for LLM tasks (entity extraction, table classification) and embeddings (`/search`). Get one at aistudio.google.com. |

> If `GOOGLE_API_KEY` is missing, the pipeline will fail at the LLM steps (Stage 3 and Stage 4) and the `/search` endpoint returns `503`.

---

## Neo4j (optional)

Only required if `GRAPH_BACKEND=neo4j` or `GRAPHITI_ENABLED=true`.

| Variable | Required | Default | Description |
|---|---|---|---|
| `GRAPH_BACKEND` | No | `mongodb` | `mongodb` or `neo4j` |
| `NEO4J_URI` | No | — | Bolt URI (e.g. `bolt://localhost:7687`) |
| `NEO4J_USER` | No | — | Neo4j username |
| `NEO4J_PASSWORD` | No | — | Neo4j password |
| `GRAPHITI_ENABLED` | No | `false` | Enable Graphiti episodic memory layer (requires Neo4j) |
| `GCP_PROJECT` | No | — | GCP project ID for Graphiti's Vertex AI client |
| `GCP_LOCATION` | No | `us-central1` | GCP region for Graphiti's Vertex AI client |

---

## Ingestion behavior

| Variable | Required | Default | Description |
|---|---|---|---|
| `INCREMENTAL_POLL_INTERVAL_HOURS` | No | `24` | How often the incremental watcher polls Confluence |
| `CONFLUENCE_PAGE_LIMIT` | No | `50` | Pages per Confluence API call (1–100) |
| `MAX_RETRY_COUNT` | No | `3` | Max retries before a page is left in `error` status |

---

## Extraction

| Variable | Required | Default | Description |
|---|---|---|---|
| `VALIDATION_API_URL` | No | — | External REST API for ID validation. If empty, all IDs are treated as valid. |
| `VALIDATION_API_KEY` | No | — | Bearer token for the validation API |
| `VALIDATION_API_TIMEOUT` | No | `10.0` | Seconds before a validation call times out |
| `VALIDATION_CONCURRENCY` | No | `10` | Max concurrent validation API calls |
| `GEMINI_MODEL` | No | `gemini-2.0-flash-001` | Gemini model for LLM tasks |
| `EMBEDDING_MODEL` | No | `text-embedding-004` | Google embedding model |

---

## Minimal `.env` for local development

```env
# Required
CONFLUENCE_URL=https://your-company.atlassian.net
CONFLUENCE_USERNAME=your-email@company.com
CONFLUENCE_API_TOKEN=your-api-token
GOOGLE_API_KEY=your-google-ai-key

# MongoDB (if not using default localhost)
MONGODB_URI=mongodb://localhost:27017
```
