# memory-gateway

`memory-gateway` is a local long-term memory service for AI clients. It keeps durable user memories in SQLite, exposes a small MCP tool surface, and provides a Web console for reviewing, editing, exporting, restoring, and consolidating memory.

The external OpenAI-compatible `/v1` gateway has been deprecated. `/v1/models` and `/v1/chat/completions` now return `410 Gone`. Use `/mcp` for AI-client integration and `/memories/*` for memory management.

## Features

- MCP Streamable HTTP endpoint at `/mcp` for searching, surfacing, saving, and reading memory.
- Memory REST endpoints under `/memories` for the Web UI and local administration.
- Web console at `/ui` for memory library, core memory, review suggestions, reports, backups, recent context, and decision logs.
- Internal upstream LLM calls for memory extraction and core-memory consolidation through `UPSTREAM_*`.
- Optional OpenAI-compatible embedding provider for semantic search through `EMBEDDING_*`.

## Architecture

```text
AI client
  |
  | MCP Streamable HTTP /mcp
  v
memory-gateway
  |-- Bearer auth by GATEWAY_API_KEY
  |-- SQLite memory store
  |-- memory extraction and core-memory consolidation
  |-- Web console /ui
  |
  v
internal upstream LLM and embedding providers
```

## Quick Start

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .[dev]
```

Create `.env`:

```env
GATEWAY_API_KEY=change-me

UPSTREAM_BASE_URL=https://open.bigmodel.cn/api/paas/v4
UPSTREAM_API_KEY=your-upstream-api-key
UPSTREAM_MODEL=glm-5.1

EMBEDDING_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
EMBEDDING_API_KEY=
EMBEDDING_MODEL=text-embedding-v4
EMBEDDING_DIMENSIONS=1024

DATABASE_PATH=data/memory.db
REQUEST_TIMEOUT_SECONDS=60
```

Start the service:

```powershell
uvicorn app.main:app --host 0.0.0.0 --port 2026
```

Useful URLs:

| Purpose | URL |
| --- | --- |
| Health | `http://localhost:2026/health` |
| Web console | `http://localhost:2026/ui` |
| MCP | `http://localhost:2026/mcp` |

Except `/health`, protected endpoints require:

```http
Authorization: Bearer <GATEWAY_API_KEY>
X-User-Id: default
```

## MCP Tools

The MCP endpoint exposes a deliberately small tool set:

| Tool | Purpose |
| --- | --- |
| `search_memory` | Search relevant long-term memories for a query. |
| `surface_memories` | Surface high-value memories without a query. |
| `submit_memory_text` | Submit user text for extraction and storage. |
| `get_core_memory` | Read consolidated core-memory sections. |
| `get_recent_context_summary` | Read recent conversation summaries. |

## Web Console

The console at `/ui` supports:

- Memory library search, edit, soft-delete, restore, and source inspection.
- Core-memory consolidation and history.
- Memory review suggestions for merge, delete, lower priority, or manual review.
- Recent context and decision log inspection.
- JSON/Markdown export and JSON restore.
- Local connection settings and MCP/REST access information.

## REST Endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/health` | Health check. |
| `GET` | `/memories` | List active memories. |
| `GET` | `/memories/deleted` | List soft-deleted memories. |
| `POST` | `/memories/search` | Search memories. |
| `POST` | `/memories/surface` | Surface important memories. |
| `POST` | `/memories/ingest` | Extract memories from raw user text. |
| `POST` | `/memories` | Save one structured memory directly. |
| `POST` | `/memories/forget` | Soft-delete memories matching a natural-language query. |
| `POST` | `/memories/context` | Return core memory, search results, and recent context. |
| `PATCH` | `/memories/{memory_id}` | Update a memory. |
| `DELETE` | `/memories/{memory_id}` | Soft-delete a memory. |
| `POST` | `/memories/{memory_id}/restore` | Restore a soft-deleted memory. |
| `GET` | `/memories/{memory_id}/why` | Explain a memory source. |
| `POST` | `/memories/merge` | Merge several memories. |
| `POST` | `/memories/review` | Generate review suggestions. |
| `GET` | `/memories/report?format=json\|markdown` | Build a memory report. |
| `GET` | `/memories/export?format=json\|markdown` | Export memory backup data. |
| `POST` | `/memories/restore` | Restore from exported JSON. |
| `GET` | `/memories/core` | List current core-memory sections. |
| `GET` | `/memories/core/history` | List core-memory history. |
| `POST` | `/memories/core/consolidate` | Rebuild core memory from long-term memories. |
| `GET` | `/memories/recent-context` | List recent context summaries. |
| `GET` | `/memories/decision-logs` | List memory decision logs. |
| `GET` | `/v1/models` | Deprecated; returns `410 Gone`. |
| `POST` | `/v1/chat/completions` | Deprecated; returns `410 Gone`. |

## Development

Backend tests:

```powershell
pytest
```

Frontend:

```powershell
cd ui
npm install
npm run build
```

The UI build output goes to `ui/dist` and is mounted by FastAPI at `/ui`.

## Security Notes

- Do not commit `.env`, `data/*.db`, `logs/`, or real provider keys.
- `data/memory.db` may contain long-term memories and should be treated as sensitive.
- `GATEWAY_API_KEY` remains the shared local access token for MCP, REST, and the Web console.
- User separation depends on `X-User-Id`; this is intended for trusted local or private-network deployments.
